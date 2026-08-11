from datetime import datetime
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
