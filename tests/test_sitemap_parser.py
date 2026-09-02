import gzip
from pathlib import Path

from src.crawler.sitemap_parser import SitemapParser


def test_parse_sitemap_index_and_timezone_safe_lastmod():
    xml = b'''<?xml version="1.0"?><sitemapindex>
      <sitemap><loc>https://example.com/posts-1.xml.gz</loc></sitemap>
    </sitemapindex>'''
    parsed = SitemapParser.parse_document(xml)
    assert parsed["kind"] == "index"
    assert parsed["sitemaps"] == ["https://example.com/posts-1.xml.gz"]

    child = b'''<urlset xmlns:news="http://www.google.com/schemas/sitemap-news/0.9"><url><loc>https://example.com/a/</loc>
      <lastmod>2022-01-01T00:00:00+08:00</lastmod><news:publication><news:publication_date>2026-08-11T00:00:00Z</news:publication_date></news:publication></url></urlset>'''
    child_parsed = SitemapParser.parse_document(gzip.compress(child))
    assert child_parsed["kind"] == "urlset"
    assert child_parsed["urls"][0]["url"] == "https://example.com/a/"
    assert child_parsed["urls"][0]["lastmod"].tzinfo is not None
    assert child_parsed["urls"][0]["publication_date"].year == 2026


def test_parse_sitemap_from_fixtures():
    fixtures_dir = Path("tests/fixtures/sitemaps")
    index_xml = (fixtures_dir / "sample_index.xml").read_bytes()
    parsed_index = SitemapParser.parse_document(index_xml)
    assert parsed_index["kind"] == "index"
    assert len(parsed_index["sitemaps"]) == 2

    news_xml = (fixtures_dir / "sample_news.xml").read_bytes()
    parsed_news = SitemapParser.parse_document(news_xml)
    assert parsed_news["kind"] == "urlset"
    assert len(parsed_news["urls"]) == 1
    assert parsed_news["urls"][0]["publication_date"] is not None


def test_malformed_sitemap_is_isolated():
    parsed = SitemapParser.parse_document("not xml")
    assert parsed["kind"] in ("unknown", "malformed")
    assert parsed["error"] is not None
