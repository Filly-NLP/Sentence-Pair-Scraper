import asyncio
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.crawler.config import CrawlerConfig
from src.crawler.fetcher import FetchResult
from src.crawler.pipeline import CrawlPipeline
from src.storage.database import Base
from src.storage.models import Article, Sentence, Source, URL, URLStatus


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
def test_config():
    with tempfile.TemporaryDirectory() as td:
        cfg_file = Path(td) / "crawler.yaml"
        cfg_file.write_text("""crawler:
  version: "1.0.0"
  date_cutoff: "2022-01-01"
  queue_batch_size: 10
  processing_timeout_seconds: 10
sentence:
  min_tokens: 3
  max_tokens: 100
  include_quotes: false
language:
  min_confidence: 0.5
storage:
  database_url: "sqlite:///:memory:"
""")
        yield CrawlerConfig(cfg_file)


@pytest.mark.asyncio
async def test_stale_processing_recovered_on_restart(db_session, test_config):
    """Scenario 8: Cancellation/restart recovers stale processing and downloaded records."""
    source = Source(source_id="rec_src", name="Recovery Test", domain="example.com", enabled=True)
    stale_time = datetime.utcnow() - timedelta(seconds=100)
    
    # URL 1 was stuck in PROCESSING due to an unexpected crash/cancellation
    url_stuck_proc = URL(
        url_id=1,
        source_id="rec_src",
        url="https://example.com/2023/01/01/crashed-midway",
        status=URLStatus.PROCESSING,
        processing_started_at=stale_time,
    )
    # URL 2 was stuck in DOWNLOADED
    url_stuck_down = URL(
        url_id=2,
        source_id="rec_src",
        url="https://example.com/2023/01/01/crashed-after-download",
        status=URLStatus.DOWNLOADED,
        fetched_at=stale_time,
    )
    db_session.add_all([source, url_stuck_proc, url_stuck_down])
    db_session.commit()

    pipeline = CrawlPipeline(db_session, test_config, "TestBot/1.0")
    pipeline.robots_mgr.is_allowed = lambda url: True

    article_html = """<html><head><meta property="article:published_time" content="2023-01-01T12:00:00Z"></head><body>
    <article><p>Matagumpay na natapos ang pagpupulong ng mga kinatawan mula sa iba't ibang sektor.</p></article>
    </body></html>"""

    pipeline.fetcher.fetch = AsyncMock(return_value=FetchResult(status=200, body=article_html, cached=False))

    # Restart crawl: _recover_stale_processing will be triggered automatically
    await pipeline.crawl_queued_urls()

    db_session.refresh(url_stuck_proc)
    db_session.refresh(url_stuck_down)

    assert url_stuck_proc.status == URLStatus.ACCEPTED
    assert url_stuck_proc.sentence_count >= 1
    assert url_stuck_down.status == URLStatus.ACCEPTED
    assert url_stuck_down.sentence_count >= 1