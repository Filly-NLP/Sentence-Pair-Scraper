import os
from pathlib import Path
from urllib.parse import quote
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from src.storage.models import Base

class DatabaseManager:
    def __init__(self, db_url: str, read_only: bool = False):
        self.db_url = db_url
        self.read_only = bool(read_only)

        if self.read_only and db_url.startswith("sqlite"):
            db_path = self._sqlite_path(db_url)
            if db_path is None:
                raise FileNotFoundError(
                    "Read-only SQLite access requires an existing file database; ':memory:' is unavailable."
                )
            if not Path(db_path).is_file():
                raise FileNotFoundError(
                    f"Read-only SQLite database does not exist: {Path(db_path).resolve()}"
                )
            # SQLite URI mode=ro prevents accidental file creation even if a
            # caller points the audit command at a missing/renamed database.
            uri_path = Path(db_path).resolve().as_posix()
            self.db_url = f"sqlite:///file:{quote(uri_path, safe='/::')}?mode=ro&uri=true"
        
        # Ensure parent directory for SQLite exists
        if not self.read_only and db_url.startswith("sqlite:///"):
            db_path = db_url.replace("sqlite:///", "")
            if db_path and db_path != ":memory:":
                os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)

        self.engine = create_engine(
            self.db_url,
            connect_args={"timeout": 30} if self.db_url.startswith("sqlite") else {}
        )

        # Enable WAL only for normal mutable connections. Read-only audit
        # connections must not alter journal mode, migrations, or schema.
        if self.db_url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def set_sqlite_pragma(dbapi_connection, connection_record):
                cursor = dbapi_connection.cursor()
                if self.read_only:
                    cursor.execute("PRAGMA query_only=ON")
                else:
                    cursor.execute("PRAGMA journal_mode=WAL")
                    cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

    @staticmethod
    def _sqlite_path(db_url: str) -> str | None:
        if not db_url.startswith("sqlite:///"):
            return None
        raw = db_url[len("sqlite:///"):]
        if raw in {":memory:", ""} or raw.startswith("file::memory:"):
            return None
        if raw.startswith("file:"):
            raw = raw[len("file:"):].split("?", 1)[0]
        return os.path.abspath(raw)

    @classmethod
    def read_only(cls, db_url: str) -> "DatabaseManager":
        """Open an existing SQLite database using URI mode=ro."""
        return cls(db_url, read_only=True)

    # Explicit alias for integrations that prefer a verb-style constructor.
    open_read_only = read_only

    def init_db(self):
        """Create tables if they do not exist."""
        if self.read_only:
            raise RuntimeError("Cannot initialize or migrate a read-only database connection.")
        Base.metadata.create_all(bind=self.engine)
        # ``create_all`` is intentionally non-destructive and does not add
        # columns to an existing corpus. Apply only small additive migrations.
        from src.storage.migrations import apply_additive_migrations
        apply_additive_migrations(self.engine)

    def get_session(self):
        """Return a new session instance."""
        return self.SessionLocal()
