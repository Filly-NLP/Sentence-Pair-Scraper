import json
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
def base_config():
    with tempfile.TemporaryDirectory() as td:
        cfg_file = Path(td) / "crawler.yaml"
        cfg_file.write_text("""crawler:
  version: "1.0.0"
  date_cutoff: "2020-01-01"
  queue_batch_size: 10
  processing_timeout_seconds: 60
sentence:
  min_tokens: 3
  max_tokens: 100
  include_quotes: false
language:
  min_confidence: 0.8
storage:
  database_url: "sqlite:///:memory:"
""")
        yield CrawlerConfig(cfg_file)


@pytest.mark.asyncio
async def test_zero_sentences_due_to_quotes_excluded(db_session, base_config):
    source = Source(source_id="test_src", name="Test", domain="example.com", enabled=True)
    url_rec = URL(url_id=10, source_id="test_src", url="https://example.com/quote-only", status=URLStatus.DISCOVERED)
    db_session.add_all([source, url_rec])
    db_session.commit()

    pipeline = CrawlPipeline(db_session, base_config, "TestBot/1.0")
    pipeline.robots_mgr.is_allowed = lambda url: True

    # HTML body only contains quotes, and include_quotes is false
    pipeline.fetcher.fetch = AsyncMock(return_value=FetchResult(
        status=200,
        body="<html><head><meta property='article:published_time' content='2024-01-01T00:00:00Z'></head><body><p>\"Ito ay isang sinabi lamang sa panayam tungkol sa balita ngayon.\"</p></body></html>",
        cached=False
    ))

    await pipeline.crawl_queued_urls()
    db_session.refresh(url_rec)

    assert url_rec.status == URLStatus.NO_SENTENCES
    assert url_rec.sentence_count == 0
    assert db_session.query(Sentence).count() == 0

    art = db_session.query(Article).filter(Article.url_id == 10).first()
    assert art is not None
    assert art.sentence_count == 0

    # Ensure extraction_diagnostics has breakdown
    assert url_rec.extraction_diagnostics is not None
    diag = json.loads(url_rec.extraction_diagnostics)
    assert diag["reason"] == "no_sentences"
    assert diag["breakdown"]["quote"] >= 1


@pytest.mark.asyncio
async def test_zero_sentences_due_to_non_target_language(db_session, base_config):
    source = Source(source_id="test_src", name="Test", domain="example.com", enabled=True)
    url_rec = URL(url_id=20, source_id="test_src", url="https://example.com/english-only", status=URLStatus.DISCOVERED)
    db_session.add_all([source, url_rec])
    db_session.commit()

    pipeline = CrawlPipeline(db_session, base_config, "TestBot/1.0")
    pipeline.robots_mgr.is_allowed = lambda url: True

    # English text
    pipeline.fetcher.fetch = AsyncMock(return_value=FetchResult(
        status=200,
        body="<html><head><meta property='article:published_time' content='2024-01-01T00:00:00Z'></head><body><p>The president announced new infrastructure initiatives across the country yesterday.</p></body></html>",
        cached=False
    ))

    await pipeline.crawl_queued_urls()
    db_session.refresh(url_rec)

    assert url_rec.status == URLStatus.NO_SENTENCES
    assert url_rec.sentence_count == 0
    assert db_session.query(Sentence).count() == 0

    art = db_session.query(Article).filter(Article.url_id == 20).first()
    assert art is not None
    assert art.sentence_count == 0

    diag = json.loads(url_rec.extraction_diagnostics)
    assert diag["reason"] == "no_sentences"
    assert diag["breakdown"]["language"] >= 1
