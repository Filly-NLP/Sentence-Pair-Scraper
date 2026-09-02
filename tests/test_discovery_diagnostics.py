import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.crawler.discovery import DiscoveryEngine
from src.crawler.discovery_reporting import DiscoveryDiagnosticsStore
from src.crawler.config import CrawlerConfig
from src.crawler.pipeline import CrawlPipeline
from src.sources.registry import RSSFeedConfig, SitemapConfig, SourceConfig
from src.storage.database import DatabaseManager
from src.storage.models import Base, CrawlRun, DiscoveryObservation, DiscoveryObservationSample, Source, URL


class _Response:
    status_code = 200
    headers = {"Content-Type": "application/xml"}

    def __init__(self, content: bytes):
        self.content = content
        self.text = content.decode("utf-8", errors="ignore")


def test_audit_persist_queue_false_does_not_call_session_writes(monkeypatch, db_session):
    xml = b"<urlset><url><loc>https://example.com/article-1</loc></url></urlset>"
    monkeypatch.setattr("src.crawler.discovery.httpx.get", lambda url, **kwargs: _Response(xml))
    for name in ("add", "flush", "commit", "rollback"):
        monkeypatch.setattr(db_session, name, lambda *args, _name=name, **kwargs: pytest.fail(_name))
    source = SourceConfig(
        id="audit",
        name="Audit",
        domain="example.com",
        article_path_patterns=[r"^/article-\d+$"],
        sitemap=[SitemapConfig(url="https://example.com/sitemap.xml")],
    )
    report = DiscoveryEngine(
        db_session,
        "OfflineTestBot/1.0",
        datetime(2022, 1, 1),
        persist_queue=False,
    ).audit_source(source)
    assert report["would_queue"] == 1
    assert report["actually_queued"] == 0
    with db_session.no_autoflush:
        assert db_session.query(URL).count() == 0


def test_diagnostics_are_aggregated_and_samples_capped(db_session):
    source = Source(source_id="diag", name="Diagnostics", domain="example.com", enabled=True, language="filipino")
    run = CrawlRun(
        crawl_id="CRAWL_DIAG",
        source_id="diag",
        mode="discover",
        crawler_version="test",
        config_hash="x",
        end_time=datetime.utcnow(),
    )
    db_session.add_all([source, run])
    db_session.flush()
    from src.crawler.discovery_reporting import BoundedDiscoveryCollector, DiscoveryReport, DiscoveryOutcome, ScopeReason
    report = DiscoveryReport(source_id="diag", collector=BoundedDiscoveryCollector(sample_cap=2))
    for index in range(7):
        report.collector.record(
            DiscoveryOutcome.REJECTED_SCOPE,
            ScopeReason.PATH_PATTERN_MISMATCH,
            discovery_method="RSS",
            root_url="https://example.com/feed",
            candidate_url=f"https://example.com/no/{index}",
        )
    store = DiscoveryDiagnosticsStore(db_session, enabled=True, sample_cap=2, retention_runs=5)
    assert store.persist(report, crawl_id=run.crawl_id, source_id="diag") == 1
    observation = db_session.query(DiscoveryObservation).one()
    assert observation.observation_count == 7
    assert db_session.query(DiscoveryObservationSample).filter_by(observation_id=observation.observation_id).count() == 2


def test_diagnostic_retention_does_not_delete_corpus_rows(db_session):
    source = Source(source_id="retain", name="Retention", domain="example.com", enabled=True, language="filipino")
    db_session.add(source)
    db_session.flush()
    runs = []
    for index in range(3):
        run = CrawlRun(
            crawl_id=f"CRAWL_RET_{index}", source_id="retain", mode="discover",
            crawler_version="test", config_hash="x",
            end_time=datetime.utcnow() - timedelta(days=3 - index),
        )
        runs.append(run)
        db_session.add(run)
    db_session.add(URL(url="https://example.com/corpus", source_id="retain", status="DISCOVERED"))
    db_session.flush()
    from src.crawler.discovery_reporting import BoundedDiscoveryCollector, DiscoveryReport, DiscoveryOutcome, ScopeReason
    for run in runs:
        report = DiscoveryReport(source_id="retain", collector=BoundedDiscoveryCollector(sample_cap=1))
        report.collector.record(DiscoveryOutcome.QUEUED, ScopeReason.ACCEPTED, discovery_method="RSS", candidate_url="https://example.com/x")
        DiscoveryDiagnosticsStore(db_session, enabled=True, sample_cap=1, retention_runs=1).persist(
            report, crawl_id=run.crawl_id, source_id="retain"
        )
    store = DiscoveryDiagnosticsStore(db_session, enabled=True, retention_runs=1)
    deleted = store.retain_completed_runs(source_id="retain")
    assert deleted == 2
    assert db_session.query(DiscoveryObservation).count() == 1
    assert db_session.query(URL).count() == 1


def test_read_only_sqlite_manager_uses_query_only_and_missing_db_is_clear(tmp_path):
    db_path = tmp_path / "audit.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    ro = DatabaseManager.read_only(f"sqlite:///{db_path}")
    with ro.get_session() as session:
        assert session.execute(text("PRAGMA query_only")).scalar() == 1
        with pytest.raises(Exception):
            session.execute(text("CREATE TABLE should_not_exist (id INTEGER)"))
    with pytest.raises(FileNotFoundError, match="does not exist"):
        DatabaseManager.read_only(f"sqlite:///{tmp_path / 'missing.db'}")


def test_pipeline_persists_enabled_discovery_diagnostics(monkeypatch, db_session):
    xml = b"""<rss version='2.0'><channel>
      <item><title>Article</title><link>https://example.com/article-1</link></item>
    </channel></rss>"""

    class Response:
        status_code = 200
        headers = {"Content-Type": "application/rss+xml"}
        url = "https://example.com/feed.xml"
        content = xml
        text = xml.decode("utf-8")

    monkeypatch.setattr("src.crawler.discovery.httpx.get", lambda url, **kwargs: Response())
    config = CrawlerConfig(Path("config/crawler.yaml"))
    config.config["discovery"]["diagnostics"]["enabled"] = True
    source = SourceConfig(
        id="pipeline_diag",
        name="Pipeline diagnostics",
        domain="example.com",
        article_path_patterns=[r"^/article-\d+$"],
        rss=[],
        sitemap=[],
    )
    # The pipeline owns a robots manager; replace both seams so this remains
    # an entirely offline integration test.
    pipeline = CrawlPipeline(db_session, config, "OfflineTestBot/1.0")
    pipeline.robots_mgr.is_allowed = MagicMock(return_value=True)
    pipeline.robots_mgr.is_allowed_async = AsyncMock(return_value=True)
    pipeline.robots_mgr.get_sitemaps = MagicMock(return_value=[])
    pipeline.robots_mgr.get_sitemaps_async = AsyncMock(return_value=[])
    source.rss = [RSSFeedConfig(url="https://example.com/feed.xml", category="general")]

    found, added = pipeline.discover_urls([source])

    assert (found, added) == (1, 1)
    observation = db_session.query(DiscoveryObservation).one()
    assert observation.source_id == "pipeline_diag"
    assert observation.outcome == "QUEUED"
    assert observation.observation_count == 1
