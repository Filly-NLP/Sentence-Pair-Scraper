from datetime import datetime, timedelta, timezone
from src.extraction.date_filter import DateFilter

def test_date_filter_boundary():
    cutoff = datetime(2022, 1, 1)
    filter_mgr = DateFilter(cutoff)
    
    assert filter_mgr.is_valid(datetime(2022, 1, 1)) is True
    assert filter_mgr.is_valid(datetime(2022, 1, 2)) is True
    assert filter_mgr.is_valid(datetime(2021, 12, 31, 23, 59, 59)) is False

def test_parse_iso_date():
    filter_mgr = DateFilter(datetime(2022, 1, 1))
    
    parsed = filter_mgr.parse_date("2026-08-11T12:00:00+08:00")
    assert parsed is not None
    assert parsed.year == 2026
    
    parsed_utc = filter_mgr.parse_date("2026-08-11T12:00:00Z")
    assert parsed_utc is not None
    assert parsed_utc.year == 2026

def test_parse_rss_date():
    filter_mgr = DateFilter(datetime(2022, 1, 1))
    
    # RFC 2822
    parsed = filter_mgr.parse_date("Tue, 11 Aug 2026 12:00:00 +0800")
    assert parsed is not None
    assert parsed.year == 2026
    assert parsed.month == 8
    assert parsed.day == 11


def test_parse_url_date_fallback():
    parsed = DateFilter.parse_date_from_url("https://example.com/news/2022/01/05/story")
    assert parsed == datetime(2022, 1, 5)


def test_timezone_aware_rss_dates_use_absolute_cutoff():
    filter_mgr = DateFilter(datetime(2022, 1, 1))
    old = datetime(2021, 12, 31, 23, 59, tzinfo=timezone.utc)
    boundary = datetime(2022, 1, 1, 8, tzinfo=timezone(timedelta(hours=8)))
    assert filter_mgr.validation_reason(old) == "date_before_cutoff"
    assert filter_mgr.validation_reason(boundary) == "accepted"
    assert filter_mgr.validate_raw("not-a-date")[1] == "date_invalid"
