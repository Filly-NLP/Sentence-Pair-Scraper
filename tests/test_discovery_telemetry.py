from datetime import datetime
from unittest.mock import MagicMock
import httpx
import pytest

from src.crawler.discovery import DiscoveryEngine
from src.sources.registry import SitemapConfig, SourceConfig


class _MockResponse:
    def __init__(self, content: bytes, status_code: int = 200, headers: dict = None):
        self.content = content
        self.text = content.decode("utf-8", errors="ignore")
        self.status_code = status_code
        self.headers = headers or {}


def test_discovery_emits_truncation_on_document_budget(monkeypatch):
    root_index = b"""<sitemapindex>
      <sitemap><loc>https://example.com/s1.xml</loc></sitemap>
      <sitemap><loc>https://example.com/s2.xml</loc></sitemap>
      <sitemap><loc>https://example.com/s3.xml</loc></sitemap>
    </sitemapindex>"""
    child = b"""<urlset><url><loc>https://example.com/a1</loc></url></urlset>"""

    responses = {
        "https://example.com/root.xml": _MockResponse(root_index),
        "https://example.com/s1.xml": _MockResponse(child),
        "https://example.com/s2.xml": _MockResponse(child),
        "https://example.com/s3.xml": _MockResponse(child),
    }

    def mock_get(url, **kwargs):
        return responses[url]

    monkeypatch.setattr("src.crawler.discovery.httpx.get", mock_get)

    source = SourceConfig(
        id="test_src",
        name="Test",
        domain="example.com",
        enabled=True,
        sitemap=[SitemapConfig(url="https://example.com/root.xml")],
    )

    events = []
    engine = DiscoveryEngine(
        db_session=None,
        user_agent="TestBot/1.0",
        date_cutoff=datetime(2020, 1, 1),
        max_sitemap_documents=2,
        event_logger=lambda et, msg=None, stat=None: events.append((et, msg)),
    )

    report = engine.discover_source(source)

    assert report["truncated"] is True
    assert any(d["dimension"] == "documents" for d in report["truncation_details"])
    assert any(et == "discovery:truncated" for et, _ in events)


def test_discovery_handles_malformed_xml_and_records_telemetry(monkeypatch):
    responses = {
        "https://example.com/bad.xml": _MockResponse(b"this is totally not xml <<>>>"),
    }

    def mock_get(url, **kwargs):
        return responses[url]

    monkeypatch.setattr("src.crawler.discovery.httpx.get", mock_get)

    source = SourceConfig(
        id="test_src",
        name="Test",
        domain="example.com",
        enabled=True,
        sitemap=[SitemapConfig(url="https://example.com/bad.xml")],
    )

    events = []
    engine = DiscoveryEngine(
        db_session=None,
        user_agent="TestBot/1.0",
        date_cutoff=datetime(2020, 1, 1),
        event_logger=lambda et, msg=None, stat=None: events.append((et, msg)),
    )

    report = engine.discover_source(source)

    assert len(report["errors"]) == 1
    assert report["errors"][0]["type"] == "sitemap_malformed"
    assert any(et == "error:sitemap_malformed" for et, _ in events)


def test_discovery_counts_and_samples_out_of_scope_candidates(monkeypatch):
    xml_with_mixed_urls = b"""<urlset>
      <url><loc>https://example.com/valid/article-1</loc></url>
      <url><loc>https://example.com/other/page-1</loc></url>
      <url><loc>https://example.com/other/page-2</loc></url>
      <url><loc>https://different-domain.com/article</loc></url>
    </urlset>"""

    def mock_get(url, **kwargs):
        return _MockResponse(xml_with_mixed_urls)

    monkeypatch.setattr("src.crawler.discovery.httpx.get", mock_get)

    source = SourceConfig(
        id="test_src",
        name="Test",
        domain="example.com",
        enabled=True,
        article_path_patterns=[r"^/valid/[^/]+$"],
        sitemap=[SitemapConfig(url="https://example.com/sitemap.xml")],
    )

    engine = DiscoveryEngine(
        db_session=None,
        user_agent="TestBot/1.0",
        date_cutoff=datetime(2020, 1, 1),
    )

    report = engine.discover_source(source)

    assert report["candidates_found"] == 4
    assert report["candidates_added"] == 1
    assert report["out_of_scope_skipped"] == 3
    assert len(report["sample_out_of_scope"]) == 3
    assert "https://example.com/other/page-1" in report["sample_out_of_scope"]
