from pathlib import Path
import hashlib
import json
import os
import sqlite3


run_dir = Path("data/rollout/20260902T104619598Z")
backup = run_dir / "corpus.db.backup.sqlite"
partial = run_dir / "trial.db.partial"
trial = run_dir / "trial.db"

source = sqlite3.connect(backup.resolve().as_uri() + "?mode=ro", uri=True)
try:
    source_check = [row[0] for row in source.execute("PRAGMA integrity_check").fetchall()]
    destination = sqlite3.connect(partial)
    try:
        destination.execute("PRAGMA journal_mode=DELETE")
        source.backup(destination, pages=1000, sleep=0.05)
        destination.commit()
        trial_check = [row[0] for row in destination.execute("PRAGMA integrity_check").fetchall()]
    finally:
        destination.close()
finally:
    source.close()

if source_check != ["ok"] or trial_check != ["ok"]:
    raise RuntimeError(json.dumps({"backup_integrity": source_check, "trial_integrity": trial_check}))

os.replace(partial, trial)
manifest = {
    "run_id": "20260902T104619598Z",
    "seed_method": "sqlite3.Connection.backup",
    "source_backup": str(backup.resolve()),
    "source_backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
    "source_backup_integrity_check": source_check,
    "trial_database": str(trial.resolve()),
    "trial_size_bytes": trial.stat().st_size,
    "trial_sha256": hashlib.sha256(trial.read_bytes()).hexdigest(),
    "trial_integrity_check": trial_check,
}
(run_dir / "trial-seed-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
print(json.dumps(manifest, indent=2))
