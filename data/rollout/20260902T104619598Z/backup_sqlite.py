from pathlib import Path
import hashlib
import json
import os
import sqlite3


run_dir = Path("data/rollout/20260902T104619598Z")
source = Path("data/corpus.db").resolve()
partial = run_dir / "corpus.db.backup.sqlite.partial"
final = run_dir / "corpus.db.backup.sqlite"
source_uri = source.as_uri() + "?mode=ro"

src = sqlite3.connect(source_uri, uri=True)
try:
    source_check = [row[0] for row in src.execute("PRAGMA integrity_check").fetchall()]
    source_size = source.stat().st_size
    source_wal = Path(str(source) + "-wal")
    source_shm = Path(str(source) + "-shm")
    dst = sqlite3.connect(partial)
    try:
        dst.execute("PRAGMA journal_mode=DELETE")
        src.backup(dst, pages=1000, sleep=0.05)
        dst.commit()
        backup_check = [row[0] for row in dst.execute("PRAGMA integrity_check").fetchall()]
    finally:
        dst.close()
finally:
    src.close()

if source_check != ["ok"] or backup_check != ["ok"]:
    raise RuntimeError(json.dumps({"source_integrity": source_check, "backup_integrity": backup_check}))

os.replace(partial, final)
digest = hashlib.sha256(final.read_bytes()).hexdigest()
manifest = {
    "run_id": "20260902T104619598Z",
    "backup_method": "sqlite3.Connection.backup",
    "source": str(source),
    "source_size_bytes": source_size,
    "source_wal_size_bytes": source_wal.stat().st_size if source_wal.exists() else None,
    "source_shm_size_bytes": source_shm.stat().st_size if source_shm.exists() else None,
    "backup": str(final.resolve()),
    "backup_size_bytes": final.stat().st_size,
    "backup_sha256": digest,
    "source_integrity_check": source_check,
    "backup_integrity_check": backup_check,
    "recoverability": "verified by opening backup and PRAGMA integrity_check",
}
(run_dir / "backup-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
print(json.dumps(manifest, indent=2))
