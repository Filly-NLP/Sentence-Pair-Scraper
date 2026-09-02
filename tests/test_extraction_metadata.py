from src.extraction.base import ArticleExtractor
from src.sources.registry import ExtractionConfig, SourceConfig


def test_jsonld_graph_and_configured_date_selector():
    source = SourceConfig(
        id="test", name="Test", domain="example.com", enabled=True, language="filipino",
        extraction=ExtractionConfig(type="generic", content_selector=".article", date_selector=".published"),
    )
    html = '''<html><head>
      <script type="application/ld+json">{"@graph":[{"@type":"WebPage"},
      {"@type":"NewsArticle","headline":"Balita","datePublished":"2026-08-11T12:00:00+08:00"}]}</script>
      </head><body><main class="article"><p>Unang talata ng balita.</p></main></body></html>'''
    result = ArticleExtractor.extract(html, source)
    assert result["headline"] == "Balita"
    assert result["publication_date_raw"].startswith("2026-08-11")
    assert result["publication_date_source"] == "jsonld"
    assert result["article_text"] == "Unang talata ng balita."


def test_empty_first_selector_does_not_hide_populated_selector():
    source = SourceConfig(
        id="test", name="Test", domain="example.com", enabled=True, language="filipino",
        extraction=ExtractionConfig(type="generic", content_selector=".empty, .article"),
    )
    html = '<div class="empty"></div><article class="article"><p>May laman ang artikulong ito.</p></article>'
    result = ArticleExtractor.extract(html, source)
    assert result["body_valid"] is True
    assert "May laman" in result["article_text"]
