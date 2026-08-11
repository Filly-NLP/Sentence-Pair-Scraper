import xml.etree.ElementTree as ET
from typing import List, Dict, Any
from datetime import datetime

class SitemapParser:
    @staticmethod
    def parse_sitemap(xml_content: str) -> List[Dict[str, Any]]:
        """Parse standard sitemap XML and extract URLs and lastmod timestamps."""
        urls = []
        try:
            # Handle XML namespaces dynamically
            root = ET.fromstring(xml_content)
            # Find all <url> tags regardless of namespace
            # Standard namespaces are: http://www.sitemaps.org/schemas/sitemap/0.9
            ns = {"ns": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            
            # Try parsing with namespace first, fallback to generic
            url_elements = root.findall(".//ns:url", ns)
            if not url_elements:
                url_elements = root.findall(".//url")

            for url_node in url_elements:
                loc_node = url_node.find("ns:loc", ns) if url_node.find("ns:loc", ns) is not None else url_node.find("loc")
                lastmod_node = url_node.find("ns:lastmod", ns) if url_node.find("ns:lastmod", ns) is not None else url_node.find("lastmod")
                
                url_val = loc_node.text.strip() if loc_node is not None and loc_node.text else ""
                if not url_val:
                    continue

                lastmod_val = None
                if lastmod_node is not None and lastmod_node.text:
                    try:
                        # Parse ISO 8601 formatting (e.g. 2026-08-11T12:00:00Z or 2026-08-11)
                        date_str = lastmod_node.text.strip()
                        if "T" in date_str:
                            lastmod_val = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                        else:
                            lastmod_val = datetime.strptime(date_str, "%Y-%m-%d")
                    except Exception:
                        pass

                urls.append({
                    "url": url_val,
                    "lastmod": lastmod_val
                })
        except Exception:
            pass
        return urls
