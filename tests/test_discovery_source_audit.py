import gzip
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

from src.crawler.discovery import DiscoveryEngine
from src.crawler.robots import RobotsManager
from src.crawler.rss_parser import RSSParser
from src.sources.registry import SitemapConfig, SourceConfig


FIXTURES = Path(__file__).parent / "fixtures" / "discovery"


class _Response:
    status_code = 200

    def __init__(self, content: bytes, *, content_type="application/xml", url=None, downloaded=None):
        self.content = content
        self.text = content.decode("utf-8", errors="ignore")
        self.headers = {"Content-Type": content_type, "Content-Encoding": "gzip" if downloaded else "identity"}
        self.url = url
        self.num_bytes_downloaded = downloaded


def test_root_audit_records_redirect_size_kind_and_entries(monkeypatch):
    xml = b"<urlset><url><loc>https://example.com/article-1</loc><lastmod>2026-08-11</lastmod></url></urlset>"
    monkeypatch.setattr(
        "src.crawler.discovery.httpx.get",
        lambda url, **kwargs: _Response(xml, content_type="text/plain", url="https://example.com/final.xml", downloaded=123),
    )
    source = SourceConfig(
        id="audit_root", name="Audit root", domain="example.com",
        article_path_patterns=[r"^/article-\d+$"],
        sitemap=[SitemapConfig(url="https://example.com/root.xml")],
    )
    report = DiscoveryEngine(None, "OfflineTestBot/1.0", datetime(2022, 1, 1)).discover_source(source)
    root = report.roots[0]
    assert root.final_url == "https://example.com/final.xml"
    assert root.status == 200
    assert root.content_type == "text/plain"
    assert root.transferred_bytes == 123
    assert root.decoded_bytes == len(xml)
    assert root.document_kind == "urlset"
    assert root.parsed_entries == 1
    assert report["would_queue"] == 1


def test_http_200_html_is_not_malformed_feed_or_sitemap(monkeypatch):
    html = (FIXTURES / "html_200.html").read_bytes()
    monkeypatch.setattr("src.crawler.discovery.httpx.get", lambda url, **kwargs: _Response(html, content_type="text/html"))
    source = SourceConfig(
        id="html_root", name="HTML root", domain="example.com",
        sitemap=[SitemapConfig(url="https://example.com/root.xml")],
    )
    report = DiscoveryEngine(None, "OfflineTestBot/1.0", datetime(2022, 1, 1)).discover_source(source)
    assert report.roots[0].document_kind == "html"
    assert report["parse_errors"] == 0
    assert report["candidates_seen"] == 0


def test_nested_gzip_and_malformed_child_keep_valid_sibling(monkeypatch):
    valid = b"<urlset><url><loc>https://example.com/article-1</loc></url></urlset>"
    index = b"<sitemapindex><sitemap><loc>https://example.com/bad.xml</loc></sitemap><sitemap><loc>https://example.com/valid.xml.gz</loc></sitemap></sitemapindex>"
    responses = {
        "https://example.com/index.xml": _Response(index),
        "https://example.com/bad.xml": _Response(b"not xml <<>>"),
        "https://example.com/valid.xml.gz": _Response(gzip.compress(gzip.compress(valid)), downloaded=88),
    }
    monkeypatch.setattr("src.crawler.discovery.httpx.get", lambda url, **kwargs: responses[url])
    source = SourceConfig(
        id="nested", name="Nested", domain="example.com",
        article_path_patterns=[r"^/article-\d+$"],
        sitemap=[SitemapConfig(url="https://example.com/index.xml")],
    )
    report = DiscoveryEngine(None, "OfflineTestBot/1.0", datetime(2022, 1, 1)).discover_source(source)
    assert report["parse_errors"] == 1
    assert report["accepted_unique"] == 1


def test_rss_parse_result_retains_kind_and_bozo_compatibility():
    result = RSSParser.parse_result((FIXTURES / "sample_feed.xml").read_text(encoding="utf-8"))
    assert result.kind == "RSS"
    assert result.bozo is False
    assert result.parsed_entries == 2
    assert len(RSSParser.parse_feed((FIXTURES / "sample_feed.xml").read_text(encoding="utf-8"))) == 2


def test_robots_denial_prevents_rss_fetch(monkeypatch):
    robots = MagicMock(spec=RobotsManager)
    robots.is_allowed.return_value = False
    robots.get_sitemaps.return_value = []
    fetched = []
    monkeypatch.setattr(
        "src.crawler.discovery.httpx.get",
        lambda url, **kwargs: fetched.append(url),
    )
    source = SourceConfig(
        id="rss_robots", name="RSS robots", domain="example.com",
        article_path_patterns=[r"^/article-\d+$"],
        rss=[type("Feed", (), {"url": "https://example.com/feed.xml"})()],
    )
    report = DiscoveryEngine(
        None, "OfflineTestBot/1.0", datetime(2022, 1, 1), robots_mgr=robots
    ).discover_source(source)
    assert fetched == []
    assert report.roots[0].errors == ["robots_disallowed"]
