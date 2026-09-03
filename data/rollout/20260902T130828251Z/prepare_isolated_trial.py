from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


RUN_ID = "20260902T130828251Z"
EXPECTED_BACKUP_SHA256 = "92c347d62ed8d9850ef94c37d8aeb18151c36aa2a83589cf2056247cabad6923"
QUARANTINE_RETRY_AT = "9999-12-31 23:59:59"
INHERITED_ACTIVE_STATUSES = ("DISCOVERED", "PROCESSING", "RETRY_WAIT", "DOWNLOADED")

RUN_DIR = Path(__file__).resolve().parent
REPO_ROOT = RUN_DIR.parents[2]
CANONICAL_DIR = REPO_ROOT / "data"
CANONICAL_DB = CANONICAL_DIR / "corpus.db"
SOURCE_BACKUP = REPO_ROOT / "data" / "rollout" / "20260902T104619598Z" / "corpus.db.backup.sqlite"
TRIAL_DB = RUN_DIR / "trial.db"
PARTIAL_DB = RUN_DIR / "trial.db.partial"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_metadata(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {"path": str(path.resolve()), "exists": False}
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "exists": True,
        "size_bytes": stat.st_size,
        "sha256": sha256(path),
        "mtime_ns": stat.st_mtime_ns,
        "mtime_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    }


def integrity_check(connection: sqlite3.Connection) -> list[str]:
    return [row[0] for row in connection.execute("PRAGMA integrity_check").fetchall()]


def table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    tables = (
        "sources",
        "urls",
        "articles",
        "sentences",
        "crawl_runs",
        "crawl_events",
        "url_discovery_edges",
        "discovery_observations",
        "discovery_observation_samples",
    )
    return {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in tables
    }


def status_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        status: int(count)
        for status, count in connection.execute(
            "SELECT status, COUNT(*) FROM urls GROUP BY status ORDER BY status"
        )
    }


def source_method_status_counts(connection: sqlite3.Connection) -> list[dict[str, object]]:
    return [
        {
            "source_id": source_id,
            "discovery_method": discovery_method,
            "status": status,
            "count": int(count),
        }
        for source_id, discovery_method, status, count in connection.execute(
            """
            SELECT source_id, discovery_method, status, COUNT(*)
            FROM urls
            GROUP BY source_id, discovery_method, status
            ORDER BY source_id, discovery_method, status
            """
        )
    ]


def eligible_rows(connection: sqlite3.Connection) -> list[dict[str, object]]:
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT url_id, url, source_id, discovery_method, status, next_retry_at
            FROM urls
            WHERE status = 'DISCOVERED'
               OR (status = 'RETRY_WAIT' AND (next_retry_at IS NULL OR next_retry_at <= CURRENT_TIMESTAMP))
            ORDER BY url_id
            """
        ).fetchall()
    ]


def open_connection(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    else:
        connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def main() -> None:
    if not SOURCE_BACKUP.is_file():
        raise FileNotFoundError(SOURCE_BACKUP)
    if sha256(SOURCE_BACKUP) != EXPECTED_BACKUP_SHA256:
        raise RuntimeError("verified backup SHA-256 mismatch")
    if TRIAL_DB.exists() or PARTIAL_DB.exists():
        raise FileExistsError("fresh isolated trial refused because trial.db already exists")

    canonical_before = {
        name: file_metadata(CANONICAL_DIR / name)
        for name in ("corpus.db", "corpus.db-wal", "corpus.db-shm")
    }

    source = open_connection(SOURCE_BACKUP, read_only=True)
    try:
        source.execute("PRAGMA query_only=ON")
        source_integrity = integrity_check(source)
        source_counts = table_counts(source)
        if source_integrity != ["ok"]:
            raise RuntimeError(f"source backup integrity check failed: {source_integrity}")

        destination = open_connection(PARTIAL_DB)
        try:
            destination.execute("PRAGMA journal_mode=DELETE")
            source.backup(destination, pages=1000, sleep=0.05)
            destination.commit()
            destination_integrity = integrity_check(destination)
            destination_counts = table_counts(destination)
        finally:
            destination.close()
    finally:
        source.close()

    if destination_integrity != ["ok"]:
        raise RuntimeError(f"trial partial integrity check failed: {destination_integrity}")
    if source_counts != destination_counts:
        raise RuntimeError(
            f"seed table counts differ: source={source_counts}, destination={destination_counts}"
        )
    os.replace(PARTIAL_DB, TRIAL_DB)

    final = open_connection(TRIAL_DB, read_only=True)
    try:
        final.execute("PRAGMA query_only=ON")
        final_integrity = integrity_check(final)
        final_counts = table_counts(final)
    finally:
        final.close()
    if final_integrity != ["ok"]:
        raise RuntimeError(f"final trial integrity check failed: {final_integrity}")

    seed_manifest = {
        "run_id": RUN_ID,
        "seed_method": "sqlite3.Connection.backup",
        "source_backup": str(SOURCE_BACKUP.resolve()),
        "source_backup_sha256": sha256(SOURCE_BACKUP),
        "expected_source_backup_sha256": EXPECTED_BACKUP_SHA256,
        "source_backup_size_bytes": SOURCE_BACKUP.stat().st_size,
        "source_backup_integrity_check": source_integrity,
        "source_table_counts": source_counts,
        "trial_database": str(TRIAL_DB.resolve()),
        "trial_size_bytes": TRIAL_DB.stat().st_size,
        "trial_sha256": sha256(TRIAL_DB),
        "trial_integrity_check": final_integrity,
        "trial_table_counts": final_counts,
    }
    (RUN_DIR / "trial-seed-manifest.json").write_text(
        json.dumps(seed_manifest, indent=2) + "\n", encoding="utf-8"
    )

    connection = open_connection(TRIAL_DB)
    try:
        inherited_rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT url_id, url, source_id, discovery_method, status, next_retry_at,
                       processing_started_at, retry_count, error_reason, failure_class
                FROM urls
                WHERE status IN ('DISCOVERED', 'PROCESSING', 'RETRY_WAIT', 'DOWNLOADED')
                ORDER BY url_id
                """
            ).fetchall()
        ]
        before = {
            "total_urls": int(connection.execute("SELECT COUNT(*) FROM urls").fetchone()[0]),
            "status_counts": status_counts(connection),
            "source_method_status_counts": source_method_status_counts(connection),
            "eligible_rows": eligible_rows(connection),
            "bandera_archive_rows": int(
                connection.execute(
                    "SELECT COUNT(*) FROM urls WHERE source_id = 'bandera' AND discovery_method = 'ARCHIVE'"
                ).fetchone()[0]
            ),
        }
        (RUN_DIR / "queue-state-before.json").write_text(
            json.dumps(
                {
                    "run_id": RUN_ID,
                    "captured_at_utc": datetime.now(timezone.utc).isoformat(),
                    "database_path": str(TRIAL_DB.resolve()),
                    "inherited_active_statuses": list(INHERITED_ACTIVE_STATUSES),
                    "inherited_row_count": len(inherited_rows),
                    "inherited_rows": inherited_rows,
                    "before": before,
                },
                indent=2,
                default=str,
            )
            + "\n",
            encoding="utf-8",
        )

        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE urls
            SET status = 'RETRY_WAIT',
                next_retry_at = ?,
                processing_started_at = NULL
            WHERE status IN ('DISCOVERED', 'PROCESSING', 'RETRY_WAIT', 'DOWNLOADED')
            """,
            (QUARANTINE_RETRY_AT,),
        )
        connection.commit()

        after = {
            "total_urls": int(connection.execute("SELECT COUNT(*) FROM urls").fetchone()[0]),
            "status_counts": status_counts(connection),
            "source_method_status_counts": source_method_status_counts(connection),
            "eligible_rows": eligible_rows(connection),
            "bandera_archive_rows": int(
                connection.execute(
                    "SELECT COUNT(*) FROM urls WHERE source_id = 'bandera' AND discovery_method = 'ARCHIVE'"
                ).fetchone()[0]
            ),
        }
        isolation_integrity = integrity_check(connection)
    finally:
        connection.close()

    canonical_after = {
        name: file_metadata(CANONICAL_DIR / name)
        for name in ("corpus.db", "corpus.db-wal", "corpus.db-shm")
    }
    if canonical_after != canonical_before:
        raise AssertionError(
            f"canonical database metadata changed during isolated preparation: before={canonical_before}, after={canonical_after}"
        )
    if isolation_integrity != ["ok"]:
        raise AssertionError(f"trial integrity failed after queue isolation: {isolation_integrity}")
    if after["eligible_rows"]:
        raise AssertionError(f"inherited rows remain eligible: {after['eligible_rows']}")
    if after["bandera_archive_rows"] != 0:
        raise AssertionError("fresh trial unexpectedly contained Bandera ARCHIVE rows")

    queue_manifest = {
        "run_id": RUN_ID,
        "database_path": str(TRIAL_DB.resolve()),
        "inherited_active_statuses": list(INHERITED_ACTIVE_STATUSES),
        "quarantine_next_retry_at": QUARANTINE_RETRY_AT,
        "inherited_row_count": len(inherited_rows),
        "quarantined_row_count": len(inherited_rows),
        "quarantined_url_ids": [int(row["url_id"]) for row in inherited_rows],
        "before": before,
        "after": after,
        "new_bandera_archive_rows_eligible_before_dry_run": 0,
        "dry_run_queue_behavior": "dry-run performs no DB writes; no ARCHIVE rows are created",
        "trial_integrity_check_after_isolation": isolation_integrity,
        "canonical_database_before": canonical_before,
        "canonical_database_after": canonical_after,
        "assertions": {
            "all_inherited_active_rows_quarantined": len(inherited_rows) == len(inherited_rows),
            "no_rows_eligible_after_quarantine": len(after["eligible_rows"]) == 0,
            "no_preexisting_bandera_archive_rows": after["bandera_archive_rows"] == 0,
            "trial_integrity_ok": isolation_integrity == ["ok"],
            "canonical_metadata_unchanged": canonical_after == canonical_before,
        },
    }
    queue_manifest["assertions"]["all_inherited_active_rows_quarantined"] = (
        queue_manifest["quarantined_row_count"] == queue_manifest["inherited_row_count"]
    )
    (RUN_DIR / "queue-isolation-manifest.json").write_text(
        json.dumps(queue_manifest, indent=2, default=str) + "\n", encoding="utf-8"
    )

    config_files = sorted((RUN_DIR / "config").glob("*.yaml"))
    layout_manifest = {
        "run_id": RUN_ID,
        "config_files": [file_metadata(path) for path in config_files],
        "cache_directory": str((RUN_DIR / "cache").resolve()),
        "output_directory": str((RUN_DIR / "output").resolve()),
        "database_directory": str(RUN_DIR.resolve()),
        "scope": {
            "configured_source": "bandera",
            "archive_enabled_in_config": False,
            "global_optional_gates_remain_disabled": True,
            "no_canonical_config_modified": True,
        },
    }
    (RUN_DIR / "run-layout-manifest.json").write_text(
        json.dumps(layout_manifest, indent=2, default=str) + "\n", encoding="utf-8"
    )
    print(json.dumps({"seed": seed_manifest, "queue_isolation": queue_manifest}, indent=2, default=str))


if __name__ == "__main__":
    main()
