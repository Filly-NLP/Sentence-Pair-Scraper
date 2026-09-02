import asyncio
import json
from pathlib import Path

import httpx
import pytest
import src.crawler.link_discovery as link_discovery_module
from click.testing import CliRunner
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from src.cli.main import cli
from src.crawler.config import CrawlerConfig, CrawlerConfigValidationError
from src.crawler.fetcher import FetchResult, HTTPFetcher
from src.crawler.link_discovery import LinkCandidate, LinkDiscoveryBatch, LinkDiscoveryEngine
from src.crawler.pipeline import CrawlJob, CrawlOutcome, CrawlPipeline
from src.sources.registry import LinkDiscoveryConfig, SourceConfig
from src.storage.migrations import apply_additive_migrations
from src.storage.models import Base, CrawlRun, Source, URL, URLDiscoveryEdge


FIXTURE = Path("tests/fixtures/discovery/link_frontier.html")


def _source(enabled=True, priority=120):
    return SourceConfig(
        id="frontier_test",
        name="Frontier Test",
        domain="example.com",
        article_path_patterns=[r"^/article/\d+$"],
        link_discovery=LinkDiscoveryConfig(enabled=enabled, article_priority=priority),
    )


def test_stage4a_defaults_and_config_validation(tmp_path):
    crawler_path = tmp_path / "crawler.yaml"
    crawler_path.write_text(
        """link_discovery:\n  enabled: false\n  max_depth: 1\n  max_parent_pages_per_source_run: 25\n  max_candidates_per_source_run: 500\n  max_links_per_page: 100\n  allow_query_parameters: false\n""",
        encoding="utf-8",
    )
    config = CrawlerConfig(crawler_path)
    assert config.get("link_discovery.enabled") is False
    assert config.get("link_discovery.max_depth") == 1
    assert _source().link_discovery.article_priority == 120
    assert SourceConfig(id="default", name="Default", domain="example.com").link_discovery == LinkDiscoveryConfig()

    crawler_path.write_text("link_discovery:\n  max_depth: -1\n", encoding="utf-8")
    with pytest.raises(CrawlerConfigValidationError, match="link_discovery.max_depth"):
        CrawlerConfig(crawler_path)


def test_link_analyzer_is_redirect_aware_and_reconciled():
    source = _source()
    html = FIXTURE.read_text(encoding="utf-8")
    engine = LinkDiscoveryEngine()

    batch = engine.analyze_html(
        html,
        final_url="https://example.com/redirected/parent",
        requested_url="https://example.com/original/parent",
        source_config=source,
        parent_depth=0,
        explicit_limits={"max_depth": 1, "max_links_per_page": 100, "max_candidates": 10},
    )

    assert [item.normalized_url for item in batch.candidates] == [
        "https://example.com/article/1",
        "https://example.com/article/2",
    ]
    assert all(item.depth == 1 and item.priority == 120 for item in batch.candidates)
    assert batch.candidates[0].link_text == "Unang balita"
    assert batch.candidates[1].rel == "nofollow"
    assert batch.rejection_reasons == {
        "host_mismatch": 1,
        "non_article_asset": 1,
        "query_parameters_disallowed": 2,
        "self_link": 1,
        "unsupported_scheme": 1,
    }
    assert batch.counters["unique_normalized"] == 7
    assert batch.counters["duplicates"] == 1
    assert batch.counters["accepted_unique"] + batch.counters["rejected_unique"] == 7

    repeated = engine.analyze_html(
        html,
        final_url="https://example.com/redirected/parent",
        requested_url="https://example.com/original/parent",
        source_config=source,
        parent_depth=0,
        explicit_limits={"max_depth": 1, "max_links_per_page": 100, "max_candidates": 10},
    )
    assert repeated.to_dict() == batch.to_dict()


def test_link_analyzer_enforces_independent_page_candidate_and_depth_limits():
    source = _source()
    html = "".join(f'<a href="/article/{i}">article {i}</a>' for i in range(5))
    engine = LinkDiscoveryEngine()

    limited = engine.analyze_html(
        html,
        final_url="https://example.com/parent",
        source_config=source,
        parent_depth=0,
        max_depth=1,
        max_links_per_page=3,
        max_candidates=1,
    )
    assert len(limited.candidates) == 1
    assert limited.counters["accepted_unique"] == 3
    assert limited.counters["links_skipped_by_page_limit"] == 2
    assert {item["dimension"] for item in limited.truncation_details} == {"links_per_page", "candidates"}

    too_deep = engine.analyze_html(
        html,
        final_url="https://example.com/parent",
        source_config=source,
        parent_depth=1,
        max_depth=1,
    )
    assert too_deep.candidates == ()
    assert too_deep.truncation_details[0]["dimension"] == "depth"
    assert too_deep.counters["unique_normalized"] == 0
    assert too_deep.counters["accepted_unique"] == 0
    assert too_deep.counters["rejected_unique"] == 0
    assert too_deep.counters["candidates_emitted"] == 0
    assert not too_deep.rejection_reasons


def test_link_analyzer_reconciles_parse_error_and_redirected_parent_self_links(monkeypatch):
    source = _source()
    engine = LinkDiscoveryEngine()
    html = (
        '<a href="https://example.com/article/10">requested</a>'
        '<a href="/article/11">final</a>'
        '<a href="/article/12">accepted</a>'
    )
    batch = engine.analyze_html(
        html,
        final_url="https://example.com/article/11",
        requested_url="https://example.com/article/10",
        source_config=source,
        explicit_limits={"max_depth": 1},
    )
    assert [candidate.normalized_url for candidate in batch.candidates] == [
        "https://example.com/article/12",
    ]
    assert batch.rejection_reasons["self_link"] == 2
    assert batch.counters["unique_normalized"] == 3
    assert batch.counters["accepted_unique"] + batch.counters["rejected_unique"] == 3

    monkeypatch.setattr(
        link_discovery_module,
        "BeautifulSoup",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("bad html")),
    )
    parse_error = engine.analyze_html(
        "<a href='/article/1'>one</a>",
        final_url="https://example.com/article/10",
        source_config=source,
    )
    assert parse_error.counters["parse_errors"] == 1
    assert parse_error.counters["unique_normalized"] == 0
    assert parse_error.counters["accepted_unique"] == 0
    assert parse_error.counters["rejected_unique"] == 0
    assert parse_error.counters["candidates_emitted"] == 0
    assert not parse_error.rejection_reasons


def test_pipeline_requires_global_and_source_gates():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        pipeline = CrawlPipeline(session, _FrontierConfig(), "test-agent")
        assert pipeline._link_discovery_gate(_source(enabled=True), 0)
        assert not pipeline._link_discovery_gate(_source(enabled=False), 0)
        pipeline.config.config["link_discovery"]["enabled"] = False
        assert not pipeline._link_discovery_gate(_source(enabled=True), 0)
    finally:
        session.close()


@pytest.mark.asyncio
async def test_link_analysis_failure_does_not_fail_parent_extraction(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        pipeline = CrawlPipeline(session, _FrontierConfig(), "test-agent")
        pipeline.fetcher = _FrontierFetcher('<a href="/article/1">One</a>')
        pipeline.robots_mgr.is_allowed = lambda url: True
        pipeline.robots_mgr.get_crawl_delay = lambda host: 0
        pipeline.link_discovery_engine.analyze_html = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("bad html"))
        monkeypatch.setattr(
            "src.crawler.pipeline.ArticleExtractor.extract",
            lambda html, source: {
                "headline": "Balita", "author": "", "publication_date_raw": "2026-08-11",
                "publication_date_source": "meta", "canonical_url": "",
                "article_text": "Magandang balita para sa buong bayan. " * 2,
            },
        )
        job = CrawlJob(
            url_id=1, url="https://example.com/parent", source_id="frontier_test",
            domain="example.com", retry_count=0, configured_delay=0, max_concurrent=1,
            link_discovery_enabled=True, discovery_depth=0,
            link_discovery_limits={"max_depth": 1, "max_links_per_page": 100, "max_candidates": 100},
        )
        outcome = await pipeline._worker_process_job(job, _source())
        assert outcome.error is None
        assert outcome.extracted is not None
        assert outcome.link_discovery_batch is None
        assert outcome.link_discovery_error == "bad html"
    finally:
        session.close()


def test_frontier_schema_migration_is_additive_and_idempotent():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE sources (source_id VARCHAR(50) PRIMARY KEY, name VARCHAR(100), domain VARCHAR(100), enabled BOOLEAN, status VARCHAR(20))"))
        connection.execute(text("CREATE TABLE urls (url_id INTEGER PRIMARY KEY, url TEXT NOT NULL, status VARCHAR(20) DEFAULT 'DISCOVERED', next_retry_at DATETIME, source_id VARCHAR(50))"))
        connection.execute(text("CREATE TABLE crawl_runs (crawl_id VARCHAR(50) PRIMARY KEY)"))

    first = apply_additive_migrations(engine)
    second = apply_additive_migrations(engine)
    assert "urls.discovery_depth" in first["columns_added"]
    assert "urls.frontier_priority" in first["columns_added"]
    assert "url_discovery_edges" in first["tables_created"]
    assert second["columns_added"] == []
    assert second["tables_created"] == []
    assert second["indexes_created"] == []
    assert {column["name"] for column in inspect(engine).get_columns("urls")} >= {
        "discovery_depth", "frontier_priority",
    }
    assert {column["name"] for column in inspect(engine).get_columns("url_discovery_edges")} >= {
        "from_url_id", "to_url_id", "crawl_id", "link_text", "rel",
    }


@pytest.mark.asyncio
async def test_fetch_result_retains_response_url_for_success_cache_and_error():
    class _Client:
        def __init__(self, response):
            self.response = response

        async def get(self, url, headers=None):
            return self.response

    requested = "https://example.com/redirected"
    successful_response = httpx.Response(
        200,
        text="<html></html>",
        request=httpx.Request("GET", requested),
    )
    fetcher = HTTPFetcher("test-agent", max_retries=1)
    success = await fetcher._fetch_with_client(_Client(successful_response), "https://example.com/original")
    assert success.final_url == requested

    cached_response = httpx.Response(
        304,
        request=httpx.Request("GET", requested),
    )
    fetcher.cache = type("_Cache", (), {
        "get": lambda self, url: {"meta": {}, "html": "cached"},
        "set": lambda self, url, html, headers: None,
    })()
    cached = await fetcher._fetch_with_client(_Client(cached_response), "https://example.com/original")
    assert cached.cached is True
    assert cached.final_url == requested

    error_response = httpx.Response(
        404,
        request=httpx.Request("GET", requested),
    )
    fetcher.cache = None
    error = await fetcher._fetch_with_client(_Client(error_response), "https://example.com/original")
    assert error.final_url == requested


@pytest.mark.asyncio
async def test_enabled_frontier_claims_depth_then_priority_then_url(db_session):
    source_cfg = _source()
    db_session.add(Source(source_id=source_cfg.id, name=source_cfg.name, domain=source_cfg.domain, enabled=True))
    db_session.add_all([
        URL(url="https://example.com/depth-one", source_id=source_cfg.id, discovery_depth=1, frontier_priority=999),
        URL(url="https://example.com/depth-zero-low", source_id=source_cfg.id, discovery_depth=0, frontier_priority=1),
        URL(url="https://example.com/depth-zero-high", source_id=source_cfg.id, discovery_depth=0, frontier_priority=2),
    ])
    db_session.commit()
    config = _FrontierConfig()
    config.config["crawler"]["queue_batch_size"] = 1
    pipeline = CrawlPipeline(db_session, config, "test-agent")
    pipeline._source_config = lambda source_id: source_cfg
    pipeline.fetcher = type("_Lifecycle", (), {
        "start": lambda self: None,
        "close": lambda self: None,
    })()
    order = []

    async def fake_worker(job, source):
        order.append(job.url)
        return CrawlOutcome(
            job=job,
            fetch_result=FetchResult(status=404, error="HTTP_404", error_category="HTTP_ERROR"),
        )

    pipeline._worker_process_job = fake_worker
    await pipeline.crawl_queued_urls()
    assert order == [
        "https://example.com/depth-zero-high",
        "https://example.com/depth-zero-low",
        "https://example.com/depth-one",
    ]


class _FrontierConfig:
    config = {
        "crawler": {"date_cutoff": "2022-01-01", "queue_batch_size": 10, "processing_timeout_seconds": 60},
        "http": {"timeout_seconds": 1, "max_retries": 1},
        "rate_limiting": {"default_delay_seconds": 0, "default_max_concurrent": 1, "backoff": {}},
        "cache": {"http_cache_dir": "data/test-cache", "robots_ttl_hours": 24},
        "language": {"min_confidence": 0.0, "classify_mixed": True},
        "sentence": {"min_tokens": 3, "max_tokens": 100, "include_quotes": True},
        "extraction": {"min_body_chars": 10},
        "link_discovery": {
            "enabled": True,
            "max_depth": 1,
            "max_parent_pages_per_source_run": 25,
            "max_candidates_per_source_run": 500,
            "max_links_per_page": 100,
            "allow_query_parameters": False,
        },
    }

    def __init__(self):
        self.config = json.loads(json.dumps(type(self).config))

    def get(self, key, default=None):
        value = self.config
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value


class _FrontierFetcher:
    def __init__(self, html):
        self.html = html
        self.urls = []

    async def start(self):
        return None

    async def close(self):
        return None

    async def fetch(self, url):
        self.urls.append(url)
        return FetchResult(
            status=200,
            body=self.html,
            headers={"content-type": "text/html"},
            final_url=url,
        )


class _FrontierDetector:
    def detect_sentence_language(self, sentence):
        return "FILIPINO", 1.0


@pytest.mark.asyncio
async def test_pipeline_persists_multiple_edges_without_overwriting_method(db_session, monkeypatch):
    source_cfg = _source()
    db_session.add(Source(source_id=source_cfg.id, name=source_cfg.name, domain=source_cfg.domain, enabled=True))
    db_session.add_all([
        URL(url="https://example.com/parent-a", source_id=source_cfg.id, discovery_method="RSS", frontier_priority=10),
        URL(url="https://example.com/parent-b", source_id=source_cfg.id, discovery_method="SITEMAP", frontier_priority=9),
        URL(url="https://example.com/article/2", source_id=source_cfg.id, discovery_method="RSS", discovery_depth=1, frontier_priority=1),
    ])
    db_session.commit()

    html = '<a href="/article/2?utm_source=feed">Shared article</a><a href="/article/3">New article</a>'
    pipeline = CrawlPipeline(db_session, _FrontierConfig(), "test-agent")
    pipeline.fetcher = _FrontierFetcher(html)
    pipeline.lang_detector = _FrontierDetector()
    pipeline._source_config = lambda source_id: source_cfg
    pipeline.robots_mgr.is_allowed = lambda url: True
    pipeline.robots_mgr.get_crawl_delay = lambda host: 0
    monkeypatch.setattr(
        "src.crawler.pipeline.ArticleExtractor.extract",
        lambda html, source: {
            "headline": "Balita", "author": "", "publication_date_raw": "2026-08-11",
            "publication_date_source": "meta", "canonical_url": "",
            "article_text": "Magandang balita para sa buong bayan. " * 2,
        },
    )

    await pipeline.crawl_queued_urls()

    child = db_session.query(URL).filter(URL.url == "https://example.com/article/2").one()
    assert child.discovery_method == "RSS"
    assert child.frontier_priority == 120
    new_child = db_session.query(URL).filter(URL.url == "https://example.com/article/3").one()
    assert new_child.discovery_method == "LINK"
    assert new_child.status == "ACCEPTED"
    assert new_child.discovery_depth == 1
    assert new_child.frontier_priority == 120
    assert new_child.publication_date_hint is None
    assert db_session.query(URLDiscoveryEdge).count() == 4
    assert db_session.query(URLDiscoveryEdge).filter(URLDiscoveryEdge.to_url_id == child.url_id).count() == 2
    assert db_session.query(URL).count() == 4

    # Accepted rows are not requeued; rerunning is therefore idempotent.
    await pipeline.crawl_queued_urls()
    assert db_session.query(URLDiscoveryEdge).count() == 4


def test_offline_link_audit_uses_no_database_or_http(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "crawler.yaml").write_text(
        """crawler:\n  date_cutoff: '2022-01-01'\nlink_discovery:\n  enabled: false\n  max_depth: 1\n  max_parent_pages_per_source_run: 25\n  max_candidates_per_source_run: 500\n  max_links_per_page: 100\n  allow_query_parameters: false\n""",
        encoding="utf-8",
    )
    (config_dir / "sources.yaml").write_text(
        """sources:\n  - id: frontier_test\n    name: Frontier Test\n    domain: example.com\n    enabled: true\n    article_path_patterns: ['^/article/\\d+$']\n""",
        encoding="utf-8",
    )
    monkeypatch.setattr("src.cli.main.DatabaseManager", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("DB must not be opened")))

    result = CliRunner().invoke(cli, [
        "--config-dir", str(config_dir),
        "audit-link-frontier",
        "--source", "frontier_test",
        "--html-file", str(FIXTURE),
        "--base-url", "https://example.com/redirected/parent",
    ])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["runtime_enablement"]["runtime_enabled"] is False
    assert payload["runtime_enablement"]["http_requests"] is False
    assert payload["runtime_enablement"]["database_access"] is False
    assert [candidate["normalized_url"] for candidate in payload["report"]["candidates"]] == [
        "https://example.com/article/1", "https://example.com/article/2",
    ]


@pytest.mark.asyncio
async def test_cancellation_requeues_claimed_frontier_rows(db_session):
    source_cfg = _source(enabled=False)
    db_session.add(Source(source_id=source_cfg.id, name=source_cfg.name, domain=source_cfg.domain, enabled=True))
    record = URL(url="https://example.com/article/99", source_id=source_cfg.id)
    db_session.add(record)
    db_session.commit()

    class _Lifecycle:
        async def start(self):
            return None

        async def close(self):
            return None

    pipeline = CrawlPipeline(db_session, _FrontierConfig(), "test-agent")
    pipeline.fetcher = _Lifecycle()
    pipeline._source_config = lambda source_id: source_cfg
    halted = asyncio.Event()

    async def blocked_worker(job, source):
        await halted.wait()
        return CrawlOutcome(job=job, fetch_result=FetchResult(status=404))

    pipeline._worker_process_job = blocked_worker
    crawl_task = asyncio.create_task(pipeline.crawl_queued_urls())
    for _ in range(100):
        await asyncio.sleep(0)
        db_session.expire_all()
        if db_session.query(URL).filter(URL.url_id == record.url_id).one().status == "PROCESSING":
            break
    else:
        crawl_task.cancel()
        await crawl_task

    crawl_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await crawl_task

    db_session.expire_all()
    refreshed = db_session.query(URL).filter(URL.url_id == record.url_id).one()
    assert refreshed.status == "RETRY_WAIT"
    assert refreshed.retry_count == 1
    assert refreshed.processing_started_at is None
    assert refreshed.error_reason == "requeued_after_cancellation"


@pytest.mark.asyncio
async def test_out_of_scope_redirect_is_rejected_before_article_processing(db_session, monkeypatch):
    source_cfg = _source(enabled=False)

    class _RedirectFetcher:
        async def fetch(self, url):
            return FetchResult(
                status=200,
                html="<html><body>ignored</body></html>",
                headers={"content-type": "text/html"},
                final_url="https://other.example/article/1",
            )

    pipeline = CrawlPipeline(db_session, _FrontierConfig(), "test-agent")
    pipeline.fetcher = _RedirectFetcher()
    pipeline.robots_mgr.is_allowed = lambda url: True
    job = CrawlJob(
        url_id=1,
        url="https://example.com/article/10",
        source_id=source_cfg.id,
        domain="example.com",
        retry_count=0,
        configured_delay=0,
        max_concurrent=1,
    )
    monkeypatch.setattr(
        "src.crawler.pipeline.ArticleExtractor.extract",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("redirect must be rejected first")),
    )

    outcome = await pipeline._worker_process_job(job, source_cfg)
    assert outcome.failure_class == "redirect_out_of_scope"
    assert outcome.error == "redirect_target_out_of_scope:host_mismatch"
