from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

from bs4 import BeautifulSoup


RUN_ID = "20260902T130828251Z"
RUN_DIR = Path(__file__).resolve().parent
REPO_ROOT = RUN_DIR.parents[2]
RAW_DIR = Path(
    r"C:\Users\Dominic\.gemini\antigravity-ide\brain\8c83375a-0d3e-47e2-8242-2d5734b8d420\raw_evidence"
)
MANIFEST = Path(r"C:\Users\Dominic\Downloads\raw_evidence_manifest.md")
SCOPED_SELECTOR = "#article-content-wrap #article-content"
ARCHIVE_SELECTOR = "#index-wrap h4:nth-of-type(14) + ul a[href]"

sys.path.insert(0, str(REPO_ROOT))
from src.extraction.base import ArticleExtractor  # noqa: E402
from src.sentence.segmenter import SentenceSegmenter  # noqa: E402
from src.sources.registry import ExtractionConfig, SourceConfig  # noqa: E402
from src.extraction.date_filter import DateFilter  # noqa: E402


ARCHIVE_FILES = {
    "2026-08-17": "archive_2026-08-17.html",
    "2026-08-18": "archive_2026-08-18.html",
    "2026-08-19": "archive_2026-08-19.html",
}
ARTICLE_FILES = [
    "article_453976_dogshow_divas.html",
    "article_453955_sen_bam.html",
    "article_453856_nanay_ni_sachzna.html",
]
HEADER_FILES = sorted(RAW_DIR.glob("*.headers.txt"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def read_bytes(path: Path) -> bytes:
    if not path.is_file():
        raise FileNotFoundError(path)
    return path.read_bytes()


def parse_headers(path: Path) -> dict[str, object]:
    raw = read_bytes(path)
    text = raw.decode("iso-8859-1")
    lines = text.splitlines()
    status_line = next((line.strip() for line in lines if line.upper().startswith("HTTP/")), "")
    status_match = re.match(r"HTTP/[^ ]+\s+(\d{3})\b", status_line)
    content_type = ""
    for line in lines:
        if line.lower().startswith("content-type:"):
            content_type = line.split(":", 1)[1].strip()
            break
    return {
        "header_file": path.name,
        "artifact_file": path.name.removesuffix(".headers.txt"),
        "sha256": sha256_bytes(raw),
        "status_line": status_line,
        "status_code": int(status_match.group(1)) if status_match else None,
        "content_type": content_type,
        "status_valid": status_match is not None,
        "content_type_valid": bool(content_type),
    }


def host_counts(links: list[str], base_url: str) -> dict[str, int]:
    hosts: Counter[str] = Counter()
    for href in links:
        resolved = urljoin(base_url, href.strip())
        hostname = (urlparse(resolved).hostname or "").lower()
        if hostname:
            hosts[hostname] += 1
    return dict(sorted(hosts.items()))


def archive_observation(date_key: str, filename: str) -> dict[str, object]:
    raw = read_bytes(RAW_DIR / filename)
    page_url = f"https://www.inquirer.net/article-index/?d={date_key}"
    soup = BeautifulSoup(raw, "html.parser")
    root = soup.select_one("#index-wrap")
    bandera_sections = [
        heading
        for heading in soup.select("#index-wrap h4")
        if heading.get_text(" ", strip=True).upper() == "BANDERA"
    ]
    section_links: list[str] = []
    for heading in bandera_sections:
        sibling = heading.find_next_sibling("ul")
        if sibling is not None:
            section_links.extend(
                anchor.get("href", "")
                for anchor in sibling.select("a[href]")
            )
    root_links = [anchor.get("href", "") for anchor in (root.select("a[href]") if root else [])]
    selector_links = [anchor.get("href", "") for anchor in soup.select(ARCHIVE_SELECTOR)]
    selector_hosts = host_counts(selector_links, page_url)
    return {
        "date": date_key,
        "artifact_file": filename,
        "body_sha256": sha256_bytes(raw),
        "body_size_bytes": len(raw),
        "bandera_section_count": len(bandera_sections),
        "bandera_section_link_count": len(section_links),
        "bandera_section_hosts": host_counts(section_links, page_url),
        "root_candidate_link_count": len(root_links),
        "root_candidate_hosts": host_counts(root_links, page_url),
        "configured_selector": ARCHIVE_SELECTOR,
        "configured_selector_link_count": len(selector_links),
        "configured_selector_hosts": selector_hosts,
        "configured_selector_only_bandera": (
            set(selector_hosts) == {"bandera.inquirer.net"}
        ),
        "pagination_mode_observation": "none; date-navigation claims are not followed",
    }


def article_observation(filename: str) -> dict[str, object]:
    raw = read_bytes(RAW_DIR / filename)
    html = raw.decode("utf-8", errors="replace")
    soup = BeautifulSoup(raw, "lxml")
    matches = soup.select(SCOPED_SELECTOR)
    source_bad = SourceConfig(
        id="bandera",
        name="Bandera",
        domain="bandera.inquirer.net",
        extraction=ExtractionConfig(
            type="wordpress", content_selector=".entry-content, #article_content"
        ),
    )
    source_scoped = SourceConfig(
        id="bandera",
        name="Bandera",
        domain="bandera.inquirer.net",
        extraction=ExtractionConfig(type="wordpress", content_selector=SCOPED_SELECTOR),
    )
    bad = ArticleExtractor.extract(html, source_bad)
    scoped = ArticleExtractor.extract(html, source_scoped)
    parsed_date = DateFilter(datetime(1970, 1, 1)).parse_date(
        scoped["publication_date_raw"]
    )
    return {
        "artifact_file": filename,
        "body_sha256": sha256_bytes(raw),
        "body_size_bytes": len(raw),
        "canonical_url": scoped["canonical_url"],
        "publication_date_raw": scoped["publication_date_raw"],
        "publication_date_source": scoped["publication_date_source"],
        "publication_date_parsed": parsed_date.isoformat() if parsed_date else None,
        "scoped_selector": SCOPED_SELECTOR,
        "scoped_selector_match": scoped["selector_match"],
        "scoped_extraction_method": scoped["extraction_method"],
        "scoped_body_chars": scoped["body_chars"],
        "scoped_paragraph_count": scoped["paragraph_count"],
        "scoped_sentence_count": len(
            SentenceSegmenter.split_sentences(scoped["article_text"])
        ),
        "scoped_body_valid": scoped["body_valid"],
        "current_selector": ".entry-content, #article_content",
        "current_selector_match": bad["selector_match"],
        "current_extraction_method": bad["extraction_method"],
        "current_body_chars": bad["body_chars"],
        "current_sentence_count": len(
            SentenceSegmenter.split_sentences(bad["article_text"])
        ),
        "article_content_scoped_match_count": len(matches),
        "article_content_duplicate_extra_matches": max(0, len(matches) - 1),
        "article_content_match_text_lengths": [
            len(node.get_text(" ", strip=True)) for node in matches
        ],
        "secondary_content_is_not_in_scoped_body": all(
            node.get_text(" ", strip=True)[:80] not in scoped["article_text"]
            for node in matches[1:]
            if node.get_text(" ", strip=True)[:80]
        ),
    }


def robots_observation() -> dict[str, object]:
    raw = read_bytes(RAW_DIR / "inquirer_robots.txt")
    lines = raw.decode("utf-8", errors="replace").splitlines()
    current_agents: list[str] = []
    wildcard_disallows: list[str] = []
    groups: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, _, value = stripped.partition(":")
        key = key.lower()
        value = value.strip()
        if key == "user-agent":
            if current is not None:
                groups.append(current)
            current_agents = [value]
            current = {"user_agents": current_agents, "disallow": []}
        elif key == "disallow" and current is not None:
            disallows = current["disallow"]
            assert isinstance(disallows, list)
            disallows.append(value)
            if "*" in current_agents:
                wildcard_disallows.append(value)
    if current is not None:
        groups.append(current)
    return {
        "artifact_file": "inquirer_robots.txt",
        "body_sha256": sha256_bytes(raw),
        "wildcard_groups": [group for group in groups if "*" in group["user_agents"]],
        "wildcard_disallow_rules": wildcard_disallows,
        "article_index_disallowed_for_wildcard": any(
            "/article-index" in rule for rule in wildcard_disallows
        ),
        "article_index_allowed_for_wildcard": not any(
            "/article-index" in rule for rule in wildcard_disallows
        ),
    }


def rss_observation(article_rows: list[dict[str, object]]) -> dict[str, object]:
    filename = "bandera_feed.xml"
    raw = read_bytes(RAW_DIR / filename)
    root = ElementTree.fromstring(raw)
    items = root.findall(".//item")
    encoded_tag = "{http://purl.org/rss/1.0/modules/content/}encoded"
    links = [
        (item.findtext("link") or "").strip().rstrip("/")
        for item in items
        if (item.findtext("link") or "").strip()
    ]
    canonical_urls = {
        str(row["canonical_url"]).rstrip("/") for row in article_rows if row["canonical_url"]
    }
    overlap = sorted(canonical_urls.intersection(links))
    return {
        "artifact_file": filename,
        "body_sha256": sha256_bytes(raw),
        "body_size_bytes": len(raw),
        "item_count": len(items),
        "content_encoded_item_count": sum(
            1 for item in items if item.find(encoded_tag) is not None
        ),
        "all_items_have_content_encoded": all(
            item.find(encoded_tag) is not None for item in items
        ),
        "item_links": links,
        "overlap_with_article_capture_canonicals": overlap,
        "overlap_count": len(overlap),
    }


def main() -> None:
    if not RAW_DIR.is_dir():
        raise FileNotFoundError(RAW_DIR)
    raw_files = []
    for path in sorted(item for item in RAW_DIR.rglob("*") if item.is_file()):
        value = read_bytes(path)
        raw_files.append(
            {
                "file": path.relative_to(RAW_DIR).as_posix(),
                "size_bytes": len(value),
                "sha256": sha256_bytes(value),
            }
        )
    hash_lines = [f"{row['sha256']}  {row['file']}" for row in raw_files]
    (RUN_DIR / "raw-evidence-files.sha256").write_text(
        "\n".join(hash_lines) + "\n", encoding="utf-8"
    )

    headers = [parse_headers(path) for path in HEADER_FILES]
    expected_body_files = {
        row["file"]
        for row in raw_files
        if not str(row["file"]).endswith(".headers.txt")
        and row["file"] != "http_responses.json"
    }
    header_artifacts = {str(row["artifact_file"]) for row in headers}
    article_rows = [article_observation(filename) for filename in ARTICLE_FILES]
    archive_rows = [
        archive_observation(date_key, filename)
        for date_key, filename in ARCHIVE_FILES.items()
    ]
    robots = robots_observation()
    rss = rss_observation(article_rows)

    report = {
        "run_id": RUN_ID,
        "validated_at_utc": datetime.now(timezone.utc).isoformat(),
        "raw_evidence_dir": str(RAW_DIR),
        "manifest_path": str(MANIFEST),
        "manifest_present": MANIFEST.is_file(),
        "raw_file_count": len(raw_files),
        "raw_files": raw_files,
        "headers": {
            "header_file_count": len(headers),
            "all_header_statuses_valid": all(row["status_valid"] for row in headers),
            "all_header_content_types_valid": all(
                row["content_type_valid"] for row in headers
            ),
            "all_body_artifacts_have_headers": expected_body_files.issubset(
                header_artifacts
            ),
            "responses": headers,
        },
        "robots": robots,
        "archives": archive_rows,
        "articles": article_rows,
        "rss": rss,
        "report_only_claims": {
            "page2_identical": "report_only_unverified",
            "page2_empty_date_behavior": "report_only_unverified",
        },
        "validation": {
            "manifest_present": MANIFEST.is_file(),
            "headers_complete": all(row["status_valid"] and row["content_type_valid"] for row in headers),
            "robots_article_index_allowed_for_wildcard": robots["article_index_allowed_for_wildcard"],
            "all_archives_have_one_bandera_section": all(
                row["bandera_section_count"] == 1 for row in archive_rows
            ),
            "all_configured_archive_candidates_are_bandera": all(
                row["configured_selector_only_bandera"] for row in archive_rows
            ),
            "articles_have_canonical_and_parsed_date": all(
                row["canonical_url"] and row["publication_date_parsed"]
                for row in article_rows
            ),
            "scoped_selector_is_used": all(
                row["scoped_selector_match"] == SCOPED_SELECTOR
                for row in article_rows
            ),
            "current_selector_mismatch_observed": all(
                row["current_selector_match"] == "article|body"
                for row in article_rows
            ),
            "duplicate_match_counts_recorded": all(
                row["article_content_scoped_match_count"] == 3
                for row in article_rows
            ),
            "no_secondary_body_or_sentence_inflation": all(
                row["secondary_content_is_not_in_scoped_body"]
                for row in article_rows
            ),
            "rss_content_type_checked": any(
                row["artifact_file"] == "bandera_feed.xml"
                and str(row["content_type"]).lower().startswith("application/rss+xml")
                for row in headers
            ),
            "rss_items_have_encoded_content": rss["all_items_have_content_encoded"],
        },
    }
    validation_values = report["validation"]
    report["overall_status"] = (
        "validated" if all(bool(value) for value in validation_values.values()) else "attention_required"
    )
    (RUN_DIR / "raw-evidence-validation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    log_lines = [
        f"run_id={RUN_ID}",
        f"raw_evidence_dir={RAW_DIR}",
        f"manifest_present={MANIFEST.is_file()}",
        f"raw_file_count={len(raw_files)}",
        "header_validation=" + json.dumps(report["headers"], ensure_ascii=False),
        "robots=" + json.dumps(robots, ensure_ascii=False),
    ]
    for row in archive_rows:
        log_lines.append(
            "archive=" + json.dumps(
                {
                    key: row[key]
                    for key in (
                        "date",
                        "bandera_section_count",
                        "bandera_section_link_count",
                        "bandera_section_hosts",
                        "root_candidate_link_count",
                        "root_candidate_hosts",
                        "configured_selector_link_count",
                        "configured_selector_hosts",
                    )
                },
                ensure_ascii=False,
            )
        )
    for row in article_rows:
        log_lines.append(
            "article=" + json.dumps(
                {
                    key: row[key]
                    for key in (
                        "artifact_file",
                        "canonical_url",
                        "publication_date_raw",
                        "publication_date_parsed",
                        "scoped_selector_match",
                        "scoped_body_chars",
                        "scoped_paragraph_count",
                        "scoped_sentence_count",
                        "current_selector_match",
                        "current_body_chars",
                        "current_sentence_count",
                        "article_content_scoped_match_count",
                        "article_content_duplicate_extra_matches",
                    )
                },
                ensure_ascii=False,
            )
        )
    log_lines.extend(
        [
            "rss=" + json.dumps(rss, ensure_ascii=False),
            "report_only_claim_page2_identical=report_only_unverified",
            "report_only_claim_page2_empty_date_behavior=report_only_unverified",
            "validation=" + json.dumps(report["validation"], ensure_ascii=False),
            f"overall_status={report['overall_status']}",
        ]
    )
    (RUN_DIR / "raw-evidence-validation.log").write_text(
        "\n".join(log_lines) + "\n", encoding="utf-8"
    )
    print(json.dumps({"run_id": RUN_ID, "overall_status": report["overall_status"], "validation": report["validation"]}, indent=2))


if __name__ == "__main__":
    main()
