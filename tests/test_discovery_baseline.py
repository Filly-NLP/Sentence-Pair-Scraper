import json
from datetime import datetime
from pathlib import Path

from src.crawler.discovery import DiscoveryEngine
from src.sources.registry import SourceConfig


FIXTURES = Path(__file__).parent / "fixtures" / "discovery"


def test_baseline_fixture_reconciles_156_candidates():
    expected = json.loads((FIXTURES / "baseline_expected.json").read_text(encoding="utf-8"))
    source = SourceConfig(
        id="baseline",
        name="Baseline",
        domain="example.com",
        article_path_patterns=[r"^/article/\d+$"],
    )
    candidates = [
        {"url": f"https://example.com/article/{index}", "method": "RSS"}
        for index in range(58)
    ] + [
        {"url": f"https://example.com/section/{index}", "method": "RSS"}
        for index in range(98)
    ]
    engine = DiscoveryEngine(
        db_session=None,
        user_agent="OfflineTestBot/1.0",
        date_cutoff=datetime(2022, 1, 1),
    )
    report = engine._new_report(source)
    engine._active_persist_queue = False
    engine._queue_urls(source, candidates, set(), report)
    report.reconcile()

    assert report["candidates_seen"] == expected["candidates_seen"]
    assert report["normalized_unique"] == expected["normalized_unique"]
    assert report["accepted_unique"] == expected["accepted_unique"]
    assert report["rejected_unique"] == expected["rejected_unique"]


def test_baseline_source_concentrations_are_fixtured():
    expected = json.loads((FIXTURES / "baseline_expected.json").read_text(encoding="utf-8"))
    assert expected["pang_masa"] == {"candidates_seen": 10, "rejected_unique": 8}
    assert expected["gma_filipino"] == {"candidates_seen": 105, "rejected_unique": 90}
