from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


RUN_ID = "20260902T112430893Z"
QUARANTINE_RETRY_AT = "9999-12-31 23:59:59"
ACTIVE_STATUSES = ("PROCESSING", "RETRY_WAIT")
RESET_FAILURE_FIELDS = (
    "error_reason",
    "last_http_status",
    "failure_class",
    "last_attempt_at",
    "extraction_diagnostics",
)

run_dir = Path(__file__).resolve().parent
repo_root = run_dir.parents[2]
trial_db = (run_dir / "trial.db").resolve()
canonical_db = (repo_root / "data" / "corpus.db").resolve()
queue_before_path = run_dir / "queue-state-before.json"
normalization_manifest_path = run_dir / "queue-normalization-manifest.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_metadata(path: Path) -> dict[str, object]:
    stat = path.stat()
    return {
        "path": str(path),
        "sha256": sha256(path),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "mtime_utc": datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc
        ).isoformat(),
    }


def row_dict(row: sqlite3.Row) -> dict[str, object]:
    return {key: row[key] for key in row.keys()}


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


def eligible_count(
    connection: sqlite3.Connection,
    where_sql: str,
    parameters: tuple[object, ...] = (),
) -> int:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    sql = f"""
        SELECT COUNT(*)
        FROM urls
        WHERE ({where_sql})
          AND (
              status = 'DISCOVERED'
              OR (
                  status = 'RETRY_WAIT'
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
              )
          )
    """
    return int(connection.execute(sql, (*parameters, now)).fetchone()[0])


def queue_counts(connection: sqlite3.Connection) -> dict[str, object]:
    return {
        "total_urls": int(connection.execute("SELECT COUNT(*) FROM urls").fetchone()[0]),
        "status_counts": status_counts(connection),
        "source_method_status_counts": source_method_status_counts(connection),
        "eligible_bandera_rss": eligible_count(
            connection, "source_id = ? AND discovery_method = ?", ("bandera", "RSS")
        ),
        "eligible_bandera_non_rss": eligible_count(
            connection, "source_id = ? AND discovery_method <> ?", ("bandera", "RSS")
        ),
        "eligible_other_sources": eligible_count(
            connection, "source_id <> ?", ("bandera",)
        ),
        "processing_count": int(
            connection.execute(
                "SELECT COUNT(*) FROM urls WHERE status = 'PROCESSING'"
            ).fetchone()[0]
        ),
    }


def integrity_check(connection: sqlite3.Connection) -> list[str]:
    return [row[0] for row in connection.execute("PRAGMA integrity_check").fetchall()]


if not trial_db.is_file():
    raise FileNotFoundError(f"Trial database not found: {trial_db}")
if not trial_db.is_relative_to(run_dir):
    raise AssertionError(f"Trial database is outside the run directory: {trial_db}")
if queue_before_path.exists() or normalization_manifest_path.exists():
    raise FileExistsError("Fresh preparation refused because a normalization artifact exists")
if not canonical_db.is_file():
    raise FileNotFoundError(f"Canonical database not found: {canonical_db}")

canonical_before = file_metadata(canonical_db)
connection = sqlite3.connect(str(trial_db))
connection.row_factory = sqlite3.Row
connection.execute("PRAGMA foreign_keys=ON")
try:
    inherited_rows = [
        row_dict(row)
        for row in connection.execute(
            """
            SELECT *
            FROM urls
            WHERE status IN ('PROCESSING', 'RETRY_WAIT')
            ORDER BY url_id
            """
        ).fetchall()
    ]
    before_counts = queue_counts(connection)
    bandera_retry_candidates = [
        row
        for row in inherited_rows
        if row["source_id"] == "bandera"
        and row["discovery_method"] == "RSS"
        and row["status"] == "RETRY_WAIT"
    ]
    if len(bandera_retry_candidates) < 5:
        raise AssertionError(
            f"Expected at least five original Bandera RSS retry rows, found {len(bandera_retry_candidates)}"
        )
    selected_rows = bandera_retry_candidates[:5]
    selected_ids = [int(row["url_id"]) for row in selected_rows]
    inherited_ids = [int(row["url_id"]) for row in inherited_rows]

    queue_before = {
        "run_id": RUN_ID,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "database_path": str(trial_db),
        "inherited_statuses": list(ACTIVE_STATUSES),
        "inherited_row_count": len(inherited_rows),
        "inherited_rows": inherited_rows,
        "before_counts": before_counts,
        "original_bandera_rss_retry_candidates": [
            {"url_id": int(row["url_id"]), "url": row["url"]}
            for row in bandera_retry_candidates
        ],
        "selected_ids": selected_ids,
    }
    queue_before_path.write_text(
        json.dumps(queue_before, indent=2, default=str) + "\n",
        encoding="utf-8",
    )

    connection.execute("BEGIN IMMEDIATE")
    current_rows = [
        row_dict(row)
        for row in connection.execute(
            """
            SELECT *
            FROM urls
            WHERE status IN ('PROCESSING', 'RETRY_WAIT')
            ORDER BY url_id
            """
        ).fetchall()
    ]
    if current_rows != inherited_rows:
        raise AssertionError("Inherited queue changed between snapshot and transaction")

    connection.executemany(
        """
        UPDATE urls
        SET status = 'RETRY_WAIT',
            processing_started_at = NULL,
            next_retry_at = ?
        WHERE url_id = ?
        """,
        [(QUARANTINE_RETRY_AT, url_id) for url_id in inherited_ids],
    )
    connection.executemany(
        """
        UPDATE urls
        SET status = 'DISCOVERED',
            retry_count = 0,
            next_retry_at = NULL,
            processing_started_at = NULL,
            error_reason = NULL,
            last_http_status = NULL,
            failure_class = NULL,
            last_attempt_at = NULL,
            extraction_diagnostics = NULL
        WHERE url_id = ?
        """,
        [(url_id,) for url_id in selected_ids],
    )
    connection.commit()

    after_counts = queue_counts(connection)
    integrity = integrity_check(connection)
    selected_after = [
        row_dict(row)
        for row in connection.execute(
            "SELECT * FROM urls WHERE url_id IN ({}) ORDER BY url_id".format(
                ",".join("?" for _ in selected_ids)
            ),
            selected_ids,
        ).fetchall()
    ]
finally:
    connection.close()

canonical_after = file_metadata(canonical_db)
assert trial_db.is_relative_to(run_dir), "Trial database escaped the run directory"
assert len(selected_ids) == 5, f"Expected exactly five selected rows, got {selected_ids}"
assert after_counts["eligible_bandera_rss"] == 5, after_counts
assert after_counts["eligible_bandera_non_rss"] == 0, after_counts
assert after_counts["eligible_other_sources"] == 0, after_counts
assert after_counts["processing_count"] == 0, after_counts
assert integrity == ["ok"], integrity
assert canonical_after == canonical_before, {
    "before": canonical_before,
    "after": canonical_after,
}
assert [row["url_id"] for row in selected_after] == selected_ids
for row in selected_after:
    assert row["status"] == "DISCOVERED"
    assert row["retry_count"] == 0
    assert row["next_retry_at"] is None
    assert row["processing_started_at"] is None
    for field in RESET_FAILURE_FIELDS:
        assert row[field] is None, {"url_id": row["url_id"], field: row[field]}

manifest = {
    "run_id": RUN_ID,
    "database_path": str(trial_db),
    "database_inside_run_dir": trial_db.is_relative_to(run_dir),
    "quarantine_next_retry_at": QUARANTINE_RETRY_AT,
    "inherited_statuses": list(ACTIVE_STATUSES),
    "inherited_row_count": len(inherited_rows),
    "quarantined_row_count": len(inherited_ids),
    "quarantined_ids": inherited_ids,
    "original_bandera_rss_retry_count": len(bandera_retry_candidates),
    "selected_ids": selected_ids,
    "selected": [
        {"url_id": int(row["url_id"]), "url": row["url"]}
        for row in selected_rows
    ],
    "reset_fields": [
        "status=DISCOVERED",
        "retry_count=0",
        "next_retry_at=NULL",
        "processing_started_at=NULL",
        *[f"{field}=NULL" for field in RESET_FAILURE_FIELDS],
    ],
    "before_counts": before_counts,
    "after_counts": after_counts,
    "selected_after": selected_after,
    "integrity_check": integrity,
    "canonical_database_before": canonical_before,
    "canonical_database_after": canonical_after,
    "assertions": {
        "eligible_bandera_rss_exactly_5": after_counts["eligible_bandera_rss"] == 5,
        "eligible_bandera_non_rss_zero": after_counts["eligible_bandera_non_rss"] == 0,
        "eligible_other_sources_zero": after_counts["eligible_other_sources"] == 0,
        "processing_zero": after_counts["processing_count"] == 0,
        "integrity_ok": integrity == ["ok"],
        "canonical_hash_size_mtime_unchanged": canonical_after == canonical_before,
    },
}
normalization_manifest_path.write_text(
    json.dumps(manifest, indent=2, default=str) + "\n",
    encoding="utf-8",
)
print(json.dumps(manifest, indent=2, default=str))
