import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from src.storage.models import Base

class DatabaseManager:
    def __init__(self, db_url: str):
        self.db_url = db_url
        
        # Ensure parent directory for SQLite exists
        if db_url.startswith("sqlite:///"):
            db_path = db_url.replace("sqlite:///", "")
            if db_path:
                os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)

        self.engine = create_engine(
            self.db_url,
            connect_args={"timeout": 30} if db_url.startswith("sqlite") else {}
        )

        # Enable WAL mode and foreign key support for SQLite
        if db_url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def set_sqlite_pragma(dbapi_connection, connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

    def init_db(self):
        """Create tables if they do not exist."""
        Base.metadata.create_all(bind=self.engine)

    def get_session(self):
        """Return a new session instance."""
        return self.SessionLocal()
