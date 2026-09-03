import tempfile
import pytest
from pathlib import Path

from src.crawler.config import CrawlerConfig, CrawlerConfigValidationError
from src.sources.registry import SourceRegistry, ConfigValidationError


def test_valid_source_config():
    with tempfile.TemporaryDirectory() as tmpdir:
        sources_yaml = Path(tmpdir) / "sources.yaml"
        sources_yaml.write_text(
            r"""
sources:
  - id: test_source
    name: Test News
    domain: example.com
    enabled: true
    language: filipino
    crawl_delay_seconds: 2
    max_concurrent: 1
    article_path_patterns:
      - '^/news/\d+/[^/]+/?$'
    rss:
      - url: https://example.com/feed
        category: general
    sitemap:
      - url: https://example.com/sitemap.xml
    archive:
      enabled: false
      start_date: "2022-01-01"
      end_date: "2023-01-01"
      pagination_limit: 10
""",
            encoding="utf-8",
        )
        registry = SourceRegistry(sources_yaml)
        src = registry.get_source("test_source")
        assert src is not None
        assert src.name == "Test News"
        assert src.domain == "example.com"
        assert src.crawl_delay_seconds == 2
        assert src.max_concurrent == 1
        assert src.archive is not None
        assert src.archive.start_date == "2022-01-01"


def test_invalid_source_regex_fails_precisely():
    with tempfile.TemporaryDirectory() as tmpdir:
        sources_yaml = Path(tmpdir) / "sources.yaml"
        sources_yaml.write_text(
            r"""
sources:
  - id: broken_regex_source
    name: Broken Regex
    domain: example.com
    article_path_patterns:
      - '^/news/(?P<id>\d+'
""",
            encoding="utf-8",
        )
        with pytest.raises(ConfigValidationError) as excinfo:
            SourceRegistry(sources_yaml)
        msg = str(excinfo.value)
        assert "broken_regex_source" in msg
        assert "article_path_patterns" in msg


def test_invalid_source_domain_fails():
    with tempfile.TemporaryDirectory() as tmpdir:
        sources_yaml = Path(tmpdir) / "sources.yaml"
        sources_yaml.write_text(
            """
sources:
  - id: broken_domain
    name: Broken Domain
    domain: https://example.com/path
""",
            encoding="utf-8",
        )
        with pytest.raises(ConfigValidationError) as excinfo:
            SourceRegistry(sources_yaml)
        msg = str(excinfo.value)
        assert "broken_domain" in msg
        assert "domain" in msg


def test_invalid_source_delay_and_concurrency():
    with tempfile.TemporaryDirectory() as tmpdir:
        sources_yaml = Path(tmpdir) / "sources.yaml"
        sources_yaml.write_text(
            """
sources:
  - id: bad_delay
    name: Bad Delay
    domain: example.com
    crawl_delay_seconds: -5
""",
            encoding="utf-8",
        )
        with pytest.raises(ConfigValidationError) as excinfo:
            SourceRegistry(sources_yaml)
        assert "bad_delay" in str(excinfo.value)
        assert "crawl_delay_seconds" in str(excinfo.value)

        sources_yaml.write_text(
            """
sources:
  - id: bad_concurrent
    name: Bad Concurrency
    domain: example.com
    max_concurrent: 0
""",
            encoding="utf-8",
        )
        with pytest.raises(ConfigValidationError) as excinfo:
            SourceRegistry(sources_yaml)
        assert "bad_concurrent" in str(excinfo.value)
        assert "max_concurrent" in str(excinfo.value)


def test_invalid_archive_date_range():
    with tempfile.TemporaryDirectory() as tmpdir:
        sources_yaml = Path(tmpdir) / "sources.yaml"
        sources_yaml.write_text(
            """
sources:
  - id: bad_archive
    name: Bad Archive
    domain: example.com
    archive:
      start_date: "2023-01-01"
      end_date: "2022-01-01"
""",
            encoding="utf-8",
        )
        with pytest.raises(ConfigValidationError) as excinfo:
            SourceRegistry(sources_yaml)
        assert "bad_archive" in str(excinfo.value)
        assert "archive" in str(excinfo.value)


def test_crawler_config_validation():
    with tempfile.TemporaryDirectory() as tmpdir:
        crawler_yaml = Path(tmpdir) / "crawler.yaml"
        crawler_yaml.write_text(
            """
crawler:
  date_cutoff: "invalid-date"
""",
            encoding="utf-8",
        )
        with pytest.raises(CrawlerConfigValidationError) as excinfo:
            CrawlerConfig(crawler_yaml)
        assert "date_cutoff" in str(excinfo.value)

        crawler_yaml.write_text(
            """
sentence:
  min_tokens: 10
  max_tokens: 5
""",
            encoding="utf-8",
        )
        with pytest.raises(CrawlerConfigValidationError) as excinfo:
            CrawlerConfig(crawler_yaml)
        assert "max_tokens" in str(excinfo.value)


def test_checked_in_stage8_runtime_is_fail_closed():
    project_root = Path(__file__).resolve().parents[1]
    crawler = CrawlerConfig(project_root / "config" / "crawler.yaml")
    sources = SourceRegistry(project_root / "config" / "sources.yaml")

    assert crawler.get("discovery.diagnostics.enabled") is False
    assert crawler.get("link_discovery.enabled") is False
    assert all(not source.link_discovery.enabled for source in sources.list_sources())
    assert all(
        source.archive is None or not source.archive.enabled
        for source in sources.list_sources()
    )
    assert crawler.get("extraction.fallback.trafilatura.enabled") is False
    assert all(
        source.extraction is None
        or not source.extraction.trafilatura.enabled
        for source in sources.list_sources()
    )

    # Optional providers may be added later, but checked-in configuration may
    # not silently turn on a runtime path before its rollout is approved.
    for path in (
        "common_crawl",
        "trafilatura",
        "warc",
        "extraction.trafilatura",
        "storage.warc",
    ):
        section = crawler.get(path)
        assert section is None or (
            isinstance(section, dict) and section.get("enabled") is False
        ), f"optional provider must be absent or explicitly disabled: {path}"


def test_optional_stage_config_validation_and_defaults(tmp_path):
    crawler_yaml = tmp_path / "crawler.yaml"
    crawler_yaml.write_text(
        """
extraction:
  fallback:
    trafilatura:
      enabled: true
      trigger: empty_or_short_primary
      min_body_chars: 250
      favor_precision: false
warc:
  enabled: true
  directory: data/warc-test
  max_response_bytes: 100
  rotate_bytes: 200
  max_total_bytes_per_run: 300
  max_disk_bytes: 400
  retention:
    mode: manual
""",
        encoding="utf-8",
    )
    config = CrawlerConfig(crawler_yaml)
    assert config.get("extraction.fallback.trafilatura.enabled") is True
    assert config.get("warc.max_disk_bytes") == 400

    crawler_yaml.write_text("warc:\n  directory: ../outside\n", encoding="utf-8")
    with pytest.raises(CrawlerConfigValidationError, match="warc.directory"):
        CrawlerConfig(crawler_yaml)
