import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, text

from src.crawler.config import CrawlerConfig
from src.crawler.fetcher import FetchResult
from src.crawler.pipeline import CrawlPipeline
from src.crawler.discovery_reporting import DiscoveryDiagnosticsStore
from src.sources.registry import ExtractionConfig, RSSFeedConfig, SourceConfig
from src.storage.migrations import apply_additive_migrations
from src.storage.models import Article, CrawlEvent, CrawlRun, Sentence, Source, URL, URLStatus


class _AlwaysFilipino:
    def __init__(self, *args, **kwargs):
        pass

    def detect_sentence_language(self, _sentence):
        return "FILIPINO", 1.0

    def is_accepted(self, _label, _confidence):
        return True


def _config_with_no_delay() -> CrawlerConfig:
    config = CrawlerConfig(Path("config/crawler.yaml"))
    config.config["rate_limiting"]["default_delay_seconds"] = 0
    config.config["link_discovery"]["enabled"] = False
    return config


@pytest.mark.asyncio
async def test_distinct_articles_keep_unique_sentences_and_record_cross_article_duplicate(
    db_session, monkeypatch
):
    source = SourceConfig(
        id="sentence_dedup",
        name="Sentence Dedup",
        domain="example.com",
        enabled=True,
        language="filipino",
        crawl_delay_seconds=0,
        article_path_patterns=[r"^/news/2026/[^/]+$"],
        extraction=ExtractionConfig(type="generic", content_selector=".article-body"),
    )
    db_session.add(Source(
        source_id=source.id,
        name=source.name,
        domain=source.domain,
        enabled=True,
        language=source.language,
    ))
    db_session.add_all([
        URL(
            url="https://example.com/news/2026/first",
            source_id=source.id,
            status=URLStatus.DISCOVERED,
        ),
        URL(
            url="https://example.com/news/2026/second",
            source_id=source.id,
            status=URLStatus.DISCOVERED,
        ),
    ])
    db_session.commit()

    config = _config_with_no_delay()
    pipeline = CrawlPipeline(db_session, config, "Stage8BTestBot/1.0")
    pipeline._source_config = lambda _source_id: source
    pipeline.robots_mgr.is_allowed = lambda _url: True
    pipeline.robots_mgr.is_allowed_async = AsyncMock(return_value=True)
    monkeypatch.setattr("src.crawler.pipeline.FilipinoLanguageDetector", _AlwaysFilipino)

    html_by_url = {
        "https://example.com/news/2026/first": (
            "<html><head><meta property='article:published_time' "
            "content='2026-08-11T09:00:00Z'></head><body><article "
            "class='article-body'><p>Ito ay pangungusap na pareho. "
            "Ito ay pangungusap na natatangi una.</p></article></body></html>"
        ),
        "https://example.com/news/2026/second": (
            "<html><head><meta property='article:published_time' "
            "content='2026-08-11T10:00:00Z'></head><body><article "
            "class='article-body'><p>Ito ay pangungusap na pareho. "
            "Ito ay pangungusap na natatangi ikalawa.</p></article></body></html>"
        ),
    }

    async def fetch(url):
        return FetchResult(
            status=200,
            html=html_by_url[url],
            headers={"content-type": "text/html"},
            final_url=url,
        )

    monkeypatch.setattr(pipeline.fetcher, "fetch", fetch)
    await pipeline.crawl_queued_urls(source_id=source.id)

    assert db_session.query(Article).count() == 2
    assert db_session.query(Sentence).count() == 3
    assert db_session.query(Sentence.content_hash).distinct().count() == 3
    second = db_session.query(URL).filter(URL.url.endswith("/second")).one()
    assert second.status == URLStatus.ACCEPTED
    second_diagnostics = json.loads(second.extraction_diagnostics)
    assert second_diagnostics["rejections"]["duplicate_db"] == 1


def test_diagnostics_persistence_failure_does_not_rollback_discovered_queue(
    db_session, monkeypatch
):
    xml = b"""<rss version='2.0'><channel>
      <item><title>Article</title><link>https://example.com/article-1</link></item>
    </channel></rss>"""

    class Response:
        status_code = 200
        headers = {"Content-Type": "application/rss+xml"}
        url = "https://example.com/feed.xml"
        content = xml
        text = xml.decode("utf-8")

    monkeypatch.setattr("src.crawler.discovery.httpx.get", lambda _url, **_kwargs: Response())
    monkeypatch.setattr(
        DiscoveryDiagnosticsStore,
        "persist",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("diagnostics unavailable")),
    )

    config = _config_with_no_delay()
    config.config["discovery"]["diagnostics"]["enabled"] = True
    source = SourceConfig(
        id="diag_failure",
        name="Diagnostics Failure",
        domain="example.com",
        article_path_patterns=[r"^/article-\d+$"],
        rss=[RSSFeedConfig(url="https://example.com/feed.xml")],
    )
    pipeline = CrawlPipeline(db_session, config, "Stage8BTestBot/1.0")
    pipeline.robots_mgr.is_allowed = lambda _url: True
    pipeline.robots_mgr.get_sitemaps = lambda _domain: []

    with pytest.raises(RuntimeError, match="diagnostics unavailable"):
        pipeline.discover_urls([source])

    queued = db_session.query(URL).filter(URL.source_id == source.id).one()
    assert queued.url == "https://example.com/article-1"
    assert queued.status == URLStatus.DISCOVERED
    run = db_session.query(CrawlRun).one()
    assert run.end_time is not None
    assert db_session.query(CrawlEvent).filter(
        CrawlEvent.event_type == "error:discovery"
    ).count() == 1


def test_file_backed_legacy_migration_is_idempotent_and_preserves_rows(tmp_path):
    db_path = tmp_path / "legacy-copy.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE sources (source_id VARCHAR(50) PRIMARY KEY, "
            "name VARCHAR(100), domain VARCHAR(100), enabled BOOLEAN, status VARCHAR(20))"
        ))
        connection.execute(text(
            "CREATE TABLE urls (url_id INTEGER PRIMARY KEY, url TEXT NOT NULL, "
            "status VARCHAR(20), next_retry_at DATETIME, source_id VARCHAR(50))"
        ))
        connection.execute(text(
            "CREATE TABLE articles (article_id VARCHAR(50) PRIMARY KEY, url_id INTEGER)"
        ))
        connection.execute(text(
            "CREATE TABLE sentences (sentence_id VARCHAR(50) PRIMARY KEY, article_id VARCHAR(50))"
        ))
        connection.execute(text(
            "INSERT INTO sources VALUES ('legacy', 'Legacy', 'example.com', 1, 'ACTIVE')"
        ))
        connection.execute(text(
            "INSERT INTO urls VALUES (1, 'https://example.com/legacy', 'DISCOVERED', NULL, 'legacy')"
        ))
        connection.execute(text(
            "INSERT INTO articles VALUES ('article-1', 1)"
        ))
        connection.execute(text(
            "INSERT INTO sentences VALUES ('sentence-1', 'article-1')"
        ))

    first = apply_additive_migrations(engine, dry_run=False)
    second = apply_additive_migrations(engine, dry_run=False)
    assert first["columns_added"]
    assert second["columns_added"] == []
    assert second["indexes_created"] == []

    engine.dispose()
    reopened = create_engine(f"sqlite:///{db_path}")
    with reopened.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM sources")).scalar() == 1
        assert connection.execute(text("SELECT COUNT(*) FROM urls")).scalar() == 1
        assert connection.execute(text("SELECT COUNT(*) FROM articles")).scalar() == 1
        assert connection.execute(text("SELECT COUNT(*) FROM sentences")).scalar() == 1
        assert connection.execute(text("SELECT sentence_count FROM articles")).scalar() == 1
        assert connection.execute(text("SELECT sentence_count FROM urls")).scalar() == 1
    reopened.dispose()
