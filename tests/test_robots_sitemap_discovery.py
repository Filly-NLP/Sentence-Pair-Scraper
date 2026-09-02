from datetime import datetime
from unittest.mock import MagicMock
import httpx
import pytest

from src.crawler.discovery import DiscoveryEngine
from src.crawler.robots import RobotsManager
from src.sources.registry import SitemapConfig, SourceConfig


class _MockResponse:
    def __init__(self, text: str, status_code: int = 200, content: bytes = b""):
        self.text = text
        self.status_code = status_code
        self.content = content or text.encode("utf-8")
        self.headers = {}


def test_robots_manager_parses_sitemaps(monkeypatch):
    robots_content = """User-agent: *
Disallow: /admin
Sitemap: https://example.com/sitemap-1.xml
Sitemap: https://example.com/sitemap-news.xml
"""
    def mock_get(url, **kwargs):
        return _MockResponse(robots_content)

    monkeypatch.setattr(httpx, "get", mock_get)

    mgr = RobotsManager("TestBot/1.0")
    sitemaps = mgr.get_sitemaps("example.com")

    assert sitemaps == [
        "https://example.com/sitemap-1.xml",
        "https://example.com/sitemap-news.xml",
    ]


def test_discovery_combines_and_deduplicates_explicit_and_robots_roots(monkeypatch):
    mgr = MagicMock(spec=RobotsManager)
    # Robots.txt provides duplicate of explicit root + one new root
    mgr.get_sitemaps.return_value = [
        "https://example.com/explicit.xml",
        "https://example.com/from-robots.xml",
    ]

    sitemap_xml = b"""<?xml version="1.0"?><urlset>
      <url><loc>https://example.com/article-1</loc></url>
    </urlset>"""

    fetched_urls = []
    def mock_get(url, **kwargs):
        fetched_urls.append(url)
        return _MockResponse("", content=sitemap_xml)

    monkeypatch.setattr("src.crawler.discovery.httpx.get", mock_get)

    source = SourceConfig(
        id="test_src",
        name="Test",
        domain="example.com",
        enabled=True,
        sitemap=[SitemapConfig(url="https://example.com/explicit.xml")],
    )

    engine = DiscoveryEngine(
        db_session=None,
        user_agent="TestBot/1.0",
        date_cutoff=datetime(2020, 1, 1),
        robots_mgr=mgr,
    )

    report = engine.discover_source(source)

    # Roots should contain both distinct roots without duplication
    assert report["roots"] == [
        "https://example.com/explicit.xml",
        "https://example.com/from-robots.xml",
    ]
    assert report["documents_fetched"] == 2
    assert report["candidates_found"] == 2


def test_sitemap_host_policy_permits_allowed_sitemap_hosts(monkeypatch):
    sitemap_xml = b"""<?xml version="1.0"?><urlset>
      <url><loc>https://example.com/article-1</loc></url>
    </urlset>"""

    fetched = []
    def mock_get(url, **kwargs):
        fetched.append(url)
        return _MockResponse("", content=sitemap_xml)

    monkeypatch.setattr("src.crawler.discovery.httpx.get", mock_get)

    source = SourceConfig(
        id="test_src",
        name="Test",
        domain="example.com",
        enabled=True,
        allowed_sitemap_hosts=["cdn.sitemaps.org"],
        sitemap=[
            SitemapConfig(url="https://cdn.sitemaps.org/sitemap.xml"),
            SitemapConfig(url="https://unauthorized.com/sitemap.xml"),
        ],
    )

    events = []
    engine = DiscoveryEngine(
        db_session=None,
        user_agent="TestBot/1.0",
        date_cutoff=datetime(2020, 1, 1),
        event_logger=lambda et, msg=None, stat=None: events.append((et, msg)),
    )

    report = engine.discover_source(source)

    assert "https://cdn.sitemaps.org/sitemap.xml" in fetched
    assert "https://unauthorized.com/sitemap.xml" not in fetched
    assert any(et == "skipped:sitemap_host" for et, _ in events)
