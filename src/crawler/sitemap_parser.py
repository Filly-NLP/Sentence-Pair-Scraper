import gzip
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


class SitemapParser:
    SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
    NEWS_NS = "http://www.google.com/schemas/sitemap-news/0.9"

    @staticmethod
    def looks_like_html(content: Any) -> bool:
        if isinstance(content, bytes):
            sample = content[:2048].decode("utf-8", errors="ignore")
        else:
            sample = str(content or "")[:2048]
        stripped = sample.lstrip().lower()
        return stripped.startswith("<!doctype html") or stripped.startswith("<html") or "<html" in stripped[:200]

    @classmethod
    def parse_document(
        cls,
        xml_content: Any,
        url_hint: Optional[str] = None,
        content_encoding: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return kind, child sitemap locations, and URL entries.

        xml_content may be text or bytes. Gzipped sitemap documents are detected
        via magic bytes (\x1f\x8b), URL .gz suffix, or content_encoding header.
        Malformed documents return a structured error result rather than throwing.
        """
        result: Dict[str, Any] = {"kind": "unknown", "sitemaps": [], "urls": [], "error": None}
        if xml_content is None or (isinstance(xml_content, (str, bytes)) and len(xml_content) == 0):
            result["error"] = "empty_content"
            return result

        if cls.looks_like_html(xml_content):
            result["kind"] = "html"
            result["error"] = "html_document"
            return result

        payload: bytes
        try:
            if isinstance(xml_content, str):
                payload = xml_content.encode("utf-8")
            else:
                payload = bytes(xml_content)

            is_gzipped = (
                payload[:2] == b"\x1f\x8b"
                or (url_hint is not None and url_hint.lower().endswith(".gz"))
                or (content_encoding is not None and "gzip" in content_encoding.lower())
            )
            # httpx exposes decoded ``response.content`` while retaining the
            # original Content-Encoding header. Do not decompress XML bytes a
            # second time merely because that header is still present.
            if content_encoding and "gzip" in content_encoding.lower() and payload.lstrip().startswith(b"<"):
                is_gzipped = bool(
                    payload[:2] == b"\x1f\x8b"
                    or (url_hint is not None and url_hint.lower().endswith(".gz"))
                )
            if is_gzipped:
                try:
                    # A sitemap index commonly points at .xml.gz children. A
                    # few fixtures and gateways wrap those bytes twice; peel
                    # only bounded gzip layers and leave XML handling below.
                    for _ in range(3):
                        payload = gzip.decompress(payload)
                        if payload[:2] != b"\x1f\x8b":
                            break
                except Exception as exc:
                    result["kind"] = "malformed"
                    result["error"] = f"decompression_failed: {exc}"
                    return result

            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            result["kind"] = "malformed"
            result["error"] = f"malformed_xml: {exc}"
            return result
        except Exception as exc:
            result["kind"] = "malformed"
            result["error"] = str(exc)
            return result

        def local_name(tag: str) -> str:
            if not isinstance(tag, str):
                return ""
            return tag.rsplit("}", 1)[-1].lower()

        root_name = local_name(root.tag)
        if root_name == "sitemapindex":
            result["kind"] = "index"
            for node in root.iter():
                if local_name(node.tag) != "sitemap":
                    continue
                loc = next((child for child in node if local_name(child.tag) == "loc"), None)
                if loc is not None and loc.text and loc.text.strip():
                    result["sitemaps"].append(loc.text.strip())
            return result

        if root_name == "urlset":
            result["kind"] = "urlset"
            for url_node in root.iter():
                if local_name(url_node.tag) != "url":
                    continue
                loc_node = next((child for child in url_node if local_name(child.tag) == "loc"), None)
                if loc_node is None or not loc_node.text or not loc_node.text.strip():
                    continue
                lastmod_node = next((child for child in url_node if local_name(child.tag) == "lastmod"), None)
                lastmod = cls.parse_lastmod(lastmod_node.text if lastmod_node is not None else None)
                publication_date = None
                for child in url_node.iter():
                    tag_name = local_name(child.tag)
                    if tag_name in ("publication_date", "news:publication_date") and child.text:
                        publication_date = cls.parse_lastmod(child.text)
                        break
                result["urls"].append({
                    "url": loc_node.text.strip(),
                    "lastmod": lastmod,
                    "publication_date": publication_date,
                })
            return result

        result["kind"] = "unknown"
        result["error"] = f"unsupported_root_element: {root.tag}"
        return result

    @staticmethod
    def parse_lastmod(value: str | None) -> datetime | None:
        """Parse sitemap dates, preserving timezone information when present."""
        if not value:
            return None
        value = value.strip()
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                return None
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc)
        return parsed

    @staticmethod
    def parse_sitemap(xml_content: str) -> List[Dict[str, Any]]:
        """Parse standard sitemap XML and extract URLs and lastmod timestamps."""
        return SitemapParser.parse_document(xml_content).get("urls", [])
