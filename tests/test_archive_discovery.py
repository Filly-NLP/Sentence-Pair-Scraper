import asyncio
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock
import httpx
import pytest

from src.crawler.archive_discovery import ArchiveDiscoveryEngine
from src.crawler.robots import RobotsManager
from src.sources.registry import ArchiveConfig, SourceConfig, SourceRegistry
from src.storage.models import Source, URL


class _MockResponse:
    def __init__(self, text: str, status_code: int = 200, headers: dict = None):
        self.text = text
        self.content = text.encode("utf-8")
        self.status_code = status_code
        self.headers = headers or {}


def test_independent_year_month_day_period_generation():
    # Day granularity
    day_periods = list(
        ArchiveDiscoveryEngine.generate_periods(
            granularity="day",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 3),
        )
    )
    assert len(day_periods) == 3
    assert day_periods[0][1]["yyyy"] == "2026"
    assert day_periods[0][1]["mm"] == "08"
    assert day_periods[0][1]["dd"] == "01"
    assert day_periods[2][1]["dd"] == "03"

    # Month granularity
    month_periods = list(
        ArchiveDiscoveryEngine.generate_periods(
            granularity="month",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 3, 1),
        )
    )
    assert len(month_periods) == 3
    assert [p[1]["mm"] for p in month_periods] == ["01", "02", "03"]

    # Year granularity
    year_periods = list(
        ArchiveDiscoveryEngine.generate_periods(
            granularity="year",
            start_date=date(2024, 1, 1),
            end_date=date(2026, 1, 1),
        )
    )
    assert len(year_periods) == 3
    assert [p[1]["yyyy"] for p in year_periods] == ["2024", "2025", "2026"]


def test_next_link_pagination_and_source_scope(monkeypatch):
    fixtures_dir = Path("tests/fixtures/archives")
    page_1 = (fixtures_dir / "sample_archive_page.html").read_text(encoding="utf-8")
    page_2 = (fixtures_dir / "sample_archive_page_2.html").read_text(encoding="utf-8")

    responses = {
        "https://www.abante.com.ph/2026/08/11/": _MockResponse(page_1),
        "https://www.abante.com.ph/2026/08/11/page/2/": _MockResponse(page_2),
    }

    def mock_get(url, **kwargs):
        return responses[url]

    monkeypatch.setattr("src.crawler.archive_discovery.httpx.get", mock_get)

    source = SourceConfig(
        id="abante",
        name="Abante",
        domain="www.abante.com.ph",
        enabled=True,
        article_path_patterns=[r"^/20\d{2}/\d{2}/\d{2}/[^/]+/?$"],
        archive=ArchiveConfig(
            enabled=True,
            granularity="day",
            start_date="2026-08-11",
            end_date="2026-08-11",
            article_link_selector="h2.entry-title a",
            pagination_mode="next_link",
            next_link_selector="a.next",
            max_pages_per_period=5,
        ),
    )

    engine = ArchiveDiscoveryEngine(
        db_session=None,
        user_agent="TestBot/1.0",
        date_cutoff=datetime(2020, 1, 1),
        apply_delay=False,
    )

    report = engine.discover_source(source)

    assert report["periods_requested"] == 1
    assert report["pages_fetched"] == 2
    assert report["links_found"] == 4
    # 2 matching in-scope articles on page 1, 1 on page 2; category url is rejected
    assert report["links_added"] == 3
    assert report["out_of_scope_skipped"] == 1
    assert "https://www.abante.com.ph/category/news" in [u.rstrip("/") for u in report["sample_out_of_scope"]]


def test_pagination_loop_detection(monkeypatch):
    # Loop scenario: next page points back to page 1
    looping_html = """
    <html><body>
      <h2 class="entry-title"><a href="https://example.com/2026/08/11/post-1">Post 1</a></h2>
      <a class="next" href="https://example.com/2026/08/11/page/1">Next</a>
    </body></html>
    """

    def mock_get(url, **kwargs):
        return _MockResponse(looping_html)

    monkeypatch.setattr("src.crawler.archive_discovery.httpx.get", mock_get)

    source = SourceConfig(
        id="test",
        name="Test",
        domain="example.com",
        enabled=True,
        article_path_patterns=[r"^/20\d{2}/\d{2}/\d{2}/[^/]+$"],
        archive=ArchiveConfig(
            enabled=True,
            granularity="day",
            url_template="https://example.com/{yyyy}/{mm}/{dd}/page/1",
            pagination_mode="next_link",
            next_link_selector="a.next",
            max_pages_per_period=10,
        ),
    )

    events = []
    engine = ArchiveDiscoveryEngine(
        db_session=None,
        user_agent="TestBot/1.0",
        date_cutoff=datetime(2020, 1, 1),
        event_logger=lambda et, msg=None, stat=None: events.append((et, msg)),
        apply_delay=False,
    )

    report = engine.discover_source(
        source,
        from_date=date(2026, 8, 11),
        to_date=date(2026, 8, 11),
    )

    assert report["pages_fetched"] == 1
    assert report["loops_detected"] == 1
    assert any(et == "discovery:pagination_loop" for et, _ in events)


def test_robots_disallowed_archive_page_is_blocked(monkeypatch):
    mgr = MagicMock(spec=RobotsManager)
    mgr.is_allowed.return_value = False

    fetched = []
    def mock_get(url, **kwargs):
        fetched.append(url)
        return _MockResponse("<html></html>")

    monkeypatch.setattr("src.crawler.archive_discovery.httpx.get", mock_get)

    source = SourceConfig(
        id="test",
        name="Test",
        domain="example.com",
        enabled=True,
        archive=ArchiveConfig(
            enabled=True,
            granularity="day",
            start_date="2026-08-11",
            end_date="2026-08-11",
        ),
    )

    events = []
    engine = ArchiveDiscoveryEngine(
        db_session=None,
        user_agent="TestBot/1.0",
        date_cutoff=datetime(2020, 1, 1),
        robots_mgr=mgr,
        event_logger=lambda et, msg=None, stat=None: events.append((et, msg)),
        apply_delay=False,
    )

    report = engine.discover_source(source)

    assert report["pages_fetched"] == 0
    assert report["pages_blocked"] == 1
    assert len(fetched) == 0
    assert any(et == "skipped:robots_disallowed" for et, _ in events)


def test_seven_day_dry_run_scope_without_network_or_db_writes():
    periods = list(
        ArchiveDiscoveryEngine.generate_periods(
            granularity="day",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 7),
            max_periods=10,
        )
    )
    assert len(periods) == 7

    source = SourceConfig(
        id="abante",
        name="Abante",
        domain="www.abante.com.ph",
        enabled=True,
        archive=ArchiveConfig(enabled=False, granularity="day"),
    )
    engine = ArchiveDiscoveryEngine(
        db_session=None,
        user_agent="TestBot/1.0",
        date_cutoff=datetime(2020, 1, 1),
        apply_delay=False,
    )

    urls = [
        engine._format_period_urls(source.archive, source, ctx)[0]
        for _, ctx in periods
    ]
    assert len(urls) == 7
    assert urls[0] == "https://www.abante.com.ph/2026/08/01/"
    assert urls[6] == "https://www.abante.com.ph/2026/08/07/"


def test_all_real_sources_are_archive_disabled_by_default():
    registry = SourceRegistry(Path("config/sources.yaml"))
    for source in registry.list_sources():
        if source.archive is not None:
            assert source.archive.enabled is False


@pytest.mark.asyncio
async def test_cancellation_rolls_back_staged_candidates_and_closes_owned_client(
    db_session, monkeypatch
):
    source = SourceConfig(
        id="cancel",
        name="Cancellation",
        domain="example.com",
        archive=ArchiveConfig(
            enabled=True,
            granularity="day",
            url_template="https://example.com/archive/{yyyy}/{mm}/{dd}/",
            article_link_selector="a.article-link",
            pagination_mode="page_template",
            page_template="{url}?page={page}",
            max_pages_per_period=2,
        ),
        article_path_patterns=[r"^/2026/08/11/[^/]+$"],
    )
    db_session.add(Source(source_id=source.id, name=source.name, domain=source.domain))
    db_session.commit()

    staged = asyncio.Event()
    engine = ArchiveDiscoveryEngine(
        db_session=db_session,
        user_agent="OfflineTestBot/1.0",
        date_cutoff=datetime(2022, 1, 1),
        apply_delay=False,
    )
    original_extract_links = engine._extract_links

    def extract_links_after_stage(html, page_url, selector):
        links = original_extract_links(html, page_url, selector)

        def staged_links():
            for link in links:
                yield link
            staged.set()

        return staged_links()

    engine._extract_links = extract_links_after_stage
    page = "<a class='article-link' href='https://example.com/2026/08/11/one'>One</a>"

    class ArchiveTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.params.get("page") == "2":
                await asyncio.sleep(0)
            return httpx.Response(
                200,
                content=page.encode("utf-8"),
                headers={"content-type": "text/html"},
                request=request,
            )

    owned_client = httpx.AsyncClient(transport=ArchiveTransport())
    monkeypatch.setattr(
        "src.crawler.archive_discovery.httpx.AsyncClient",
        lambda **_kwargs: owned_client,
    )
    archive_task = asyncio.create_task(
        engine.discover_source_async(
            source,
            from_date=date(2026, 8, 11),
            to_date=date(2026, 8, 11),
            max_pages_override=2,
        )
    )

    async def cancel_after_stage():
        await staged.wait()
        archive_task.cancel()

    cancellation_task = asyncio.create_task(cancel_after_stage())
    with pytest.raises(asyncio.CancelledError):
        await archive_task
    await cancellation_task

    assert owned_client.is_closed
    assert db_session.query(URL).count() == 0
    monkeypatch.undo()

    # A fresh run inserts the staged URL exactly once, and a further rerun is
    # duplicate-free.
    rerun_page = "<a class='article-link' href='https://example.com/2026/08/11/one'>One</a>"

    def rerun_handler(request):
        return httpx.Response(
            200,
            content=rerun_page.encode("utf-8"),
            headers={"content-type": "text/html"},
            request=request,
        )

    rerun_transport = httpx.MockTransport(rerun_handler)
    async with httpx.AsyncClient(transport=rerun_transport) as client:
        rerun = ArchiveDiscoveryEngine(
            db_session=db_session,
            user_agent="OfflineTestBot/1.0",
            date_cutoff=datetime(2022, 1, 1),
            apply_delay=False,
        )
        report = await rerun.discover_source_async(
            source,
            client=client,
            from_date=date(2026, 8, 11),
            to_date=date(2026, 8, 11),
            max_pages_override=1,
        )
        assert report["links_added"] == 1

    async with httpx.AsyncClient(transport=rerun_transport) as client:
        second = ArchiveDiscoveryEngine(
            db_session=db_session,
            user_agent="OfflineTestBot/1.0",
            date_cutoff=datetime(2022, 1, 1),
            apply_delay=False,
        )
        report = await second.discover_source_async(
            source,
            client=client,
            from_date=date(2026, 8, 11),
            to_date=date(2026, 8, 11),
            max_pages_override=1,
        )
        assert report["links_added"] == 0
    assert db_session.query(URL).count() == 1
