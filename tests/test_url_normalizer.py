from src.crawler.url_normalizer import normalize_url

def test_normalize_url_utm_stripping():
    url = "https://example.com/balita?utm_source=facebook&utm_medium=feed&id=100"
    assert normalize_url(url) == "https://example.com/balita?id=100"

def test_normalize_url_fragments():
    url = "https://example.com/balita#comments-section"
    assert normalize_url(url) == "https://example.com/balita"

def test_normalize_url_lowercase_host():
    url = "HTTPS://WWW.Abante.COM.PH/balita"
    assert normalize_url(url) == "https://www.abante.com.ph/balita"

def test_normalize_url_sorted_queries():
    url = "https://example.com/balita?b=2&a=1"
    assert normalize_url(url) == "https://example.com/balita?a=1&b=2"

def test_normalize_url_empty():
    assert normalize_url("") == ""
    assert normalize_url(None) == ""
