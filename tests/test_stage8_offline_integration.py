import gzip
import json
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest
from src.crawler.archive_discovery import ArchiveDiscoveryEngine
from src.crawler.config import CrawlerConfig
from src.crawler.discovery import DiscoveryEngine
from src.crawler.fetcher import HTTPFetcher
from src.crawler.pipeline import CrawlPipeline
from src.crawler.link_discovery import LinkDiscoveryEngine
from src.sources.registry import (
    ArchiveConfig,
    ExtractionConfig,
    LinkDiscoveryConfig,
    RSSFeedConfig,
    SitemapConfig,
    SourceConfig,
)
from src.storage.models import Article, CrawlEvent, Sentence, Source, URL, URLDiscoveryEdge, URLStatus


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "integration"


def _fixture(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


@pytest.fixture
def integration_transport():
    """Return a strict fixture-only transport and its observed request URLs."""

    responses = {
        "https://example.com/feed.xml": (_fixture("feed.xml"), {"content-type": "application/rss+xml"}),
        "https://example.com/sitemap-index.xml": (
            _fixture("sitemap-index.xml"),
            {"content-type": "application/xml"},
        ),
        "https://example.com/sitemap/news-index.xml.gz": (
            gzip.compress(_fixture("sitemap-news-index.xml")),
            {"content-type": "application/gzip"},
        ),
        "https://example.com/sitemap/news-articles.xml.gz": (
            gzip.compress(_fixture("sitemap-news-articles.xml")),
            {"content-type": "application/gzip"},
        ),
        "https://example.com/archive/2026/08/11/": (
            _fixture("archive-page-1.html"),
            {"content-type": "text/html"},
        ),
        "https://example.com/archive/2026/08/11?page=2": (
            _fixture("archive-page-2.html"),
            {"content-type": "text/html"},
        ),
        "https://example.com/news/2026/08/11/100/parent-story": (
            _fixture("parent.html"),
            {"content-type": "text/html"},
        ),
        "https://example.com/news/2026/08/11/101/shared-story": (
            _fixture("shared.html"),
            {"content-type": "text/html"},
        ),
        "https://example.com/news/2026/08/11/102/archive-only": (
            _fixture("archive-only.html"),
            {"content-type": "text/html"},
        ),
        "https://example.com/news/2026/08/11/103/linked-story": (
            _fixture("shared.html"),
            {"content-type": "text/html"},
        ),
    }
    requested_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_url = str(request.url)
        requested_urls.append(requested_url)
        if requested_url not in responses:
            raise AssertionError(
                f"Fixture transport has no response for {requested_url}; "
                "offline integration must not discover an unlisted URL"
            )
        body, headers = responses[requested_url]
        return httpx.Response(200, content=body, headers=headers, request=request)

    return httpx.MockTransport(handler), requested_urls, set(responses)


def _source() -> SourceConfig:
    return SourceConfig(
        id="example_integration",
        name="Example Integration News",
        domain="example.com",
        enabled=True,
        language="filipino",
        crawl_delay_seconds=0,
        max_concurrent=1,
        article_path_patterns=[r"^/news/20\d{2}/\d{2}/\d{2}/\d+/[^/]+/?$"],
        rss=[RSSFeedConfig(url="https://example.com/feed.xml")],
        sitemap=[SitemapConfig(url="https://example.com/sitemap-index.xml")],
        archive=ArchiveConfig(
            enabled=True,
            granularity="day",
            url_template="https://example.com/archive/{yyyy}/{mm}/{dd}/",
            article_link_selector="a.article-link",
            pagination_mode="page_template",
            page_template="{url}?page={page}",
            max_periods=1,
            max_pages_per_period=2,
            max_total_pages=2,
            max_candidates=10,
            max_response_bytes=1024 * 1024,
        ),
        extraction=ExtractionConfig(type="generic", content_selector=".article-body"),
        link_discovery=LinkDiscoveryConfig(enabled=True, article_priority=120),
    )


def _config(tmp_path: Path) -> CrawlerConfig:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "crawler.yaml"
    config_path.write_text(
        f"""crawler:
  version: "1.0.0"
  date_cutoff: "2022-01-01"
  queue_batch_size: 10
  processing_timeout_seconds: 60
  max_lifecycle_retries: 1
  max_403_retries: 0
http:
  timeout_seconds: 1
  max_retries: 1
rate_limiting:
  default_delay_seconds: 0
  default_max_concurrent: 1
  backoff:
    initial_seconds: 0
    multiplier: 1
    max_seconds: 0
    jitter: false
  cooldown:
    trigger_threshold: 5
    duration_seconds: 1
cache:
  http_cache_dir: "{(tmp_path / 'cache').as_posix()}"
  robots_ttl_hours: 24
language:
  min_confidence: 0.0
  classify_mixed: true
  accepted_languages: [FILIPINO]
  allow_mixed: false
sentence:
  min_tokens: 3
  max_tokens: 100
  include_quotes: false
  include_headlines: false
extraction:
  min_body_chars: 40
link_discovery:
  enabled: true
  max_depth: 1
  max_parent_pages_per_source_run: 25
  max_candidates_per_source_run: 50
  max_links_per_page: 100
  allow_query_parameters: false
storage:
  database_url: "sqlite:///:memory:"
""",
        encoding="utf-8",
    )
    return CrawlerConfig(config_path)


class _FixtureRobots:
    def is_allowed(self, _url: str) -> bool:
        return True

    def get_crawl_delay(self, _domain: str) -> float:
        return 0.0


@pytest.mark.asyncio
async def test_stage8a_offline_end_to_end_is_reconciled_and_idempotent(
    db_session, tmp_path, integration_transport
):
    transport, requested_urls, fixture_urls = integration_transport
    source_cfg = _source()
    db_session.add(Source(
        source_id=source_cfg.id,
        name=source_cfg.name,
        domain=source_cfg.domain,
        enabled=True,
        language=source_cfg.language,
    ))
    db_session.commit()

    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        discovery = DiscoveryEngine(
            db_session=db_session,
            user_agent="Stage8TestBot/1.0",
            date_cutoff=datetime(2022, 1, 1),
            timeout=1,
            max_sitemap_depth=5,
            max_sitemap_documents=10,
            max_sitemap_roots=5,
            max_candidates=20,
            robots_mgr=None,
        )
        report = await discovery.discover_source_async(source_cfg, client=client)

        assert report.fetch_errors == 0
        assert report.parse_errors == 0
        assert report.candidates_seen == 6
        assert report.invalid_or_unsupported == 0
        assert report.duplicate_in_run == 2
        assert report.normalized_unique == 4
        assert report.accepted_unique == 2
        assert report.rejected_unique == 2
        assert report.queued_new == 2
        assert report.actually_queued == 2
        assert report.cross_method_duplicates == 1
        assert report.normalized_unique == report.accepted_unique + report.rejected_unique
        assert report.accepted_unique == report.queued_new + report.already_stored
        assert report.candidates_seen == report.invalid_or_unsupported + report.duplicate_in_run + report.normalized_unique
        report.reconcile()

        gzip_roots = [root for root in report.roots if root.requested_url and root.requested_url.endswith(".gz")]
        assert len(gzip_roots) == 2
        assert any(root.document_kind == "index" for root in gzip_roots)
        assert any(root.document_kind == "urlset" for root in gzip_roots)

        shared = db_session.query(URL).filter(URL.url.endswith("/101/shared-story")).one()
        parent = db_session.query(URL).filter(URL.url.endswith("/100/parent-story")).one()
        assert shared.discovery_method == "RSS"
        assert shared.url == "https://example.com/news/2026/08/11/101/shared-story"
        assert parent.discovery_method == "SITEMAP"
        assert parent.sitemap_lastmod_hint is not None
        assert parent.sitemap_lastmod_hint.year == 2030
        assert parent.date_hint_source is None
        assert db_session.query(URL).count() == 2

        archive = ArchiveDiscoveryEngine(
            db_session=db_session,
            user_agent="Stage8TestBot/1.0",
            date_cutoff=datetime(2022, 1, 1),
            timeout=1,
            apply_delay=False,
            persist_queue=True,
        )
        archive_report = await archive.discover_source_async(
            source_cfg,
            client=client,
            from_date=date(2026, 8, 11),
            to_date=date(2026, 8, 11),
            max_periods_override=1,
            max_pages_override=2,
        )
        assert archive_report["periods_planned"] == 1
        assert archive_report["pages_fetched"] == 2
        assert archive_report["links_found"] == 5
        assert archive_report["links_added"] == 1
        assert archive_report["duplicates_skipped"] == 3
        assert archive_report["out_of_scope_skipped"] == 1

        archive_only = db_session.query(URL).filter(URL.url.endswith("/102/archive-only")).one()
        assert archive_only.discovery_method == "ARCHIVE"
        assert db_session.query(URL).count() == 3

    config = _config(tmp_path)
    pipeline = CrawlPipeline(db_session, config, "Stage8TestBot/1.0")
    pipeline.robots_mgr = _FixtureRobots()
    pipeline._source_config = lambda _source_id: source_cfg
    fetcher = HTTPFetcher("Stage8TestBot/1.0", cache=None, timeout=1, max_retries=1)
    fetcher.client = httpx.AsyncClient(transport=transport, follow_redirects=True)
    pipeline.fetcher = fetcher
    await pipeline.crawl_queued_urls(source_id=source_cfg.id)

    parent = db_session.query(URL).filter(URL.url.endswith("/100/parent-story")).one()
    shared = db_session.query(URL).filter(URL.url.endswith("/101/shared-story")).one()
    archive_only = db_session.query(URL).filter(URL.url.endswith("/102/archive-only")).one()
    linked = db_session.query(URL).filter(URL.url.endswith("/103/linked-story")).one()
    assert parent.status == URLStatus.ACCEPTED
    assert shared.status == URLStatus.ACCEPTED
    assert archive_only.status == URLStatus.NO_SENTENCES
    assert linked.status == URLStatus.ACCEPTED

    # The sitemap's future lastmod is retained as a hint, but HTML publication
    # metadata remains authoritative for the article date.
    parent_article = db_session.query(Article).filter(Article.url_id == parent.url_id).one()
    assert parent_article.publication_date == datetime(2026, 8, 11, 9, 0)
    assert parent_article.date_source == "html_meta"
    assert parent.sitemap_lastmod_hint.year == 2030
    assert parent.date_hint_source == "html_meta"

    assert linked.discovery_method == "LINK"
    assert linked.discovery_depth == 1
    assert linked.frontier_priority == 120
    assert shared.discovery_method == "RSS"
    assert shared.frontier_priority == 120
    assert db_session.query(URL).count() == 4

    edges = db_session.query(URLDiscoveryEdge).filter(
        URLDiscoveryEdge.from_url_id == parent.url_id,
        URLDiscoveryEdge.discovery_method == "LINK",
    ).all()
    assert len(edges) == 3
    assert len({edge.to_url_id for edge in edges}) == 3
    assert {edge.to_url_id for edge in edges} == {
        shared.url_id,
        archive_only.url_id,
        linked.url_id,
    }

    link_event = db_session.query(CrawlEvent).filter(
        CrawlEvent.url == parent.url,
        CrawlEvent.event_type == "discovery:link_batch",
    ).one()
    link_payload = json.loads(link_event.error_message)
    assert link_payload["new_urls"] == 1
    assert link_payload["new_edges"] == 3
    assert link_payload["rejection_reasons"] == {
        "host_mismatch": 1,
        "non_article_asset": 1,
        "query_parameters_disallowed": 1,
        "self_link": 1,
        "unsupported_scheme": 1,
    }

    archive_article = db_session.query(Article).filter(Article.url_id == archive_only.url_id).one()
    assert archive_article.sentence_count == 0
    assert json.loads(archive_only.extraction_diagnostics)["reason"] == "no_sentences"
    parent_diagnostics = json.loads(parent.extraction_diagnostics)
    assert parent_diagnostics["breakdown"]["duplicate_batch"] == 1

    shared_article = db_session.query(Article).filter(Article.url_id == shared.url_id).one()
    assert linked.content_hash == shared.content_hash
    assert linked.article is None
    assert db_session.query(Article).count() == 3
    assert db_session.query(Sentence).count() == 3
    assert db_session.query(Sentence.content_hash).distinct().count() == 3

    counts_before_rerun = {
        "urls": db_session.query(URL).count(),
        "articles": db_session.query(Article).count(),
        "sentences": db_session.query(Sentence).count(),
        "edges": db_session.query(URLDiscoveryEdge).count(),
    }
    requested_before_rerun = len(requested_urls)

    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        restarted_discovery = DiscoveryEngine(
            db_session=db_session,
            user_agent="Stage8TestBot/1.0",
            date_cutoff=datetime(2022, 1, 1),
            timeout=1,
            max_sitemap_depth=5,
            max_sitemap_documents=10,
            max_sitemap_roots=5,
            max_candidates=20,
            robots_mgr=None,
        )
        rerun_report = await restarted_discovery.discover_source_async(source_cfg, client=client)
        assert rerun_report.queued_new == 0
        assert rerun_report.actually_queued == 0
        assert rerun_report.already_stored == rerun_report.accepted_unique
        rerun_report.reconcile()

        restarted_archive = ArchiveDiscoveryEngine(
            db_session=db_session,
            user_agent="Stage8TestBot/1.0",
            date_cutoff=datetime(2022, 1, 1),
            timeout=1,
            apply_delay=False,
        )
        rerun_archive = await restarted_archive.discover_source_async(
            source_cfg,
            client=client,
            from_date=date(2026, 8, 11),
            to_date=date(2026, 8, 11),
            max_periods_override=1,
            max_pages_override=2,
        )
        assert rerun_archive["links_added"] == 0

    restarted_pipeline = CrawlPipeline(db_session, _config(tmp_path / "restart"), "Stage8TestBot/1.0")
    restarted_pipeline.robots_mgr = _FixtureRobots()
    restarted_pipeline._source_config = lambda _source_id: source_cfg
    restarted_fetcher = HTTPFetcher("Stage8TestBot/1.0", cache=None, timeout=1, max_retries=1)
    restarted_fetcher.client = httpx.AsyncClient(transport=transport, follow_redirects=True)
    restarted_pipeline.fetcher = restarted_fetcher
    await restarted_pipeline.crawl_queued_urls(source_id=source_cfg.id)

    assert len(requested_urls) > requested_before_rerun
    assert set(requested_urls).issubset(fixture_urls)
    assert counts_before_rerun == {
        "urls": db_session.query(URL).count(),
        "articles": db_session.query(Article).count(),
        "sentences": db_session.query(Sentence).count(),
        "edges": db_session.query(URLDiscoveryEdge).count(),
    }


def test_stage8a_link_rejection_contract_is_stable():
    source_cfg = _source()
    batch = LinkDiscoveryEngine().analyze_html(
        _fixture("parent.html").decode("utf-8"),
        final_url="https://example.com/news/2026/08/11/100/parent-story",
        requested_url="https://example.com/news/2026/08/11/100/parent-story",
        source_config=source_cfg,
        explicit_limits={"max_depth": 1},
    )
    assert batch.rejection_reasons == {
        "host_mismatch": 1,
        "non_article_asset": 1,
        "query_parameters_disallowed": 1,
        "self_link": 1,
        "unsupported_scheme": 1,
    }
    assert batch.counters["accepted_unique"] == 3
    assert batch.counters["unique_normalized"] == 7
    assert batch.counters["accepted_unique"] + batch.counters["rejected_unique"] == batch.counters["unique_normalized"]


def test_stage8a_tripwire_has_not_been_bypassed():
    with pytest.raises(AssertionError, match="Offline test tripwire"):
        import socket

        socket.create_connection(("example.com", 80), timeout=0.01)
