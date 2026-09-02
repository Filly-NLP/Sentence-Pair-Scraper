from datetime import datetime, timezone, timedelta
from src.extraction.date_filter import DateFilter, DateEvidence


def test_html_meta_has_highest_precedence():
    date_filter = DateFilter(cutoff_date=datetime(2022, 1, 1))
    candidates = [
        DateEvidence(value=datetime(2023, 6, 1), source="url", confidence=0.75),
        DateEvidence(value=datetime(2024, 1, 15), source="html_jsonld", confidence=1.0),
        DateEvidence(value=datetime(2023, 12, 1), source="rss", confidence=0.85),
        DateEvidence(value=datetime(2024, 2, 1), source="lastmod", confidence=0.5),
    ]
    resolved, reason, source, conf, diag = date_filter.resolve_publication_evidence(candidates)
    assert reason == "accepted"
    assert resolved == datetime(2024, 1, 15)
    assert source == "html_jsonld"
    assert conf == 1.0


def test_news_sitemap_precedence_over_rss_and_url_when_html_missing():
    date_filter = DateFilter(cutoff_date=datetime(2022, 1, 1))
    candidates = [
        DateEvidence(value=datetime(2023, 5, 10), source="url", confidence=0.75),
        DateEvidence(value=datetime(2023, 5, 20), source="rss", confidence=0.85),
        DateEvidence(value=datetime(2023, 5, 25), source="news_sitemap", confidence=0.90),
    ]
    resolved, reason, source, conf, diag = date_filter.resolve_publication_evidence(candidates)
    assert reason == "accepted"
    assert resolved == datetime(2023, 5, 25)
    assert source == "news_sitemap"
    assert conf == 0.90


def test_rss_precedence_over_url_and_archive_period():
    date_filter = DateFilter(cutoff_date=datetime(2022, 1, 1))
    candidates = [
        DateEvidence(value=datetime(2023, 1, 1), source="archive_period", confidence=0.70),
        DateEvidence(value=datetime(2023, 3, 15), source="url", confidence=0.75),
        DateEvidence(value=datetime(2023, 3, 18), source="rss", confidence=0.85),
    ]
    resolved, reason, source, conf, diag = date_filter.resolve_publication_evidence(candidates)
    assert reason == "accepted"
    assert resolved == datetime(2023, 3, 18)
    assert source == "rss"


def test_url_fallback_when_other_sources_missing():
    date_filter = DateFilter(cutoff_date=datetime(2022, 1, 1))
    url_date = DateFilter.parse_date_from_url("https://example.com/2024/07/04/headline-here")
    assert url_date == datetime(2024, 7, 4)

    candidates = [
        DateEvidence(value=url_date, source="url", confidence=0.75),
    ]
    resolved, reason, source, conf, diag = date_filter.resolve_publication_evidence(candidates)
    assert reason == "accepted"
    assert resolved == datetime(2024, 7, 4)
    assert source == "url"


def test_lastmod_alone_is_ignored_by_default():
    date_filter = DateFilter(cutoff_date=datetime(2022, 1, 1))
    candidates = [
        DateEvidence(value=datetime(2024, 1, 1), source="lastmod", confidence=0.50),
    ]
    resolved, reason, source, conf, diag = date_filter.resolve_publication_evidence(
        candidates,
        allow_lastmod_as_publication=False,
    )
    # lastmod must not masquerade as publication date
    assert resolved is None
    assert reason == "date_missing"


def test_lastmod_used_only_when_explicitly_configured():
    date_filter = DateFilter(cutoff_date=datetime(2022, 1, 1))
    candidates = [
        DateEvidence(value=datetime(2024, 1, 1), source="lastmod", confidence=0.50),
    ]
    resolved, reason, source, conf, diag = date_filter.resolve_publication_evidence(
        candidates,
        allow_lastmod_as_publication=True,
    )
    assert reason == "accepted"
    assert resolved == datetime(2024, 1, 1)
    assert source == "lastmod"


def test_impossible_future_date_rejection():
    date_filter = DateFilter(cutoff_date=datetime(2022, 1, 1))
    future_date = datetime.now(timezone.utc) + timedelta(days=400)
    candidates = [
        DateEvidence(value=future_date, source="html_meta", confidence=1.0),
    ]
    resolved, reason, source, conf, diag = date_filter.resolve_publication_evidence(candidates)
    assert resolved is None
    assert reason == "date_in_future"


def test_material_disagreement_detection():
    date_filter = DateFilter(cutoff_date=datetime(2022, 1, 1))
    candidates = [
        DateEvidence(value=datetime(2024, 6, 1), source="html_meta", confidence=1.0),
        DateEvidence(value=datetime(2023, 1, 1), source="url", confidence=0.75),
    ]
    resolved, reason, source, conf, diag = date_filter.resolve_publication_evidence(candidates, disagreement_threshold_days=30)
    assert reason == "accepted"
    assert resolved == datetime(2024, 6, 1)
    assert len(diag["disagreements"]) == 1
    assert diag["disagreements"][0]["days_difference"] > 300
