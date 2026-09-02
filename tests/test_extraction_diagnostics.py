import json
from pathlib import Path
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from src.crawler.config import CrawlerConfig
from src.crawler.fetcher import FetchResult
from src.crawler.pipeline import CrawlPipeline
from src.extraction.base import ArticleExtractor
from src.sources.registry import ExtractionConfig, SourceConfig
from src.storage.models import Article, URL, URLStatus


def test_article_extractor_captures_selector_and_paragraph_count():
    source = SourceConfig(
        id="test", name="Test", domain="example.com", enabled=True,
        extraction=ExtractionConfig(type="custom", content_selector=".story-content", date_selector=".pub-time"),
    )
    html = '''<html>
      <head><meta property="article:published_time" content="2024-03-01T08:00:00Z" /></head>
      <body>
        <div class="story-content">
          <p>Unang talata ng balita para sa pagsusuri.</p>
          <p>Pangalawang talata na may mahahalagang detalye.</p>
        </div>
      </body>
    </html>'''
    extracted = ArticleExtractor.extract(html, source)
    assert extracted["selector_match"] == ".story-content"
    assert extracted["extraction_method"] == "custom"
    assert extracted["paragraph_count"] == 2
    assert extracted["body_chars"] > 50


@pytest.mark.asyncio
async def test_pipeline_records_bounded_extraction_diagnostics(db_session):
    config = CrawlerConfig(Path("config/crawler.yaml"))
    pipeline = CrawlPipeline(db_session=db_session, config=config, user_agent="TestBot/1.0")

    html_content = '''<html>
      <head><meta property="article:published_time" content="2024-03-01T08:00:00Z" /></head>
      <body>
        <article class="entry-content">
          <p>Ito ay isang magandang balita para sa lahat ng mamamayan sa bansa.</p>
          <p>Patuloy ang pamahalaan sa pagbibigay ng tulong sa mga nangangailangan.</p>
        </article>
      </body>
    </html>'''

    fetch_res = FetchResult(
        status=200,
        html=html_content,
        headers={},
    )

    with patch.object(pipeline.fetcher, "fetch", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = fetch_res
        url_rec = URL(
            url="https://bandera.inquirer.net/2024/03/01/sample-news",
            source_id="bandera",
            status=URLStatus.DISCOVERED,
        )
        db_session.add(url_rec)
        db_session.commit()

        await pipeline.crawl_queued_urls(source_id="bandera")

        db_session.refresh(url_rec)
        assert url_rec.status == URLStatus.ACCEPTED
        assert url_rec.extraction_diagnostics is not None

        diagnostics = json.loads(url_rec.extraction_diagnostics)
        assert "selector_match" in diagnostics
        assert "extraction_method" in diagnostics
        assert diagnostics["body_chars"] > 0
        assert diagnostics["paragraph_count"] == 2
        assert diagnostics["segmented_count"] >= 2
        assert diagnostics["accepted_count"] >= 2
        assert "rejections" in diagnostics
        assert diagnostics["rejections"]["quality"] == 0
        assert diagnostics["date_source"] == "html_meta"
        assert diagnostics["date_confidence"] == 1.0


@pytest.mark.asyncio
async def test_pipeline_records_zero_sentence_rejection_breakdown(db_session):
    config = CrawlerConfig(Path("config/crawler.yaml"))
    pipeline = CrawlPipeline(db_session=db_session, config=config, user_agent="TestBot/1.0")
    pipeline.language_threshold = 0.99

    # English text in a Filipino news adapter -> rejected by language filter
    html_content = '''<html>
      <head><meta property="article:published_time" content="2024-03-01T08:00:00Z" /></head>
      <body>
        <article class="entry-content">
          <p>This is purely English content that should be rejected by the Filipino language filter.</p>
        </article>
      </body>
    </html>'''

    fetch_res = FetchResult(
        status=200,
        html=html_content,
        headers={},
    )

    with patch.object(pipeline.fetcher, "fetch", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = fetch_res
        url_rec = URL(
            url="https://bandera.inquirer.net/2024/03/01/english-article",
            source_id="bandera",
            status=URLStatus.DISCOVERED,
        )
        db_session.add(url_rec)
        db_session.commit()

        await pipeline.crawl_queued_urls(source_id="bandera")

        db_session.refresh(url_rec)
        assert url_rec.status == URLStatus.NO_SENTENCES
        diagnostics = json.loads(url_rec.extraction_diagnostics)
        assert diagnostics["accepted_count"] == 0
        assert diagnostics["rejections"]["language"] >= 1
