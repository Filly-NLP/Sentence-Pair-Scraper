from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


RUN_ID = "20260902T112430893Z"
EXPECTED_BACKUP_SHA256 = "92c347d62ed8d9850ef94c37d8aeb18151c36aa2a83589cf2056247cabad6923"
QUARANTINE_RETRY_AT = "9999-12-31 23:59:59"
SOURCE_DOMAIN = "bandera.inquirer.net"

run_dir = Path(__file__).resolve().parent
repo_root = run_dir.parents[2]
trial_path = (run_dir / "trial.db").resolve()
backup_path = (repo_root / "data" / "rollout" / "20260902T104619598Z" / "corpus.db.backup.sqlite").resolve()
prep_path = run_dir / "queue-normalization-manifest.json"
seed_path = run_dir / "trial-seed-manifest.json"
report_path = run_dir / "verify-trial.json"
log_path = run_dir / "verify-trial.log"


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    return value if isinstance(value, dict) else {}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_info(path: Path) -> dict:
    stat = path.stat()
    return {
        "path": str(path),
        "sha256": sha256(path),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "mtime_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    }


def open_ro(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def columns(connection: sqlite3.Connection, table: str) -> set[str]:
    if table not in tables(connection):
        return set()
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def as_dict(row: sqlite3.Row | None) -> dict:
    return {key: row[key] for key in row.keys()} if row is not None else {}


def integrity(connection: sqlite3.Connection) -> list[str]:
    return [row[0] for row in connection.execute("PRAGMA integrity_check").fetchall()]


def count_table(connection: sqlite3.Connection, table: str) -> int | None:
    if table not in tables(connection):
        return None
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def queue_summary(connection: sqlite3.Connection) -> dict:
    cols = columns(connection, "urls")
    if not {"source_id", "discovery_method", "status"}.issubset(cols):
        return {"schema_available": False}
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    due = "status = 'DISCOVERED'"
    if "next_retry_at" in cols:
        due += " OR (status = 'RETRY_WAIT' AND (next_retry_at IS NULL OR next_retry_at <= ?))"
    params = (now,) if "next_retry_at" in cols else ()
    def eligible(where: str, extra: tuple = ()) -> int:
        return int(connection.execute(
            f"SELECT COUNT(*) FROM urls WHERE ({where}) AND ({due})", (*extra, *params)
        ).fetchone()[0])
    result = {
        "schema_available": True,
        "eligible_bandera_rss": eligible("source_id = ? AND discovery_method = ?", ("bandera", "RSS")),
        "eligible_bandera_non_rss": eligible("source_id = ? AND discovery_method <> ?", ("bandera", "RSS")),
        "eligible_other_sources": eligible("source_id <> ?", ("bandera",)),
    }
    result["processing_count"] = int(connection.execute(
        "SELECT COUNT(*) FROM urls WHERE status = 'PROCESSING'"
    ).fetchone()[0])
    result["status_counts"] = {
        str(status): int(number)
        for status, number in connection.execute(
            "SELECT status, COUNT(*) FROM urls GROUP BY status ORDER BY status"
        )
    }
    return result


def url_rows(connection: sqlite3.Connection) -> dict[int, dict]:
    if "urls" not in tables(connection) or "url_id" not in columns(connection, "urls"):
        return {}
    return {
        int(row["url_id"]): as_dict(row)
        for row in connection.execute("SELECT * FROM urls ORDER BY url_id").fetchall()
    }


def row_diff(expected: dict, actual: dict) -> dict:
    return {
        key: {"expected": expected.get(key), "actual": actual.get(key)}
        for key in sorted(set(expected) | set(actual))
        if expected.get(key) != actual.get(key)
    }


def host(value: object) -> str | None:
    if not value:
        return None
    return (urlparse(str(value)).hostname or "").lower() or None


def parse_json(value: object) -> object:
    if not value:
        return None
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"_raw": value, "_parse_error": True}


if not trial_path.is_file():
    raise FileNotFoundError(f"Missing trial database: {trial_path}")
if not trial_path.is_relative_to(run_dir):
    raise AssertionError(f"Trial database is outside run directory: {trial_path}")
if not backup_path.is_file():
    raise FileNotFoundError(f"Missing verified backup: {backup_path}")

prep = load_json(prep_path)
seed = load_json(seed_path)
selected_ids = [2, 3, 4, 5, 6]
quarantined_ids = {int(value) for value in prep.get("quarantined_ids", [])}
canonical_expected = prep.get("canonical_database_before")

trial = open_ro(trial_path)
backup = open_ro(backup_path)
try:
    trial_tables = sorted(tables(trial))
    backup_tables = sorted(tables(backup))
    trial_schema = {table: sorted(columns(trial, table)) for table in trial_tables}
    backup_schema = {table: sorted(columns(backup, table)) for table in backup_tables}
    trial_integrity = integrity(trial)
    backup_integrity = integrity(backup)
    trial_queue = queue_summary(trial)
    trial_counts = {table: count_table(trial, table) for table in ("articles", "sentences", "urls", "crawl_runs", "crawl_events")}
    backup_counts = {table: count_table(backup, table) for table in ("articles", "sentences", "urls", "crawl_runs", "crawl_events")}

    url_columns = columns(trial, "urls")
    selected_rows = []
    if "urls" in trial_tables and "url_id" in url_columns:
        placeholders = ",".join("?" for _ in selected_ids)
        selected_rows = [
            as_dict(row)
            for row in trial.execute(
                f"SELECT * FROM urls WHERE url_id IN ({placeholders}) ORDER BY url_id",
                selected_ids,
            ).fetchall()
        ]

    run_columns = columns(trial, "crawl_runs")
    latest_run = {}
    if {"crawl_id", "source_id", "mode"}.issubset(run_columns):
        order = "start_time DESC, crawl_id DESC" if "start_time" in run_columns else "crawl_id DESC"
        latest_run = as_dict(trial.execute(
            f"SELECT * FROM crawl_runs WHERE source_id = 'bandera' AND mode = 'crawl' ORDER BY {order} LIMIT 1"
        ).fetchone())
    crawl_id = latest_run.get("crawl_id")

    event_rows = []
    event_columns = columns(trial, "crawl_events")
    if crawl_id and "crawl_id" in event_columns:
        event_rows = [
            as_dict(row)
            for row in trial.execute(
                "SELECT * FROM crawl_events WHERE crawl_id = ? ORDER BY event_id",
                (crawl_id,),
            ).fetchall()
        ]
    event_types = Counter(str(row.get("event_type")) for row in event_rows)
    event_statuses = Counter(
        str(row.get("http_status")) if row.get("http_status") is not None else "null"
        for row in event_rows
    )
    event_type_statuses = Counter(
        (
            str(row.get("event_type")),
            str(row.get("http_status")) if row.get("http_status") is not None else "null",
        )
        for row in event_rows
    )
    event_urls = sorted({str(row["url"]) for row in event_rows if row.get("url")})

    backup_url_rows = url_rows(backup)
    trial_url_rows = url_rows(trial)
    changed_nonselected = []
    for url_id, expected in backup_url_rows.items():
        if url_id in selected_ids:
            continue
        normalized = dict(expected)
        if url_id in quarantined_ids:
            normalized.update({
                "status": "RETRY_WAIT",
                "processing_started_at": None,
                "next_retry_at": QUARANTINE_RETRY_AT,
            })
        actual = trial_url_rows.get(url_id)
        differences = row_diff(normalized, actual or {})
        if actual is None or differences:
            changed_nonselected.append({
                "url_id": url_id,
                "differences": differences or {"row": {"expected": normalized, "actual": None}},
            })
    for url_id, actual in trial_url_rows.items():
        if url_id not in backup_url_rows and url_id not in selected_ids:
            changed_nonselected.append({"url_id": url_id, "differences": {"unexpected_new_row": actual}})

    source_changes = []
    if "source_id" in columns(trial, "sources") and "source_id" in columns(backup, "sources"):
        baseline_sources = {
            str(row["source_id"]): as_dict(row)
            for row in backup.execute("SELECT * FROM sources ORDER BY source_id").fetchall()
        }
        current_sources = {
            str(row["source_id"]): as_dict(row)
            for row in trial.execute("SELECT * FROM sources ORDER BY source_id").fetchall()
        }
        for source_id in sorted(set(baseline_sources) | set(current_sources)):
            differences = row_diff(baseline_sources.get(source_id, {}), current_sources.get(source_id, {}))
            if differences:
                source_changes.append({"source_id": source_id, "differences": differences})

    remaining = []
    if {"source_id", "discovery_method", "status"}.issubset(url_columns):
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        due = "status = 'DISCOVERED'"
        params = ["bandera", "RSS"]
        if "next_retry_at" in url_columns:
            due += " OR (status = 'RETRY_WAIT' AND (next_retry_at IS NULL OR next_retry_at <= ?))"
            params.append(now)
        remaining = [
            as_dict(row)
            for row in trial.execute(
                f"SELECT * FROM urls WHERE source_id = ? AND discovery_method = ? AND ({due}) ORDER BY url_id",
                params,
            ).fetchall()
        ]

    selected_final = []
    domain_violations = []
    for row in selected_rows:
        diagnostic = parse_json(row.get("extraction_diagnostics"))
        selected_final.append({
            "url_id": row.get("url_id"),
            "source_id": row.get("source_id"),
            "discovery_method": row.get("discovery_method"),
            "url": row.get("url"),
            "status": row.get("status"),
            "http_status": row.get("last_http_status", row.get("http_status")),
            "retry_count": row.get("retry_count"),
            "extraction_diagnostics": diagnostic,
        })
        for value in (row.get("url"), row.get("canonical_url")):
            value_host = host(value)
            if value_host and value_host != SOURCE_DOMAIN:
                domain_violations.append({"url_id": row.get("url_id"), "value": value, "host": value_host})

    seed_counts = seed.get("source_table_counts", {})
    before_articles = int(seed_counts.get("articles", 3))
    before_sentences = int(seed_counts.get("sentences", 21))
    after_articles = int(trial_counts.get("articles") or 0)
    after_sentences = int(trial_counts.get("sentences") or 0)
    new_articles = []
    new_sentences = []
    for table, id_column, output in (("articles", "article_id", new_articles), ("sentences", "sentence_id", new_sentences)):
        if id_column not in columns(backup, table) or id_column not in columns(trial, table):
            continue
        baseline_ids = {str(row[0]) for row in backup.execute(f"SELECT {id_column} FROM {table}").fetchall()}
        if baseline_ids:
            placeholders = ",".join("?" for _ in baseline_ids)
            output.extend(as_dict(row) for row in trial.execute(
                f"SELECT * FROM {table} WHERE {id_column} NOT IN ({placeholders}) ORDER BY {id_column}",
                tuple(sorted(baseline_ids)),
            ).fetchall())

    rejection_reasons = Counter(
        event_type for event_type in event_types
        if event_type.startswith("rejected:") or event_type.startswith("duplicate:")
    )
    diagnostic_breakdowns = []
    for row in selected_rows:
        diagnostic = parse_json(row.get("extraction_diagnostics"))
        if isinstance(diagnostic, dict) and "breakdown" in diagnostic:
            diagnostic_breakdowns.append({"url_id": row.get("url_id"), "breakdown": diagnostic["breakdown"]})

    http_403_429 = {
        status: count for status, count in event_statuses.items() if status in {"403", "429"}
    }
    robots_denials = sum(count for event_type, count in event_types.items() if event_type.startswith("blocked:robots"))
    processing_count = int(trial.execute(
        "SELECT COUNT(*) FROM urls WHERE status = 'PROCESSING'"
    ).fetchone()[0]) if "status" in url_columns else None
finally:
    trial.close()
    backup.close()

canonical_current = file_info((repo_root / "data" / "corpus.db").resolve())
canonical_unchanged = canonical_current == canonical_expected
backup_hash = sha256(backup_path)
selected_present = {int(row["url_id"]) for row in selected_rows} == set(selected_ids)
run_claimed = int(latest_run["urls_discovered"]) if latest_run.get("urls_discovered") is not None else None
event_urls_within_selected = set(event_urls).issubset({str(row.get("url")) for row in selected_final})
selected_not_processing = all(row.get("status") != "PROCESSING" for row in selected_final) and selected_present
criteria = {
    "selected_ids_2_3_4_5_6_present": selected_present,
    "latest_bandera_crawl_run_present": bool(latest_run),
    "claimed_urls_at_most_5": run_claimed is not None and run_claimed <= 5,
    "crawl_events_only_selected_urls": event_urls_within_selected,
    "selected_rows_not_processing": selected_not_processing,
    "no_robots_denials": robots_denials == 0,
    "no_repeated_403_or_429": all(count < 2 for count in http_403_429.values()),
    "no_off_domain_or_cross_brand_content_observed": not domain_violations,
    "no_processing_rows": processing_count == 0,
    "no_unexpected_nonselected_changes": not changed_nonselected and not source_changes,
    "trial_integrity_ok": trial_integrity == ["ok"],
    "verified_backup_hash_and_integrity_ok": backup_hash == EXPECTED_BACKUP_SHA256 and backup_integrity == ["ok"],
    "canonical_hash_size_mtime_unchanged": canonical_unchanged,
    "remaining_eligible_bandera_rss_zero": len(remaining) == 0,
    "new_sentence_yield_positive": after_sentences > before_sentences,
}

report = {
    "run_id": RUN_ID,
    "read_only": True,
    "verified_at_utc": datetime.now(timezone.utc).isoformat(),
    "schema": {"trial_tables": trial_tables, "backup_tables": backup_tables, "trial_columns": trial_schema, "backup_columns": backup_schema},
    "latest_bandera_crawl_run": latest_run,
    "selected_final_rows": selected_final,
    "crawl_events": {
        "count": len(event_rows),
        "by_event_type": dict(sorted(event_types.items())),
        "by_http_status": dict(sorted(event_statuses.items())),
        "by_event_type_and_http_status": [
            {"event_type": event_type, "http_status": status, "count": count}
            for (event_type, status), count in sorted(event_type_statuses.items())
        ],
        "rows": event_rows,
    },
    "corpus_counts": {
        "articles_before_seed": before_articles,
        "articles_after": after_articles,
        "articles_added": after_articles - before_articles,
        "sentences_before_seed": before_sentences,
        "sentences_after": after_sentences,
        "sentences_added": after_sentences - before_sentences,
        "new_articles": new_articles,
        "new_sentences": new_sentences,
        "accepted_sentences": latest_run.get("sentences_accepted"),
        "rejected_sentences": latest_run.get("sentences_rejected"),
        "rejection_reasons": dict(sorted(rejection_reasons.items())),
        "selected_extraction_breakdowns": diagnostic_breakdowns,
    },
    "remaining_eligible_bandera_rss": remaining,
    "changed_nonselected_rows": {"url_rows": changed_nonselected, "source_rows": source_changes},
    "processing_count": processing_count,
    "integrity": {"trial": trial_integrity, "backup": backup_integrity},
    "backup": {"path": str(backup_path), "sha256": backup_hash, "expected_sha256": EXPECTED_BACKUP_SHA256},
    "canonical_database": {"pre_crawl": canonical_expected, "current": canonical_current, "unchanged": canonical_unchanged},
    "safety_observations": {
        "robots_denials": robots_denials,
        "http_403_429": http_403_429,
        "domain_violations": domain_violations,
        "claimed_urls": run_claimed,
        "event_urls_within_selected": event_urls_within_selected,
        "redirect_check_note": "Final response URL is not persisted; original and canonical URL hosts were checked.",
    },
    "acceptance_criteria": criteria,
    "all_acceptance_criteria_pass": all(criteria.values()),
    "criteria_not_met": [name for name, passed in criteria.items() if not passed],
}
report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")

log_lines = [
    f"RUN_ID: {RUN_ID}",
    "READ_ONLY: true",
    f"LATEST_CRAWL_RUN: {json.dumps(latest_run, default=str)}",
    f"SELECTED_FINAL_ROWS: {json.dumps(selected_final, ensure_ascii=False, default=str)}",
    f"EVENTS_BY_TYPE: {json.dumps(dict(sorted(event_types.items())), sort_keys=True)}",
    f"EVENTS_BY_HTTP_STATUS: {json.dumps(dict(sorted(event_statuses.items())), sort_keys=True)}",
    f"ARTICLES_BEFORE_AFTER_ADDED: {before_articles}/{after_articles}/{after_articles - before_articles}",
    f"SENTENCES_BEFORE_AFTER_ADDED: {before_sentences}/{after_sentences}/{after_sentences - before_sentences}",
    f"ACCEPTED_SENTENCES: {latest_run.get('sentences_accepted')}",
    f"REJECTED_SENTENCES: {latest_run.get('sentences_rejected')}",
    f"REJECTION_REASONS: {json.dumps(dict(sorted(rejection_reasons.items())), sort_keys=True)}",
    f"REMAINING_ELIGIBLE_BANDERA_RSS: {len(remaining)}",
    f"CHANGED_NONSELected_ROWS: {len(changed_nonselected) + len(source_changes)}",
    f"PROCESSING_COUNT: {processing_count}",
    f"TRIAL_INTEGRITY: {trial_integrity}",
    f"CANONICAL_UNCHANGED: {canonical_unchanged}",
    "ACCEPTANCE_CRITERIA:",
]
log_lines.extend(f"  {name}: {'PASS' if passed else 'FAIL'}" for name, passed in criteria.items())
log_lines.append(f"ALL_ACCEPTANCE_CRITERIA_PASS: {all(criteria.values())}")
log_lines.append(f"CRITERIA_NOT_MET: {json.dumps([name for name, passed in criteria.items() if not passed])}")
log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
