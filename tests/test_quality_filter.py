from src.sentence.quality_filter import SentenceQualityFilter

def test_quality_filter_valid():
    filter_mgr = SentenceQualityFilter()
    assert filter_mgr.is_clean("Pumunta siya sa palengke kahapon para bumili ng masarap na gulay.") is True

def test_quality_filter_too_short():
    filter_mgr = SentenceQualityFilter(min_tokens=5)
    assert filter_mgr.is_clean("Salamat po.") is False

def test_quality_filter_urls_and_emails():
    filter_mgr = SentenceQualityFilter()
    assert filter_mgr.is_clean("I-download ang file sa https://www.abante.com.ph para makita.") is False
    assert filter_mgr.is_clean("I-email sa contact@example.com para sa detalye.") is False

def test_quality_filter_navigation_noise():
    filter_mgr = SentenceQualityFilter()
    assert filter_mgr.is_clean("Basahin din: Mga bagong balita ngayong Martes.") is False
    assert filter_mgr.is_clean("Copyright 2026 by Abante News.") is False

def test_quality_filter_html_leftovers():
    filter_mgr = SentenceQualityFilter()
    assert filter_mgr.is_clean("<div>Magandang umaga sa lahat.</div>") is False
