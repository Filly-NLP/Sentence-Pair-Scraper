import asyncio
from contextlib import contextmanager
from pathlib import Path

import pytest
from click.testing import CliRunner

from src.cli.main import cli
from src.crawler.fetcher import FetchResult
from src.crawler.link_discovery import LinkCandidate, LinkDiscoveryBatch
from src.crawler.pipeline import CrawlOutcome, CrawlPipeline
from src.sources.registry import LinkDiscoveryConfig, SourceConfig, SourceRegistry
from src.storage.models import Source, URL, URLStatus


class _Config:
    config = {
        "crawler": {
            "date_cutoff": "2022-01-01",
            "queue_batch_size": 100,
            "processing_timeout_seconds": 60,
        },
        "http": {"timeout_seconds": 1, "max_retries": 1},
        "rate_limiting": {
            "default_delay_seconds": 0,
            "default_max_concurrent": 1,
            "backoff": {},
        },
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
        self.config = {
            key: dict(value) if isinstance(value, dict) else value
            for key, value in type(self).config.items()
        }

    def get(self, key, default=None):
        value = self.config
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value


class _NoopFetcher:
    async def start(self):
        return None

    async def close(self):
        return None


def _source(*, link_enabled=False):
    return SourceConfig(
        id="stage9",
        name="Stage 9",
        domain="example.com",
        enabled=True,
        language="filipino",
        crawl_delay_seconds=0,
        article_path_patterns=[r"^/article/\d+$"],
        link_discovery=LinkDiscoveryConfig(enabled=link_enabled),
    )


@pytest.mark.asyncio
async def test_limit_is_global_and_leaves_link_frontier_rows_resumable(db_session):
    source_cfg = _source(link_enabled=True)
    db_session.add(Source(
        source_id=source_cfg.id,
        name=source_cfg.name,
        domain=source_cfg.domain,
        enabled=True,
        language=source_cfg.language,
    ))
    db_session.add(URL(
        url="https://example.com/article/0",
        source_id=source_cfg.id,
        status=URLStatus.DISCOVERED,
        discovery_method="ARCHIVE",
    ))
    db_session.commit()

    pipeline = CrawlPipeline(db_session, _Config(), "test-agent")
    pipeline.fetcher = _NoopFetcher()
    pipeline._source_config = lambda _source_id: source_cfg
    seen = []

    async def worker(job, _source):
        seen.append(job.url)
        if job.url.endswith("/0"):
            candidates = tuple(
                LinkCandidate(
                    raw_url=f"https://example.com/article/{number}",
                    normalized_url=f"https://example.com/article/{number}",
                    depth=1,
                    priority=100,
                )
                for number in (1, 2)
            )
            return CrawlOutcome(
                job=job,
                fetch_result=FetchResult(status=200),
                link_discovery_batch=LinkDiscoveryBatch(candidates=candidates),
            )
        return CrawlOutcome(
            job=job,
            fetch_result=FetchResult(status=404, error="HTTP_404", error_category="HTTP_ERROR"),
        )

    pipeline._worker_process_job = worker
    await pipeline.crawl_queued_urls(limit=2)

    assert len(seen) == 2
    assert seen == [
        "https://example.com/article/0",
        "https://example.com/article/1",
    ]
    assert db_session.query(URL).filter(URL.url == "https://example.com/article/2").one().status == URLStatus.DISCOVERED
    assert db_session.query(URL).filter(URL.status == URLStatus.PROCESSING).count() == 0


@pytest.mark.asyncio
async def test_discovery_method_filter_only_claims_selected_provenance(db_session):
    source_cfg = _source()
    db_session.add(Source(
        source_id=source_cfg.id,
        name=source_cfg.name,
        domain=source_cfg.domain,
        enabled=True,
    ))
    methods = ("RSS", "SITEMAP", "ARCHIVE", "LINK", "COMMON_CRAWL")
    db_session.add_all([
        URL(
            url=f"https://example.com/article/{index}",
            source_id=source_cfg.id,
            status=URLStatus.DISCOVERED,
            discovery_method=method,
        )
        for index, method in enumerate(methods)
    ])
    db_session.commit()

    pipeline = CrawlPipeline(db_session, _Config(), "test-agent")
    pipeline.fetcher = _NoopFetcher()
    pipeline._source_config = lambda _source_id: source_cfg
    seen = []

    async def worker(job, _source):
        seen.append(job.url)
        return CrawlOutcome(
            job=job,
            fetch_result=FetchResult(status=404, error="HTTP_404", error_category="HTTP_ERROR"),
        )

    pipeline._worker_process_job = worker
    await pipeline.crawl_queued_urls(discovery_method="ARCHIVE")

    assert seen == ["https://example.com/article/2"]
    statuses = {
        record.discovery_method: record.status
        for record in db_session.query(URL).order_by(URL.url_id).all()
    }
    assert statuses["ARCHIVE"] == URLStatus.TERMINAL_FAILED
    assert all(status == URLStatus.DISCOVERED for method, status in statuses.items() if method != "ARCHIVE")


def test_injected_registry_is_authoritative_for_pipeline_source_selection(tmp_path, monkeypatch, db_session):
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text(
        """sources:
  - id: stage9
    name: Isolated Source
    domain: isolated.example
    enabled: true
    crawl_delay_seconds: 17
    article_path_patterns: ['^/isolated/\\d+$']
""",
        encoding="utf-8",
    )
    registry = SourceRegistry(sources_path)
    pipeline = CrawlPipeline(
        db_session,
        _Config(),
        "test-agent",
        source_registry=registry,
    )

    def fail_canonical_reload(_path):
        raise AssertionError("injected registry must prevent canonical reload")

    monkeypatch.setattr("src.crawler.pipeline.SourceRegistry", fail_canonical_reload)
    selected = pipeline._source_config("stage9")

    assert selected.name == "Isolated Source"
    assert selected.domain == "isolated.example"
    assert selected.crawl_delay_seconds == 17
    assert selected.article_path_patterns == [r"^/isolated/\d+$"]


def test_config_dir_passes_its_registry_to_crawl_pipeline(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "crawler.yaml").write_text(
        "crawler:\n  date_cutoff: '2022-01-01'\nstorage:\n  database_url: 'sqlite:///:memory:'\n",
        encoding="utf-8",
    )
    (config_dir / "sources.yaml").write_text(
        "sources:\n  - id: isolated\n    name: Isolated\n    domain: isolated.example\n",
        encoding="utf-8",
    )

    class _Query:
        def filter(self, *args, **kwargs):
            return self

        def count(self):
            return 0

    class _Session:
        def query(self, *args, **kwargs):
            return _Query()

    class _DatabaseManager:
        def __init__(self, _url):
            pass

        def init_db(self):
            pass

        @contextmanager
        def get_session(self):
            yield _Session()

    captured = {}

    class _Pipeline(CrawlPipeline):
        def __init__(self, *args, **kwargs):
            captured["registry"] = kwargs.get("source_registry")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("src.cli.main.DatabaseManager", _DatabaseManager)
    monkeypatch.setattr("src.cli.main.CrawlPipeline", _Pipeline)
    result = CliRunner().invoke(cli, [
        "--config-dir", str(config_dir),
        "crawl",
        "--limit", "1",
        "--discovery-method", "ARCHIVE",
    ])

    assert result.exit_code == 0, result.output
    assert captured["registry"].get_source("isolated").domain == "isolated.example"


@pytest.mark.asyncio
async def test_cancellation_requeues_all_claimed_rows_without_processing_stragglers(db_session):
    source_cfg = _source()
    db_session.add(Source(
        source_id=source_cfg.id,
        name=source_cfg.name,
        domain=source_cfg.domain,
        enabled=True,
    ))
    db_session.add_all([
        URL(
            url=f"https://example.com/article/{index}",
            source_id=source_cfg.id,
            status=URLStatus.DISCOVERED,
        )
        for index in (10, 11)
    ])
    db_session.commit()

    pipeline = CrawlPipeline(db_session, _Config(), "test-agent")
    pipeline.fetcher = _NoopFetcher()
    pipeline._source_config = lambda _source_id: source_cfg
    halted = asyncio.Event()

    async def blocked_worker(_job, _source):
        await halted.wait()

    pipeline._worker_process_job = blocked_worker
    crawl_task = asyncio.create_task(pipeline.crawl_queued_urls(limit=1))
    for _ in range(100):
        await asyncio.sleep(0)
        db_session.expire_all()
        if db_session.query(URL).filter(URL.status == URLStatus.PROCESSING).count() == 1:
            break
    else:
        crawl_task.cancel()
        await crawl_task

    crawl_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await crawl_task

    db_session.expire_all()
    rows = db_session.query(URL).order_by(URL.url_id).all()
    assert all(row.status != URLStatus.PROCESSING for row in rows)
    assert sum(row.status == URLStatus.RETRY_WAIT for row in rows) == 1
    assert all(row.processing_started_at is None for row in rows)
