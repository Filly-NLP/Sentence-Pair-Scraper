import gzip
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.crawler.config import CrawlerConfig
from src.crawler.fetcher import FetchResult
from src.crawler.pipeline import CrawlPipeline
from src.storage.models import URL, URLStatus
from src.storage.warc_store import WarcStore


def test_disabled_warc_store_performs_no_writes(tmp_path: Path):
    store = WarcStore(tmp_path / "warc", enabled=False)
    reference = store.capture("https://example.com/a", FetchResult(status=500))
    assert reference.outcome == "disabled"
    assert not (tmp_path / "warc").exists()
    assert store.finalize()["outcome"] == "disabled"


def test_response_round_trip_and_header_redaction(tmp_path: Path):
    store = WarcStore(
        tmp_path / "warc",
        preserve_failures=True,
        success_sample_rate=1.0,
        max_response_bytes=1000,
        rotate_bytes=10000,
    )
    result = FetchResult(
        status=500,
        network_status=500,
        headers={
            "Content-Type": "text/html",
            "Set-Cookie": "session=private",
            "X-Debug": "kept",
        },
        request_headers={"Authorization": "Bearer private", "User-Agent": "TestBot"},
        response_bytes=b"failure body",
    )
    reference = store.capture(
        "https://user:password@example.com/a?token=private&keep=value",
        result,
        run_id="run-1",
    )
    assert reference.outcome == "written"
    assert reference.record_id and reference.digest
    path = tmp_path / "warc" / reference.path
    raw = gzip.decompress(path.read_bytes()).decode("utf-8", errors="replace")
    assert "WARC-Type: response" in raw
    assert "WARC-Target-URI: https://example.com/a?token=%5BREDACTED%5D&keep=value" in raw
    assert "failure body" in raw
    assert "private" not in raw
    assert "X-Debug: kept" in raw


def test_response_and_run_budgets_truncate_and_rotate(tmp_path: Path):
    store = WarcStore(
        tmp_path / "warc",
        preserve_failures=True,
        max_response_bytes=4,
        max_total_bytes_per_run=6,
        max_disk_bytes=100_000,
        rotate_bytes=1,
    )
    first = store.capture(
        "https://example.com/one",
        FetchResult(status=503, network_status=503, response_bytes=b"123456"),
        run_id="run-1",
    )
    second = store.capture(
        "https://example.com/two",
        FetchResult(status=503, network_status=503, response_bytes=b"abcdef"),
        run_id="run-1",
    )
    assert first.outcome == "written"
    assert first.truncated is True
    assert second.outcome == "written"
    assert second.truncated is True
    assert second.bytes_written > 0
    assert len(list((tmp_path / "warc").glob("segment-*.warc.gz"))) == 2


def test_interrupted_part_is_quarantined(tmp_path: Path):
    directory = tmp_path / "warc"
    directory.mkdir()
    (directory / "segment-000001.warc.gz.deadbeef.part").write_bytes(b"partial")
    store = WarcStore(directory, enabled=True)
    assert not list(directory.glob("*.part"))
    assert list(directory.glob("*.partial.recovered.*"))
    assert store.finalize()["outcome"] == "finalized"


@pytest.mark.asyncio
async def test_warc_failure_isolation_keeps_successful_crawl(db_session, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "crawler.yaml"
    config_path.write_text(
        """
crawler:
  date_cutoff: "2022-01-01"
extraction:
  min_body_chars: 40
warc:
  enabled: true
  directory: "artifacts"
  preserve_failures: true
  success_sample_rate: 1.0
  max_response_bytes: 100000
  max_total_bytes_per_run: 100000
  max_disk_bytes: 1000000
  rotate_bytes: 100000
""",
        encoding="utf-8",
    )
    pipeline = CrawlPipeline(db_session, CrawlerConfig(config_path), "TestBot/1.0")
    pipeline.robots_mgr = None
    pipeline.warc_store.capture = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("sidecar unavailable"))
    pipeline.fetcher.fetch = AsyncMock(
        return_value=FetchResult(
            status=200,
            html="""
            <html><head><meta property="article:published_time" content="2024-03-01T08:00:00Z"></head>
            <body><article class="entry-content"><p>Ito ay isang magandang balita para sa lahat ng mamamayan sa bansa.</p>
            <p>Patuloy ang pamahalaan sa pagbibigay ng tulong sa mga nangangailangan.</p></article></body></html>
            """,
            response_bytes=b"wire response",
        )
    )
    url = URL(url="https://example.com/news/1", source_id="bandera", status=URLStatus.DISCOVERED)
    db_session.add(url)
    db_session.commit()

    await pipeline.crawl_queued_urls(source_id="bandera")

    db_session.refresh(url)
    assert url.status == URLStatus.ACCEPTED
    assert '"outcome": "error"' in (url.extraction_diagnostics or "")
