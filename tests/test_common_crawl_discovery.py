import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from src.crawler.common_crawl_discovery import CommonCrawlCache, CommonCrawlDiscoveryEngine
from src.crawler.config import CommonCrawlConfig, CrawlerConfig, CrawlerConfigValidationError
from src.crawler.pipeline import CrawlPipeline
from src.sources.registry import CommonCrawlSourceConfig, SourceConfig, SourceRegistry, ConfigValidationError
from src.storage.models import CrawlRun, URL


def _source(enabled=True, patterns=None):
    return SourceConfig(
        id="example",
        name="Example",
        domain="example.com",
        enabled=True,
        article_path_patterns=[r"^/news/20\d{2}/[^/]+$"],
        common_crawl=CommonCrawlSourceConfig(
            enabled=enabled,
            url_patterns=patterns or ["https://example.com/news/20*/*"],
        ),
    )


def _settings(tmp_path, **overrides):
    values = {
        "enabled": True,
        "index_collection": "CC-MAIN-2026-30",
        "max_requests_per_source_run": 10,
        "max_index_pages": 10,
        "max_candidates_per_source_run": 20,
        "max_response_bytes": 10000,
        "max_total_response_bytes_per_source_run": 50000,
        "timeout_seconds": 1,
        "cache_dir": str(tmp_path / "cc-cache"),
    }
    values.update(overrides)
    return CommonCrawlConfig.from_mapping(values)


def _transport(responses, calls):
    def handler(request):
        calls.append(request)
        params = request.url.params
        key = (params.get("url"), params.get("showNumPages"), params.get("page"))
        body, status = responses[key]
        return httpx.Response(status, content=body, headers={"content-type": "application/json"}, request=request)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_disabled_provider_is_side_effect_free(tmp_path, db_session):
    calls = []

    def handler(request):
        calls.append(request)
        raise AssertionError("disabled Common Crawl must not make a request")

    cache_dir = tmp_path / "not-created"
    engine = CommonCrawlDiscoveryEngine(
        db_session=db_session,
        user_agent="TestBot/1.0",
        config=CommonCrawlConfig(enabled=False, cache_dir=str(cache_dir)),
    )
    source = _source(enabled=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        report = await engine.discover_source_async(source, client=client)

    assert report.metadata["common_crawl"]["reason"] == "global_disabled"
    assert calls == []
    assert not cache_dir.exists()
    assert db_session.query(URL).count() == 0


@pytest.mark.asyncio
async def test_invalid_direct_source_pattern_fails_closed(tmp_path, db_session):
    calls = []

    def handler(request):
        calls.append(request)
        raise AssertionError("unsafe source pattern must not be queried")

    engine = CommonCrawlDiscoveryEngine(
        db_session=db_session,
        user_agent="TestBot/1.0",
        config=_settings(tmp_path),
    )
    source = _source(patterns=["https://other.example/news/*"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        report = await engine.discover_source_async(source, client=client)
    assert report.metadata["common_crawl"]["reason"] == "source_patterns_missing"
    assert calls == []
    assert db_session.query(URL).count() == 0


def test_pipeline_global_disabled_is_side_effect_free(tmp_path, db_session):
    config_path = tmp_path / "crawler.yaml"
    config_path.write_text(
        f"""common_crawl:
  enabled: false
  index_collection: null
  cache_dir: "{(tmp_path / 'cc-cache').as_posix()}"
cache:
  http_cache_dir: "{(tmp_path / 'http-cache').as_posix()}"
storage:
  database_url: "sqlite:///:memory:"
""",
        encoding="utf-8",
    )
    pipeline = CrawlPipeline(db_session, CrawlerConfig(config_path), "TestBot/1.0")
    found, added = pipeline.discover_common_crawl_urls([_source(enabled=True)])
    assert (found, added) == (0, 0)
    assert db_session.query(CrawlRun).count() == 0
    assert not (tmp_path / "cc-cache").exists()


@pytest.mark.asyncio
async def test_index_pages_are_parsed_scoped_deduplicated_and_cached(tmp_path, db_session):
    pattern = "https://example.com/news/20*/*"
    page_zero = b"\n".join([
        json.dumps({"url": "https://example.com/news/2026/one", "timestamp": "19000101000000", "filename": "never-fetch"}).encode(),
        json.dumps({"url": "https://example.com/news/2026/two"}).encode(),
        b"not-json",
    ])
    page_one = b"\n".join([
        json.dumps({"url": "https://example.com/news/2026/two"}).encode(),
        json.dumps({"url": "https://other.example/news/2026/out"}).encode(),
        json.dumps({"url": "https://example.com/news/2026/three"}).encode(),
    ])
    responses = {
        (pattern, "true", None): (b'{"pages": 2}', 200),
        (pattern, None, "0"): (page_zero, 200),
        (pattern, None, "1"): (page_one, 200),
    }
    calls = []
    source = _source(patterns=[pattern])
    engine = CommonCrawlDiscoveryEngine(
        db_session=db_session,
        user_agent="TestBot/1.0",
        config=_settings(tmp_path),
        date_cutoff=datetime(2022, 1, 1),
    )
    async with httpx.AsyncClient(transport=_transport(responses, calls), follow_redirects=False) as client:
        report = await engine.discover_source_async(source, client=client)

    assert report.candidates_seen == 5
    assert report.parse_errors == 1
    assert report.out_of_scope_skipped == 1
    assert report.candidates_added == 3
    assert report.truncated is False
    assert report.metadata["common_crawl"]["pages_fetched"] == 2
    assert report.metadata["common_crawl"]["requests_made"] == 3
    assert all(item.discovery_method == "COMMON_CRAWL" for item in db_session.query(URL).all())
    assert all(item.publication_date_hint is None and item.sitemap_lastmod_hint is None for item in db_session.query(URL).all())
    assert all("filename" not in str(request.url) for request in calls)
    assert len(list((tmp_path / "cc-cache").glob("*.json"))) == 3

    second_calls = []
    second = CommonCrawlDiscoveryEngine(
        db_session=db_session,
        user_agent="TestBot/1.0",
        config=_settings(tmp_path),
        date_cutoff=datetime(2022, 1, 1),
    )
    async with httpx.AsyncClient(transport=_transport({}, second_calls), follow_redirects=False) as client:
        rerun = await second.discover_source_async(source, client=client)
    assert second_calls == []
    assert rerun.metadata["common_crawl"]["cache_hits"] == 3
    assert rerun.candidates_added == 0
    assert rerun.already_stored == rerun.accepted_unique


@pytest.mark.asyncio
async def test_response_budget_truncates_and_does_not_cache_oversized_page(tmp_path, db_session):
    pattern = "https://example.com/news/20*/*"
    responses = {
        (pattern, "true", None): (b'{"pages": 1}', 200),
        (pattern, None, "0"): (b"x" * 101, 200),
    }
    calls = []
    engine = CommonCrawlDiscoveryEngine(
        db_session=db_session,
        user_agent="TestBot/1.0",
        config=_settings(tmp_path, max_response_bytes=100),
    )
    async with httpx.AsyncClient(transport=_transport(responses, calls), follow_redirects=False) as client:
        report = await engine.discover_source_async(_source(patterns=[pattern]), client=client)
    assert report.truncated is True
    assert "response_bytes" in report.truncation_dimensions
    assert report.candidates_added == 0
    assert len(list((tmp_path / "cc-cache").glob("*.json"))) == 1  # pagination metadata only


def test_common_crawl_validation_is_fail_closed(tmp_path):
    crawler_path = tmp_path / "crawler.yaml"
    crawler_path.write_text(
        "common_crawl:\n  enabled: true\n  index_collection: latest\n",
        encoding="utf-8",
    )
    with pytest.raises(CrawlerConfigValidationError):
        CrawlerConfig(crawler_path)

    crawler_path.write_text(
        "common_crawl:\n  enabled: true\n  index_collection: CC-MAIN-2026-30\n  max_index_pages: true\n",
        encoding="utf-8",
    )
    with pytest.raises(CrawlerConfigValidationError):
        CrawlerConfig(crawler_path)

    source_path = tmp_path / "sources.yaml"
    source_path.write_text(
        "sources:\n  - id: broad\n    name: Broad\n    domain: www.philstar.com\n    common_crawl:\n      enabled: true\n      url_patterns: ['https://www.philstar.com/*']\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigValidationError):
        SourceRegistry(source_path)


def test_cache_validates_digest_and_writes_atomically(tmp_path):
    cache = CommonCrawlCache(tmp_path / "cache")
    identity = {"collection": "CC-MAIN-2026-30", "pattern": "https://example.com/news/*", "kind": "page", "page": 0}
    body = b'{"url":"https://example.com/news/1"}\n'
    assert cache.set(identity, body)
    assert cache.get(identity) == body
    path = cache.path_for(identity)
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["sha256"] = "0" * 64
    path.write_text(json.dumps(envelope), encoding="utf-8")
    assert cache.get(identity) is None
    assert not list((tmp_path / "cache").glob("*.tmp"))
