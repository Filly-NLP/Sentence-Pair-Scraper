import asyncio
from datetime import datetime, timezone
from pathlib import Path
import time
from unittest.mock import MagicMock
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.crawler.config import CrawlerConfig
from src.crawler.fetcher import FetchResult, HTTPFetcher
from src.crawler.pipeline import CrawlJob, CrawlPipeline
from src.crawler.rate_limiter import RateLimiter
from src.sources.registry import SourceConfig
from src.storage.models import Base, Source, URL, URLStatus


@pytest.fixture
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.mark.asyncio
async def test_cross_domain_overlap_in_pipeline(memory_db, monkeypatch):
    session = memory_db
    config = CrawlerConfig(Path("config/crawler.yaml"))
    config.config = {
        "crawler": {"date_cutoff": "2020-01-01", "queue_batch_size": 10},
        "rate_limiting": {"default_delay_seconds": 0.0, "default_max_concurrent": 1},
        "http": {"timeout_seconds": 5, "max_retries": 1},
    }

    # Insert two sources
    session.add(Source(source_id="src_a", name="Source A", domain="domain-a.com", enabled=True, language="filipino"))
    session.add(Source(source_id="src_b", name="Source B", domain="domain-b.com", enabled=True, language="filipino"))
    session.add(URL(url_id=1, url="https://domain-a.com/2023/01/01/article-1", source_id="src_a", status=URLStatus.DISCOVERED))
    session.add(URL(url_id=2, url="https://domain-b.com/2023/01/01/article-1", source_id="src_b", status=URLStatus.DISCOVERED))
    session.commit()

    active_fetches = set()
    max_active_observed = 0

    async def mock_fetch(url: str) -> FetchResult:
        nonlocal max_active_observed
        active_fetches.add(url)
        max_active_observed = max(max_active_observed, len(active_fetches))
        await asyncio.sleep(0.05)
        active_fetches.remove(url)
        html = f"""<html><body>
          <h1>Headline for {url}</h1>
          <p>Ito ay isang magandang balita sa wikang Filipino na may sapat na haba para sa pagsusuri.</p>
        </body></html>"""
        return FetchResult(status=200, html=html, body=html)

    pipeline = CrawlPipeline(session, config, "TestBot/1.0")
    monkeypatch.setattr(pipeline.fetcher, "fetch", mock_fetch)
    monkeypatch.setattr(pipeline.robots_mgr, "is_allowed", lambda url: True)
    monkeypatch.setattr(pipeline.robots_mgr, "is_allowed_async", lambda url, client=None: asyncio.sleep(0, result=True))

    await pipeline.crawl_queued_urls()

    # Both domain-a and domain-b should have been active at the same time
    assert max_active_observed == 2

    # Verify both completed in database
    u1 = session.query(URL).filter(URL.url_id == 1).first()
    u2 = session.query(URL).filter(URL.url_id == 2).first()
    assert u1.status == URLStatus.ACCEPTED
    assert u2.status == URLStatus.ACCEPTED


@pytest.mark.asyncio
async def test_per_domain_concurrency_bound_is_enforced(memory_db, monkeypatch):
    session = memory_db
    config = CrawlerConfig(Path("config/crawler.yaml"))
    config.config = {
        "crawler": {"date_cutoff": "2020-01-01", "queue_batch_size": 10},
        "rate_limiting": {"default_delay_seconds": 0.0, "default_max_concurrent": 1},
        "http": {"timeout_seconds": 5, "max_retries": 1},
    }

    session.add(Source(source_id="src_a", name="Source A", domain="domain-a.com", enabled=True, language="filipino"))
    session.add(URL(url_id=1, url="https://domain-a.com/2023/01/01/article-1", source_id="src_a", status=URLStatus.DISCOVERED))
    session.add(URL(url_id=2, url="https://domain-a.com/2023/01/01/article-2", source_id="src_a", status=URLStatus.DISCOVERED))
    session.add(URL(url_id=3, url="https://domain-a.com/2023/01/01/article-3", source_id="src_a", status=URLStatus.DISCOVERED))
    session.commit()

    active_fetches = 0
    max_observed_per_domain = 0

    async def mock_fetch(url: str) -> FetchResult:
        nonlocal active_fetches, max_observed_per_domain
        active_fetches += 1
        max_observed_per_domain = max(max_observed_per_domain, active_fetches)
        await asyncio.sleep(0.04)
        active_fetches -= 1
        html = """<html><body>
          <h1>Headline</h1>
          <p>Ito ay isang artikulo sa wikang Filipino para sa pagsubok ng concurrency.</p>
        </body></html>"""
        return FetchResult(status=200, html=html, body=html)

    pipeline = CrawlPipeline(session, config, "TestBot/1.0")
    monkeypatch.setattr(pipeline.fetcher, "fetch", mock_fetch)
    monkeypatch.setattr(pipeline.robots_mgr, "is_allowed", lambda url: True)
    monkeypatch.setattr(pipeline.robots_mgr, "is_allowed_async", lambda url, client=None: asyncio.sleep(0, result=True))

    await pipeline.crawl_queued_urls()

    # Per-domain concurrency must NEVER exceed 1
    assert max_observed_per_domain == 1


@pytest.mark.asyncio
async def test_worker_exception_isolation(memory_db, monkeypatch):
    session = memory_db
    config = CrawlerConfig(Path("config/crawler.yaml"))
    config.config = {
        "crawler": {"date_cutoff": "2020-01-01", "queue_batch_size": 10},
        "rate_limiting": {"default_delay_seconds": 0.0, "default_max_concurrent": 1},
        "http": {"timeout_seconds": 5, "max_retries": 1},
    }

    session.add(Source(source_id="src_a", name="Source A", domain="domain-a.com", enabled=True, language="filipino"))
    session.add(Source(source_id="src_b", name="Source B", domain="domain-b.com", enabled=True, language="filipino"))
    session.add(URL(url_id=1, url="https://domain-a.com/2023/01/01/faulty", source_id="src_a", status=URLStatus.DISCOVERED))
    session.add(URL(url_id=2, url="https://domain-b.com/2023/01/01/healthy", source_id="src_b", status=URLStatus.DISCOVERED))
    session.commit()

    async def mock_fetch(url: str) -> FetchResult:
        if "faulty" in url:
            raise RuntimeError("Simulated network crash")
        html = """<html><body>
          <h1>Headline</h1>
          <p>Ito ay maayos na artikulo sa wikang Filipino na naiproseso nang tama.</p>
        </body></html>"""
        return FetchResult(status=200, html=html, body=html)

    pipeline = CrawlPipeline(session, config, "TestBot/1.0")
    monkeypatch.setattr(pipeline.fetcher, "fetch", mock_fetch)
    monkeypatch.setattr(pipeline.robots_mgr, "is_allowed", lambda url: True)
    monkeypatch.setattr(pipeline.robots_mgr, "is_allowed_async", lambda url, client=None: asyncio.sleep(0, result=True))

    await pipeline.crawl_queued_urls()

    u1 = session.query(URL).filter(URL.url_id == 1).first()
    u2 = session.query(URL).filter(URL.url_id == 2).first()

    assert u1.status == URLStatus.FAILED
    assert "Simulated network crash" in (u1.error_reason or "")
    assert u2.status == URLStatus.ACCEPTED
