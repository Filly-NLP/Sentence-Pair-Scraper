from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path


RUN_ID = "20260902T112430893Z"
EXPECTED_BACKUP_SHA256 = (
    "92c347d62ed8d9850ef94c37d8aeb18151c36aa2a83589cf2056247cabad6923"
)

run_dir = Path(__file__).resolve().parent
repo_root = run_dir.parents[2]
source_backup = repo_root / "data" / "rollout" / "20260902T104619598Z" / "corpus.db.backup.sqlite"
partial = run_dir / "trial.db.partial"
trial = run_dir / "trial.db"
manifest_path = run_dir / "trial-seed-manifest.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def integrity_check(connection: sqlite3.Connection) -> list[str]:
    return [row[0] for row in connection.execute("PRAGMA integrity_check").fetchall()]


def table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    tables = ("sources", "urls", "articles", "sentences", "crawl_runs", "crawl_events")
    return {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in tables
    }


if not source_backup.is_file():
    raise FileNotFoundError(f"Verified source backup not found: {source_backup}")
if sha256(source_backup) != EXPECTED_BACKUP_SHA256:
    raise RuntimeError(f"Source backup SHA-256 mismatch: {source_backup}")
if partial.exists() or trial.exists() or manifest_path.exists():
    raise FileExistsError("Fresh seed refused because a trial artifact already exists")

source_uri = source_backup.resolve().as_uri() + "?mode=ro"
source_check: list[str]
destination_check: list[str]
source_counts: dict[str, int]
destination_counts: dict[str, int]

source = sqlite3.connect(source_uri, uri=True)
try:
    source.execute("PRAGMA query_only=ON")
    source_check = integrity_check(source)
    source_counts = table_counts(source)
    if source_check != ["ok"]:
        raise RuntimeError(f"Source backup integrity check failed: {source_check}")

    destination = sqlite3.connect(str(partial))
    try:
        destination.execute("PRAGMA journal_mode=DELETE")
        source.backup(destination, pages=1000, sleep=0.05)
        destination.commit()
        destination_check = integrity_check(destination)
        destination_counts = table_counts(destination)
    finally:
        destination.close()
finally:
    source.close()

if destination_check != ["ok"]:
    raise RuntimeError(f"Trial partial integrity check failed: {destination_check}")
if source_counts != destination_counts:
    raise RuntimeError(
        f"Seed table counts differ: source={source_counts}, destination={destination_counts}"
    )

os.replace(partial, trial)

final = sqlite3.connect(trial.resolve().as_uri() + "?mode=ro", uri=True)
try:
    final.execute("PRAGMA query_only=ON")
    final_check = integrity_check(final)
finally:
    final.close()
if final_check != ["ok"]:
    raise RuntimeError(f"Final trial integrity check failed: {final_check}")

manifest = {
    "run_id": RUN_ID,
    "seed_method": "sqlite3.Connection.backup",
    "source_backup": str(source_backup.resolve()),
    "source_backup_sha256": sha256(source_backup),
    "expected_source_backup_sha256": EXPECTED_BACKUP_SHA256,
    "source_backup_size_bytes": source_backup.stat().st_size,
    "source_backup_integrity_check": source_check,
    "source_table_counts": source_counts,
    "trial_database": str(trial.resolve()),
    "trial_size_bytes": trial.stat().st_size,
    "trial_sha256": sha256(trial),
    "trial_integrity_check": final_check,
    "trial_table_counts": destination_counts,
}
manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
print(json.dumps(manifest, indent=2))
