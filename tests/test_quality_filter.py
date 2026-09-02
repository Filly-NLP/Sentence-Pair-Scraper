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

def test_quality_filter_quotes_and_headlines():
    # Quotes excluded by default
    q_filter = SentenceQualityFilter(include_quotes=False, include_headlines=False)
    assert q_filter.is_clean('"Magandang balita po ito," sabi ng opisyal.', is_quote=True) is False
    assert q_filter.is_clean('Nagsimula ang pagtitipon kaninang umaga sa Maynila.', is_headline=True) is False

    # Quotes and headlines included when configured
    q_filter_inc = SentenceQualityFilter(include_quotes=True, include_headlines=True)
    assert q_filter_inc.is_clean('"Magandang balita po ito," sabi ng opisyal.', is_quote=True) is True
    assert q_filter_inc.is_clean('Nagsimula ang pagtitipon kaninang umaga sa Maynila.', is_headline=True) is True

def test_quality_filter_custom_noise_patterns():
    custom_patterns = [r"^eksklusibo\b"]
    q_filter = SentenceQualityFilter(noise_patterns=custom_patterns)
    assert q_filter.is_clean("Eksklusibo: Bagong proyekto para sa bayan.") is False
    # Standard pattern not in custom patterns should now pass if clean
    assert q_filter.is_clean("Basahin din: Mga bagong balita ngayong Martes.") is True
