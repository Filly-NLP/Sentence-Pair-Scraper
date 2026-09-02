from datetime import datetime, timedelta
import tempfile
from pathlib import Path
import pytest
from unittest.mock import AsyncMock
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.crawler.config import CrawlerConfig
from src.crawler.fetcher import FetchResult
from src.crawler.pipeline import CrawlPipeline
from src.storage.database import Base
from src.storage.models import Source, URL, URLStatus


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


@pytest.fixture
def sample_config():
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
        yield CrawlerConfig(cfg_file)


@pytest.mark.asyncio
async def test_202_enters_bounded_retry_wait(db_session, sample_config):
    source = Source(source_id="test_src", name="Test", domain="example.com", enabled=True)
    url_rec = URL(url_id=1, source_id="test_src", url="https://example.com/article1", status=URLStatus.DISCOVERED)
    db_session.add_all([source, url_rec])
    db_session.commit()

    pipeline = CrawlPipeline(db_session, sample_config, "TestBot/1.0")
    pipeline.robots_mgr.is_allowed = lambda url: True
    pipeline.fetcher.fetch = AsyncMock(return_value=FetchResult(
        status=202,
        body="",
        error="HTTP_202",
        error_category="HTTP_TRANSIENT",
        retry_after=15.0
    ))

    await pipeline.crawl_queued_urls()
    db_session.refresh(url_rec)

    assert url_rec.status == URLStatus.RETRY_WAIT
    assert url_rec.retry_count == 1
    assert url_rec.last_http_status == 202
    assert url_rec.next_retry_at is not None


@pytest.mark.asyncio
async def test_403_bounded_lifecycle_transitions_to_blocked(db_session, sample_config):
    source = Source(source_id="test_src", name="Test", domain="example.com", enabled=True)
    url_rec = URL(url_id=2, source_id="test_src", url="https://example.com/article1", status=URLStatus.DISCOVERED, retry_count=0)
    db_session.add_all([source, url_rec])
    db_session.commit()

    pipeline = CrawlPipeline(db_session, sample_config, "TestBot/1.0")
    pipeline.robots_mgr.is_allowed = lambda url: True
    pipeline.fetcher.fetch = AsyncMock(return_value=FetchResult(
        status=403,
        body="",
        error="HTTP_403",
        error_category="FORBIDDEN",
        retry_after=60.0
    ))

    # First attempt: enters RETRY_WAIT (since max_403_retries = 1)
    await pipeline.crawl_queued_urls()
    db_session.refresh(url_rec)
    assert url_rec.status == URLStatus.RETRY_WAIT
    assert url_rec.retry_count == 1
    assert url_rec.last_http_status == 403

    # Reset next_retry_at to past so it becomes due
    url_rec.next_retry_at = datetime.utcnow() - timedelta(minutes=5)
    db_session.commit()

    # Second attempt: retry count reaches limit -> transitions to BLOCKED
    await pipeline.crawl_queued_urls()
    db_session.refresh(url_rec)
    assert url_rec.status == URLStatus.BLOCKED
    assert url_rec.last_http_status == 403


@pytest.mark.asyncio
async def test_due_retries_claimed_and_future_retries_skipped(db_session, sample_config):
    source = Source(source_id="test_src", name="Test", domain="example.com", enabled=True)
    due_url = URL(
        url_id=3,
        source_id="test_src",
        url="https://example.com/due",
        status=URLStatus.RETRY_WAIT,
        next_retry_at=datetime.utcnow() - timedelta(minutes=10),
        retry_count=1,
    )
    future_url = URL(
        url_id=4,
        source_id="test_src",
        url="https://example.com/future",
        status=URLStatus.RETRY_WAIT,
        next_retry_at=datetime.utcnow() + timedelta(minutes=30),
        retry_count=1,
    )
    db_session.add_all([source, due_url, future_url])
    db_session.commit()

    pipeline = CrawlPipeline(db_session, sample_config, "TestBot/1.0")
    pipeline.robots_mgr.is_allowed = lambda url: True

    processed = []
    async def fake_fetch(url):
        processed.append(url)
        return FetchResult(
            status=200,
            body="<html><head><meta property='article:published_time' content='2024-01-01T00:00:00Z'></head><body><p>Ito ay isang magandang artikulo tungkol sa balita.</p></body></html>",
            cached=False
        )

    pipeline.fetcher.fetch = AsyncMock(side_effect=fake_fetch)
    await pipeline.crawl_queued_urls()

    db_session.refresh(due_url)
    db_session.refresh(future_url)

    assert "https://example.com/due" in processed
    assert "https://example.com/future" not in processed
    assert due_url.status == URLStatus.ACCEPTED
    assert future_url.status == URLStatus.RETRY_WAIT
