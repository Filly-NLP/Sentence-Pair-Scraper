from datetime import datetime, timedelta, timezone

import pytest

from src.crawler.pipeline import CrawlPipeline
from src.sources.registry import SourceConfig
from src.storage.models import Article, Sentence, Source, URL


class FakeConfig:
    config = {
        "crawler": {"date_cutoff": "2022-01-01", "queue_batch_size": 10, "processing_timeout_seconds": 60},
        "http": {"timeout_seconds": 1, "max_retries": 1},
        "rate_limiting": {"default_delay_seconds": 0, "default_max_concurrent": 1, "backoff": {}},
        "cache": {"http_cache_dir": "data/test-cache", "robots_ttl_hours": 24},
        "language": {"min_confidence": 0.5, "classify_mixed": True},
        "sentence": {"min_tokens": 3, "max_tokens": 100, "include_quotes": True},
        "extraction": {"min_body_chars": 10},
    }

    def get(self, key, default=None):
        value = self.config
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value


class StubFetcher:
    async def start(self):
        pass

    async def close(self):
        pass

    async def fetch(self, url):
        return {"status": 200, "html": "<html></html>"}


class StubDetector:
    def __init__(self, confidence=1.0):
        self.confidence = confidence

    def detect_sentence_language(self, sentence):
        return "FILIPINO", self.confidence


def _pipeline(db_session, monkeypatch, include_quotes=True, confidence=1.0):
    source = Source(source_id="test", name="Test", domain="example.com", language="filipino", enabled=True)
    db_session.add(source)
    db_session.commit()
    config = FakeConfig()
    config.config = {key: (dict(value) if isinstance(value, dict) else value) for key, value in FakeConfig.config.items()}
    config.config["sentence"]["include_quotes"] = include_quotes
    pipeline = CrawlPipeline(db_session, config, "test-agent")
    pipeline.fetcher = StubFetcher()
    pipeline.lang_detector = StubDetector(confidence)
    pipeline.robots_mgr.is_allowed = lambda url: True
    pipeline.robots_mgr.get_crawl_delay = lambda host: 0
    async def no_wait(url):
        pass
    pipeline.rate_limiter.wait_if_needed = no_wait
    monkeypatch.setattr(
        "src.crawler.pipeline.ArticleExtractor.extract",
        lambda html, source: {
            "headline": "Balita",
            "author": "",
            "publication_date_raw": "2026-08-11",
            "publication_date_source": "meta",
            "canonical_url": "",
            "article_text": "Magandang balita para sa buong bayan. " * 2,
        },
    )
    return pipeline


@pytest.mark.asyncio
async def test_repeated_sentence_does_not_rollback_article(db_session, monkeypatch):
    pipeline = _pipeline(db_session, monkeypatch)
    db_session.add(URL(url="https://example.com/article", source_id="test", status="DISCOVERED"))
    db_session.commit()

    await pipeline.crawl_queued_urls()

    assert db_session.query(Article).count() == 1
    assert db_session.query(Sentence).count() == 1
    assert db_session.query(URL).one().status == "ACCEPTED"


@pytest.mark.asyncio
async def test_quote_and_language_threshold_are_config_driven(db_session, monkeypatch):
    pipeline = _pipeline(db_session, monkeypatch, include_quotes=False, confidence=0.6)
    db_session.add(URL(url="https://example.com/quoted", source_id="test", status="DISCOVERED"))
    db_session.commit()
    monkeypatch.setattr(
        "src.crawler.pipeline.ArticleExtractor.extract",
        lambda html, source: {
            "headline": "Balita", "author": "", "publication_date_raw": "2026-08-11",
            "publication_date_source": "meta", "canonical_url": "",
            "article_text": '"Mabuti ang plano para sa bayan," sabi ng opisyal. ' * 2,
        },
    )

    await pipeline.crawl_queued_urls()
    assert db_session.query(Sentence).count() == 0
    url_rec = db_session.query(URL).one()
    assert url_rec.status == "NO_SENTENCES"
    assert url_rec.sentence_count == 0


@pytest.mark.asyncio
async def test_stale_processing_record_is_recovered(db_session, monkeypatch):
    pipeline = _pipeline(db_session, monkeypatch)
    db_session.add(URL(
        url="https://example.com/stale", source_id="test", status="PROCESSING",
        discovered_at=datetime.utcnow() - timedelta(hours=2),
    ))
    db_session.commit()

    await pipeline.crawl_queued_urls()

    assert db_session.query(URL).one().status == "ACCEPTED"


@pytest.mark.asyncio
async def test_rss_date_hint_uses_timezone_aware_cutoff(db_session, monkeypatch):
    pipeline = _pipeline(db_session, monkeypatch)
    monkeypatch.setattr(
        "src.crawler.pipeline.ArticleExtractor.extract",
        lambda html, source: {
            "headline": "Balita", "author": "", "publication_date_raw": "",
            "publication_date_source": "missing", "canonical_url": "",
            "article_text": "Magandang balita para sa buong bayan. " * 2,
        },
    )
    db_session.add_all([
        URL(
            url="https://example.com/old-rss", source_id="test", status="DISCOVERED",
            publication_date_hint=datetime(2021, 12, 31, 23, 59, tzinfo=timezone.utc),
        ),
        URL(
            url="https://example.com/boundary-rss", source_id="test", status="DISCOVERED",
            publication_date_hint=datetime(2022, 1, 1, 8, 0, tzinfo=timezone(timedelta(hours=8))),
        ),
    ])
    db_session.commit()

    await pipeline.crawl_queued_urls()

    statuses = {url.url: (url.status, url.error_reason) for url in db_session.query(URL).all()}
    assert statuses["https://example.com/old-rss"] == ("REJECTED", "date_before_cutoff")
    assert statuses["https://example.com/boundary-rss"][0] == "ACCEPTED"
