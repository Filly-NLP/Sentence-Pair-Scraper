import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

from src.crawler.config import CrawlerConfig
from src.crawler.fetcher import FetchResult
from src.crawler.pipeline import CrawlJob, CrawlPipeline
from src.extraction.trafilatura_fallback import FallbackExtraction
from src.sources.registry import ExtractionConfig, SourceConfig, TrafilaturaSourceConfig


def _config(tmp_path: Path, enabled: bool) -> CrawlerConfig:
    path = tmp_path / "crawler.yaml"
    path.write_text(
        f"""
crawler:
  date_cutoff: "2022-01-01"
extraction:
  min_body_chars: 40
  fallback:
    trafilatura:
      enabled: {str(enabled).lower()}
      min_body_chars: 200
      favor_precision: true
""",
        encoding="utf-8",
    )
    return CrawlerConfig(path)


def _source(enabled: bool) -> SourceConfig:
    return SourceConfig(
        id="fallback-test",
        name="Fallback Test",
        domain="example.com",
        extraction=ExtractionConfig(
            type="custom",
            content_selector=".primary",
            trafilatura=TrafilaturaSourceConfig(enabled=enabled),
        ),
    )


def _job() -> CrawlJob:
    return CrawlJob(
        url_id=1,
        url="https://example.com/news/1",
        source_id="fallback-test",
        domain="example.com",
        retry_count=0,
        configured_delay=0,
        max_concurrent=1,
    )


def _pipeline(tmp_path: Path, enabled: bool) -> CrawlPipeline:
    # This worker-only test does not use ORM persistence; a minimal SQLite
    # session is still supplied because CrawlPipeline owns that dependency.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from src.storage.models import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return CrawlPipeline(sessionmaker(bind=engine)(), _config(tmp_path, enabled), "TestBot/1.0")


def _html() -> str:
    return """
    <html><head><meta property="article:published_time" content="2024-03-01T08:00:00Z"></head>
    <body><div class="primary"><p>Maikling primary.</p></div></body></html>
    """


def test_fallback_extract_body_is_lazy_and_body_only(monkeypatch):
    imported = {}

    class FakeTrafilatura:
        def extract(self, html, **kwargs):
            imported["kwargs"] = kwargs
            return "Unang talata ng balita.\n\nIkalawang talata ng balita."

    monkeypatch.setattr(
        "src.extraction.trafilatura_fallback._load_trafilatura",
        lambda: FakeTrafilatura(),
    )
    result = __import__("src.extraction.trafilatura_fallback", fromlist=["extract_body"]).extract_body(
        "<html>fixture</html>", min_body_chars=10
    )
    assert result.outcome == "success"
    assert result.paragraph_count == 2
    assert imported["kwargs"]["include_comments"] is False
    assert imported["kwargs"]["include_tables"] is False


def test_successful_primary_does_not_call_fallback(tmp_path, monkeypatch):
    pipeline = _pipeline(tmp_path, True)
    pipeline.fetcher.fetch = AsyncMock(
        return_value=FetchResult(status=200, html=_html().replace("Maikling primary.", " ".join(["Mahabang primary na nilalaman"] * 20)))
    )
    monkeypatch.setattr(
        "src.crawler.pipeline.extract_trafilatura_body",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("fallback called")),
    )
    pipeline.robots_mgr = None
    outcome = asyncio.run(pipeline._worker_process_job(_job(), _source(True)))
    assert outcome.extracted is not None
    assert outcome.extracted.extractor_used == "primary"
    assert outcome.extracted.fallback_outcome == "not_triggered"


def test_short_primary_uses_fallback_without_replacing_metadata(tmp_path, monkeypatch):
    pipeline = _pipeline(tmp_path, True)
    pipeline.fetcher.fetch = AsyncMock(return_value=FetchResult(status=200, html=_html()))
    pipeline.robots_mgr = None
    monkeypatch.setattr(
        "src.crawler.pipeline.extract_trafilatura_body",
        lambda *args, **kwargs: FallbackExtraction(
            article_text=" ".join(["Narekober na artikulo para sa mambabasa"] * 12),
            body_chars=360,
            paragraph_count=1,
            outcome="success",
            extractor_used="trafilatura",
        ),
    )
    outcome = asyncio.run(pipeline._worker_process_job(_job(), _source(True)))
    assert outcome.extracted is not None
    extracted = outcome.extracted
    assert extracted.extractor_used == "trafilatura"
    assert extracted.extraction_method == "trafilatura"
    assert extracted.publication_date_raw.startswith("2024-03-01")
    assert extracted.primary_outcome == "short"
    assert extracted.fallback_outcome == "success"
    assert extracted.article_text.startswith("Narekober")


def test_disabled_or_source_disabled_fallback_preserves_primary(tmp_path, monkeypatch):
    pipeline = _pipeline(tmp_path, False)
    pipeline.fetcher.fetch = AsyncMock(return_value=FetchResult(status=200, html=_html()))
    pipeline.robots_mgr = None
    monkeypatch.setattr(
        "src.crawler.pipeline.extract_trafilatura_body",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("fallback called")),
    )
    outcome = asyncio.run(pipeline._worker_process_job(_job(), _source(True)))
    assert outcome.extracted is not None
    assert outcome.extracted.extractor_used == "primary"
    assert outcome.extracted.fallback_outcome == "disabled"

