from datetime import datetime

from src.crawler.discovery import DiscoveryEngine
from src.sources.registry import SitemapConfig, SourceConfig


class _Response:
    status_code = 200

    def __init__(self, content):
        self.content = content
        self.text = content.decode("utf-8")


def test_sitemap_document_budget_is_shared_across_roots(monkeypatch, db_session):
    root = b"""<sitemapindex><sitemap><loc>https://example.com/child.xml</loc></sitemap></sitemapindex>"""
    child = b"""<urlset><url><loc>https://example.com/article</loc></url></urlset>"""
    second = b"""<urlset><url><loc>https://example.com/second</loc></url></urlset>"""
    responses = {
        "https://example.com/root.xml": _Response(root),
        "https://example.com/child.xml": _Response(child),
        "https://example.com/second.xml": _Response(second),
    }
    fetched = []

    def fake_get(url, **kwargs):
        fetched.append(url)
        return responses[url]

    monkeypatch.setattr("src.crawler.discovery.httpx.get", fake_get)
    source = SourceConfig(
        id="test", name="Test", domain="example.com", enabled=True, language="filipino",
        sitemap=[SitemapConfig(url="https://example.com/root.xml"), SitemapConfig(url="https://example.com/second.xml")],
    )
    engine = DiscoveryEngine(db_session, "agent", datetime(2022, 1, 1), max_sitemap_documents=2)

    seen, added = engine.discover_source_urls(source)

    assert fetched == ["https://example.com/root.xml", "https://example.com/child.xml"]
    assert (seen, added) == (1, 1)
