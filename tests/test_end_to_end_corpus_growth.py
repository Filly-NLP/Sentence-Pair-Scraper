import asyncio
import gzip
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.crawler.config import CrawlerConfig
from src.crawler.discovery import DiscoveryEngine
from src.crawler.archive_discovery import ArchiveDiscoveryEngine
from src.crawler.fetcher import FetchResult
from src.crawler.pipeline import CrawlPipeline
from src.crawler.robots import RobotsManager
from src.sources.registry import SourceConfig, SitemapConfig, ArchiveConfig
from src.storage.database import Base
from src.storage.models import Article, Source, URL, URLStatus


class _MockResponse:
    def __init__(self, text: str = "", status_code: int = 200, content: bytes = b"", headers: dict = None):
        self.text = text
        self.status_code = status_code
        self.content = content if content else text.encode("utf-8")
        self.headers = headers or {}


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


# Scenario 1: Robots declares a nested gzip sitemap yielding new and duplicate URLs
def test_robots_nested_gzip_sitemap_discovery(db_session, monkeypatch):
    child_sitemap_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://example.com/2023/05/01/article-1</loc>
    <lastmod>2023-05-01T10:00:00Z</lastmod>
  </url>
  <url>
    <loc>https://example.com/2023/05/01/article-2</loc>
    <lastmod>2023-05-01T11:00:00Z</lastmod>
  </url>
  <url>
    <loc>https://example.com/2023/05/01/article-1</loc>
  </url>
</urlset>"""
    child_gzip = gzip.compress(child_sitemap_xml)

    index_sitemap_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap>
    <loc>https://example.com/sitemap-child.xml.gz</loc>
    <lastmod>2023-05-01</lastmod>
  </sitemap>
</sitemapindex>"""

    responses = {
        "https://example.com/robots.txt": _MockResponse("User-agent: *\nSitemap: https://example.com/sitemap-index.xml\n"),
        "https://example.com/sitemap-index.xml": _MockResponse("", content=index_sitemap_xml),
        "https://example.com/sitemap-child.xml.gz": _MockResponse("", content=child_gzip),
    }

    def mock_get(url, **kwargs):
        return responses.get(url, _MockResponse(status_code=404))

    monkeypatch.setattr("src.crawler.discovery.httpx.get", mock_get)
    monkeypatch.setattr("src.crawler.robots.httpx.get", mock_get)

    source_cfg = SourceConfig(
        id="test_gzip",
        name="Test Gzip",
        domain="example.com",
        enabled=True,
        article_path_patterns=[r"^/20\d{2}/\d{2}/\d{2}/[^/]+/?$"],
    )

    mgr = RobotsManager("TestBot/1.0")
    engine = DiscoveryEngine(
        db_session=db_session,
        user_agent="TestBot/1.0",
        date_cutoff=datetime(2022, 1, 1),
        robots_mgr=mgr,
    )

    report = engine.discover_source(source_cfg)
    assert report["candidates_added"] == 2
    assert report["duplicates_skipped"] >= 1
    assert db_session.query(URL).filter(URL.source_id == "test_gzip").count() == 2


# Scenario 2: Archive overlaps RSS/sitemap without duplicate queue rows
def test_archive_overlap_no_duplicate_queue_rows(db_session, monkeypatch):
    source_cfg = SourceConfig(
        id="overlap_src",
        name="Overlap Test",
        domain="example.com",
        enabled=True,
        article_path_patterns=[r"^/20\d{2}/\d{2}/\d{2}/[^/]+/?$"],
        archive=ArchiveConfig(
            enabled=True,
            granularity="day",
            url_template="https://example.com/archive/{year}/{month}/{day}",
            start_date="2023-01-01",
            end_date="2023-01-01",
            article_link_selector="a",
        )
    )
    # Pre-populate database with URL from RSS
    db_session.add(Source(source_id="overlap_src", name="Overlap Test", domain="example.com", enabled=True, language="filipino"))
    db_session.add(URL(
        url="https://example.com/2023/01/01/article-shared",
        source_id="overlap_src",
        status=URLStatus.DISCOVERED,
        discovery_method="RSS",
    ))
    db_session.commit()

    archive_html = """
    <html><body>
      <a href="/2023/01/01/article-shared">Shared Article</a>
      <a href="/2023/01/01/article-new">New Article</a>
    </body></html>
    """

    def mock_get(url, **kwargs):
        return _MockResponse(archive_html)

    monkeypatch.setattr("src.crawler.archive_discovery.httpx.get", mock_get)

    archive_engine = ArchiveDiscoveryEngine(
        db_session=db_session,
        user_agent="TestBot/1.0",
        date_cutoff=datetime(2022, 1, 1),
        apply_delay=False,
    )

    report = archive_engine.discover_source(source_cfg)
    assert report["links_found"] == 2
    assert report["links_added"] == 1  # only new article added

    urls = db_session.query(URL).filter(URL.source_id == "overlap_src").all()
    assert len(urls) == 2
    urls_map = {u.url: u.discovery_method for u in urls}
    assert urls_map["https://example.com/2023/01/01/article-shared"] == "RSS"
    assert urls_map["https://example.com/2023/01/01/article-new"] == "ARCHIVE"


# Scenario 3 & 4: 202 retry and repeated 403 cooldown/block
@pytest.mark.asyncio
async def test_transient_202_retry_and_403_cooldown_blocking(db_session, monkeypatch):
    source = Source(source_id="http_test", name="HTTP Test", domain="example.com", enabled=True)
    url_202 = URL(url_id=1, source_id="http_test", url="https://example.com/2023/01/01/retrying-202", status=URLStatus.DISCOVERED)
    url_403_a = URL(url_id=2, source_id="http_test", url="https://example.com/2023/01/01/blocked-403-a", status=URLStatus.DISCOVERED)
    url_403_b = URL(url_id=3, source_id="http_test", url="https://example.com/2023/01/01/blocked-403-b", status=URLStatus.DISCOVERED)
    db_session.add_all([source, url_202, url_403_a, url_403_b])
    db_session.commit()

    with tempfile.TemporaryDirectory() as td:
        cfg_file = Path(td) / "crawler.yaml"
        cfg_file.write_text("""crawler:
  version: "1.0.0"
  date_cutoff: "2020-01-01"
  queue_batch_size: 10
  processing_timeout_seconds: 60
  max_lifecycle_retries: 3
  max_403_retries: 1
http:
  timeout_seconds: 5
  max_retries: 1
rate_limiting:
  default_delay_seconds: 0
  default_max_concurrent: 1
  backoff:
    initial_seconds: 10
    multiplier: 2
    max_seconds: 60
    jitter: false
  cooldown:
    trigger_threshold: 2
    duration_seconds: 120
sentence:
  min_tokens: 3
  max_tokens: 100
  include_quotes: true
language:
  min_confidence: 0.0
storage:
  database_url: "sqlite:///:memory:"
""")
        config = CrawlerConfig(cfg_file)

    pipeline = CrawlPipeline(db_session, config, "TestBot/1.0")
    pipeline.robots_mgr.is_allowed = lambda url: True

    article_html = """<html><head><meta property="article:published_time" content="2023-01-01T12:00:00Z"></head><body>
    <article><p>Pumunta ang pangulo sa lalawigan kaninang umaga para magbigay ng tulong sa mga nasalanta.</p></article>
    </body></html>"""

    # 1. First crawl: 202 returns 202, 403-a & 403-b return 403
    async def mock_fetch_1(url):
        if "retrying-202" in url:
            return FetchResult(status=202, body="", cached=False, retry_after=0.01, error_category="HTTP_TRANSIENT")
        if "blocked-403" in url:
            return FetchResult(status=403, body="", cached=False, error_category="FORBIDDEN")
        return FetchResult(status=200, body=article_html, cached=False)

    pipeline.fetcher.fetch = mock_fetch_1
    await pipeline.crawl_queued_urls()

    db_session.refresh(url_202)
    db_session.refresh(url_403_a)
    db_session.refresh(url_403_b)
    db_session.refresh(source)
    assert url_202.status == URLStatus.RETRY_WAIT
    assert url_202.retry_count == 1
    assert url_403_a.status == URLStatus.RETRY_WAIT
    assert url_403_a.retry_count == 1
    assert url_403_b.status == URLStatus.RETRY_WAIT
    assert url_403_b.retry_count == 1

    # Source should now be in COOLDOWN because 2 403s met the trigger_threshold=2
    assert source.status == "COOLDOWN"
    assert source.cooldown_until is not None

    # Insert another URL while source is in COOLDOWN
    url_other = URL(url_id=4, source_id="http_test", url="https://example.com/2023/01/01/other-article", status=URLStatus.DISCOVERED)
    db_session.add(url_other)
    db_session.commit()

    # Attempt to crawl during active cooldown: url_other should be excluded
    await pipeline.crawl_queued_urls()
    db_session.refresh(url_other)
    assert url_other.status == URLStatus.DISCOVERED

    # 2. Advance time past cooldown and past retry wait
    url_202.next_retry_at = datetime.utcnow() - timedelta(seconds=1)
    url_403_a.next_retry_at = datetime.utcnow() - timedelta(seconds=1)
    url_403_b.next_retry_at = datetime.utcnow() - timedelta(seconds=1)
    source.cooldown_until = datetime.utcnow() - timedelta(seconds=1)
    db_session.commit()

    article_html = """<html><head><meta property="article:published_time" content="2023-01-01T12:00:00Z"></head><body>
    <article><p>Pumunta ang pangulo sa lalawigan kaninang umaga para magbigay ng tulong sa mga nasalanta.</p></article>
    </body></html>"""

    async def mock_fetch_2(url):
        if "retrying-202" in url:
            return FetchResult(status=200, body=article_html, cached=False)
        if "blocked-403" in url:
            return FetchResult(status=403, body="", cached=False, error_category="FORBIDDEN")
        return FetchResult(status=200, body=article_html, cached=False)

    pipeline.fetcher.fetch = mock_fetch_2
    await pipeline.crawl_queued_urls()

    db_session.refresh(url_202)
    db_session.refresh(url_403_a)
    db_session.refresh(url_403_b)
    db_session.refresh(url_other)
    db_session.refresh(source)

    # 202 succeeds, 403-a and 403-b transition to BLOCKED (max_403_retries=1 exhausted), url_other is now crawled and ACCEPTED
    assert url_202.status == URLStatus.ACCEPTED
    assert url_202.sentence_count >= 1
    assert url_403_a.status == URLStatus.BLOCKED
    assert url_403_b.status == URLStatus.BLOCKED
    assert url_other.status == URLStatus.ACCEPTED
    assert source.status == "ACTIVE" or source.status == "COOLDOWN"


# Scenario 5: Missing HTML date uses trusted fallback while lastmod cannot override it
@pytest.mark.asyncio
async def test_date_hint_fallback_and_lastmod_isolation(db_session, monkeypatch):
    source = Source(source_id="date_src", name="Date Test", domain="example.com", enabled=True)
    url_rec = URL(
        url_id=10,
        source_id="date_src",
        url="https://example.com/2023/01/01/date-test-art",
        status=URLStatus.DISCOVERED,
        publication_date_hint=datetime(2023, 1, 1, 10, 0, 0),
        date_hint_source="rss",
        date_hint_confidence=0.85,
        sitemap_lastmod_hint=datetime(2024, 5, 1, 10, 0, 0),
    )
    db_session.add_all([source, url_rec])
    db_session.commit()

    class FakeConfig:
        config = {
            "crawler": {"date_cutoff": "2022-01-01", "queue_batch_size": 10, "processing_timeout_seconds": 60},
            "http": {"timeout_seconds": 1, "max_retries": 1},
            "rate_limiting": {"default_delay_seconds": 0, "default_max_concurrent": 1},
            "language": {"min_confidence": 0.5, "classify_mixed": True},
            "sentence": {"min_tokens": 3, "max_tokens": 100, "include_quotes": False},
            "extraction": {"min_body_chars": 10},
        }
        def get(self, key, default=None):
            val = self.config
            for part in key.split("."):
                if not isinstance(val, dict) or part not in val:
                    return default
                val = val[part]
            return val

    pipeline = CrawlPipeline(db_session, FakeConfig(), "TestBot/1.0")
    pipeline.robots_mgr.is_allowed = lambda url: True

    html_without_date = """<html><body>
    <article><p>Nagsagawa ng malawakang operasyon ang kapulisan sa iba't ibang bahagi ng lungsod kahapon.</p></article>
    </body></html>"""

    pipeline.fetcher.fetch = AsyncMock(return_value=FetchResult(status=200, body=html_without_date, cached=False))
    await pipeline.crawl_queued_urls()

    db_session.refresh(url_rec)
    assert url_rec.status == URLStatus.ACCEPTED
    art = db_session.query(Article).filter(Article.url_id == 10).one()
    assert art.publication_date == datetime(2023, 1, 1, 10, 0, 0)
    assert art.date_source == "rss"
    assert art.modified_date == datetime(2024, 5, 1, 10, 0, 0)


# Scenario 9: Truncation telemetry
def test_budget_exhaustion_emits_truncation(db_session, monkeypatch):
    source_cfg = SourceConfig(
        id="trunc_src",
        name="Trunc Test",
        domain="example.com",
        enabled=True,
        sitemap=[SitemapConfig(url="https://example.com/sitemap.xml")],
    )

    sitemap_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/a1</loc></url>
  <url><loc>https://example.com/a2</loc></url>
  <url><loc>https://example.com/a3</loc></url>
  <url><loc>https://example.com/a4</loc></url>
</urlset>"""

    def mock_get(url, **kwargs):
        return _MockResponse("", content=sitemap_xml)

    monkeypatch.setattr("src.crawler.discovery.httpx.get", mock_get)

    emitted_events = []
    def record_event(event_type, msg=None, status=None):
        emitted_events.append((event_type, msg, status))

    engine = DiscoveryEngine(
        db_session=db_session,
        user_agent="TestBot/1.0",
        date_cutoff=datetime(2020, 1, 1),
        max_candidates=2,  # Limit to 2 candidates
        event_logger=record_event,
    )

    report = engine.discover_source(source_cfg)
    assert report["truncated"] is True
    assert report["candidates_added"] == 2
    assert len(report["truncation_details"]) >= 1
    assert report["truncation_details"][0]["dimension"] == "candidates"
    assert report["truncation_details"][0]["limit"] == 2
    assert report["truncation_details"][0]["count"] == 2

    # Assert observable discovery:truncated event
    assert any(
        et == "discovery:truncated" and "dimension=candidates" in msg and "limit=2" in msg
        for et, msg, _ in emitted_events
    )


# Scenario 10: Request logs prove robots, delay, and concurrency compliance
@pytest.mark.asyncio
async def test_combined_compliance_trace(db_session, monkeypatch):
    """Scenario 10: Prove robots checked before fetch, disallowed never fetched, stricter delay, concurrency limit."""
    source_cfg = SourceConfig(
        id="compliance_src",
        name="Compliance Test",
        domain="example.com",
        enabled=True,
        crawl_delay_seconds=0.04,  # Configured delay 40ms
        max_concurrent=1,          # Bound 1 per domain
    )
    db_session.add(Source(source_id="compliance_src", name="Compliance Test", domain="example.com", enabled=True, language="filipino"))

    url_allowed_1 = URL(url_id=1, source_id="compliance_src", url="https://example.com/art1", status=URLStatus.DISCOVERED)
    url_disallowed = URL(url_id=2, source_id="compliance_src", url="https://example.com/admin-disallowed", status=URLStatus.DISCOVERED)
    url_allowed_2 = URL(url_id=3, source_id="compliance_src", url="https://example.com/art2", status=URLStatus.DISCOVERED)
    db_session.add_all([url_allowed_1, url_disallowed, url_allowed_2])
    db_session.commit()

    with tempfile.TemporaryDirectory() as td:
        cfg_file = Path(td) / "crawler.yaml"
        cfg_file.write_text("""crawler:
  version: "1.0.0"
  date_cutoff: "2020-01-01"
  queue_batch_size: 10
  processing_timeout_seconds: 60
rate_limiting:
  default_delay_seconds: 0.01
  default_max_concurrent: 1
sentence:
  min_tokens: 3
  max_tokens: 100
  include_quotes: true
language:
  min_confidence: 0.0
storage:
  database_url: "sqlite:///:memory:"
""")
        config = CrawlerConfig(cfg_file)

    pipeline = CrawlPipeline(db_session, config, "TestBot/1.0")
    pipeline._source_config = lambda sid: source_cfg

    # Trace log of all actions in exact execution order
    action_trace = []
    active_fetches = 0
    max_observed_concurrency = 0

    def mock_is_allowed(url, **kwargs):
        action_trace.append(("robots_check", url))
        return "admin" not in url

    async def mock_is_allowed_async(url, **kwargs):
        action_trace.append(("robots_check", url))
        return "admin" not in url

    # Robots crawl-delay of 0.05s (stricter than configured 0.04s)
    pipeline.robots_mgr.is_allowed = mock_is_allowed
    pipeline.robots_mgr.is_allowed_async = mock_is_allowed_async
    pipeline.robots_mgr.get_crawl_delay = lambda host: 0.05
    async def mock_get_crawl_delay_async(host, **kwargs):
        return 0.05
    pipeline.robots_mgr.get_crawl_delay_async = mock_get_crawl_delay_async

    article_html = """<html><head><meta property="article:published_time" content="2023-01-01T12:00:00Z"></head><body>
    <article><p>Pumunta ang pangulo sa lalawigan kaninang umaga para magbigay ng tulong sa mga nasalanta.</p></article>
    </body></html>"""

    fetch_start_times = []

    async def mock_fetch(url):
        nonlocal active_fetches, max_observed_concurrency
        active_fetches += 1
        max_observed_concurrency = max(max_observed_concurrency, active_fetches)
        fetch_start_times.append(asyncio.get_event_loop().time())
        action_trace.append(("http_fetch", url))
        await asyncio.sleep(0.01)  # small simulated fetch latency
        active_fetches -= 1
        return FetchResult(status=200, body=article_html, cached=False)

    pipeline.fetcher.fetch = mock_fetch

    await pipeline.crawl_queued_urls()

    db_session.refresh(url_allowed_1)
    db_session.refresh(url_disallowed)
    db_session.refresh(url_allowed_2)

    # 1. Robots checked before every candidate URL
    robots_checked_urls = [url for action, url in action_trace if action == "robots_check"]
    assert "https://example.com/art1" in robots_checked_urls
    assert "https://example.com/admin-disallowed" in robots_checked_urls
    assert "https://example.com/art2" in robots_checked_urls

    # 2. Robots-disallowed URL is NEVER fetched
    http_fetched_urls = [url for action, url in action_trace if action == "http_fetch"]
    assert "https://example.com/admin-disallowed" not in http_fetched_urls
    assert url_disallowed.status == URLStatus.BLOCKED
    assert url_disallowed.failure_class == "robots_disallowed"

    # 3. Both allowed URLs were fetched and accepted
    assert url_allowed_1.status == URLStatus.ACCEPTED, f"status={url_allowed_1.status}, failure_class={url_allowed_1.failure_class}, error_reason={url_allowed_1.error_reason}"
    assert "https://example.com/art1" in http_fetched_urls
    assert "https://example.com/art2" in http_fetched_urls
    assert url_allowed_2.status == URLStatus.ACCEPTED

    # 4. For fetched URLs, robots check strictly precedes http fetch
    art1_check_idx = action_trace.index(("robots_check", "https://example.com/art1"))
    art1_fetch_idx = action_trace.index(("http_fetch", "https://example.com/art1"))
    assert art1_check_idx < art1_fetch_idx

    # 5. Effective delay is stricter of configured (0.04) and robots (0.05) -> >= 0.045s elapsed between starts
    assert len(fetch_start_times) == 2
    time_between_fetches = fetch_start_times[1] - fetch_start_times[0]
    assert time_between_fetches >= 0.045

    # 6. Active fetch concurrency for the domain never exceeded 1
    assert max_observed_concurrency == 1
