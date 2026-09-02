from datetime import datetime

from src.crawler.archive_discovery import ArchiveDiscoveryEngine
from src.sources.registry import ArchiveConfig, SourceConfig


def test_disabled_or_missing_archive_fails_closed_without_http(monkeypatch):
    requests = []

    def forbidden_get(url, **kwargs):
        requests.append(url)
        raise AssertionError("archive guard allowed an HTTP request")

    monkeypatch.setattr("src.crawler.archive_discovery.httpx.get", forbidden_get)
    engine = ArchiveDiscoveryEngine(None, "OfflineTestBot/1.0", datetime(2022, 1, 1))
    disabled = SourceConfig(
        id="disabled", name="Disabled", domain="example.com",
        archive=ArchiveConfig(enabled=False, url_template="https://example.com/archive/{yyyy}"),
    )
    missing = SourceConfig(id="missing", name="Missing", domain="example.com")
    disabled_report = engine.discover_source(disabled)
    missing_report = engine.discover_source(missing)
    assert disabled_report["refused"] is True
    assert missing_report["refused"] is True
    assert disabled_report["pages_fetched"] == 0
    assert missing_report["pages_fetched"] == 0
    assert requests == []


def test_disabled_archive_override_requires_both_explicit_dates():
    source = SourceConfig(
        id="disabled", name="Disabled", domain="example.com",
        archive=ArchiveConfig(enabled=False),
    )
    engine = ArchiveDiscoveryEngine(
        None, "OfflineTestBot/1.0", datetime(2022, 1, 1),
        allow_disabled_override=True,
    )
    report = engine.discover_source(source)
    assert report["refused"] is True
    assert report["errors"][0]["reason"] == "disabled_in_config"
