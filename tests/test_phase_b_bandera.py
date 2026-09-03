import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import pytest
from bs4 import BeautifulSoup

from src.crawler.archive_discovery import ArchiveDiscoveryEngine
from src.extraction.base import ArticleExtractor
from src.extraction.date_filter import DateFilter
from src.sentence.segmenter import SentenceSegmenter
from src.sources.registry import (
    ArchiveConfig,
    ExtractionConfig,
    SourceConfig,
    SourceRegistry,
)


RAW_EVIDENCE_DIR = Path(
    os.environ.get(
        "PHASE_B_RAW_EVIDENCE_DIR",
        r"C:\Users\Dominic\.gemini\antigravity-ide\brain\8c83375a-0d3e-47e2-8242-2d5734b8d420\raw_evidence",
    )
)
RUN_CONFIG_DIR = os.environ.get("PHASE_B_CONFIG_DIR")
SCOPED_SELECTOR = "#article-content-wrap #article-content"


def raw_artifact(name: str) -> Path:
    path = RAW_EVIDENCE_DIR / name
    if not path.is_file():
        pytest.skip(f"raw evidence artifact not available: {path}")
    return path


def bandera_source(*, archive: ArchiveConfig | None = None, selector: str = SCOPED_SELECTOR) -> SourceConfig:
    return SourceConfig(
        id="bandera",
        name="Bandera",
        domain="bandera.inquirer.net",
        enabled=True,
        article_path_patterns=[r"^/\d+/[^/]+/?$"],
        archive=archive,
        extraction=ExtractionConfig(type="wordpress", content_selector=selector),
    )


def test_isolated_archive_schema_is_fail_closed_and_bounded():
    if not RUN_CONFIG_DIR:
        pytest.skip("PHASE_B_CONFIG_DIR is not set")

    registry = SourceRegistry(Path(RUN_CONFIG_DIR) / "sources.yaml")
    source = registry.get_source("bandera")
    assert source is not None
    assert source.archive is not None
    archive = source.archive
    assert archive.enabled is False
    assert archive.granularity == "day"
    assert archive.url_template == "https://www.inquirer.net/article-index/?d={yyyy}-{mm}-{dd}"
    assert archive.article_link_selector == "#index-wrap h4:nth-of-type(14) + ul a[href]"
    assert archive.allowed_hosts == ["bandera.inquirer.net"]
    assert archive.pagination_mode == "none"
    assert archive.max_periods == 7
    assert archive.max_pages_per_period == 1
    assert archive.max_total_pages == 7
    assert archive.max_response_bytes == 524288
    assert archive.max_candidates == 200
    assert all(
        other.archive is None or other.id == "bandera"
        for other in registry.list_sources()
    )


def test_archive_selector_scopes_www_root_to_only_bandera_candidates():
    archive = ArchiveConfig(
        enabled=False,
        granularity="day",
        url_template="https://www.inquirer.net/article-index/?d={yyyy}-{mm}-{dd}",
        article_link_selector="#index-wrap h4:nth-of-type(14) + ul a[href]",
        allowed_hosts=["bandera.inquirer.net"],
        pagination_mode="none",
        max_periods=7,
        max_pages_per_period=1,
        max_total_pages=7,
        max_response_bytes=524288,
        max_candidates=200,
    )
    source = bandera_source(archive=archive)
    html = raw_artifact("archive_2026-08-17.html").read_bytes().decode("utf-8", errors="replace")
    engine = ArchiveDiscoveryEngine(
        db_session=None,
        user_agent="PhaseBOfflineTest/1.0",
        date_cutoff=datetime(2022, 1, 1),
        apply_delay=False,
        persist_queue=False,
    )

    links = engine._extract_links(
        html,
        "https://www.inquirer.net/article-index/?d=2026-08-17",
        archive.article_link_selector,
    )

    assert len(links) == 20
    assert {urlparse(link).hostname for link in links} == {"bandera.inquirer.net"}
    assert all(source.accepts_url(link) for link in links)
    assert all(not urlparse(link).hostname == "www.inquirer.net" for link in links)
    assert list(ArchiveDiscoveryEngine.generate_periods(
        "day", datetime(2026, 8, 17).date(), datetime(2026, 8, 23).date(), 7
    ))
    assert len(list(ArchiveDiscoveryEngine.generate_periods(
        "day", datetime(2026, 8, 17).date(), datetime(2026, 8, 23).date(), 7
    ))) == 7


@pytest.mark.parametrize(
    ("filename", "canonical", "body_chars", "paragraphs", "sentences"),
    [
        (
            "article_453976_dogshow_divas.html",
            "https://bandera.inquirer.net/453976/dogshow-divas-thankful-kay-joy-barcoma-we-will-always-be-rooting-for-you",
            1955,
            16,
            20,
        ),
        (
            "article_453955_sen_bam.html",
            "https://bandera.inquirer.net/453955/sen-bam-nangalampag-bakit-wala-pa-ring-batas-kontra-katiwalian",
            3193,
            17,
            26,
        ),
        (
            "article_453856_nanay_ni_sachzna.html",
            "https://bandera.inquirer.net/453856/nanay-ni-sachzna-laparan-rumesbak-kay-angelica-jones-wala-po-kaming-kinita",
            3224,
            19,
            24,
        ),
    ],
)
def test_scoped_selector_preserves_metadata_without_duplicate_body_inflation(
    filename: str,
    canonical: str,
    body_chars: int,
    paragraphs: int,
    sentences: int,
):
    html = raw_artifact(filename).read_bytes().decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "lxml")
    matches = soup.select(SCOPED_SELECTOR)
    assert len(matches) == 3, "raw capture honestly contains three duplicate #article-content matches"

    source = bandera_source()
    result = ArticleExtractor.extract(html, source)
    parsed_date = DateFilter(datetime(1970, 1, 1)).parse_date(result["publication_date_raw"])

    assert result["selector_match"] == SCOPED_SELECTOR
    assert result["extraction_method"] == "wordpress"
    assert result["canonical_url"] == canonical
    assert parsed_date is not None
    assert result["body_chars"] == body_chars
    assert result["paragraph_count"] == paragraphs
    assert len(SentenceSegmenter.split_sentences(result["article_text"])) == sentences
    assert "IBA PANG PASABOG" not in result["article_text"]
    assert "Disclaimer: The comments uploaded" not in result["article_text"]


def test_current_bandera_selector_is_a_mismatch_against_raw_captures():
    html = raw_artifact("article_453976_dogshow_divas.html").read_bytes().decode(
        "utf-8", errors="replace"
    )
    result = ArticleExtractor.extract(
        html,
        bandera_source(selector=".entry-content, #article_content"),
    )
    assert result["selector_match"] == "article|body"
    assert result["extraction_method"] == "generic-fallback"
