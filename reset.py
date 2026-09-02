# Open the database and reset failed URLs (run in Python REPL)
from src.storage.database import DatabaseManager
from src.storage.models import URL
from pathlib import Path

db = DatabaseManager("sqlite:///data/corpus.db")
with db.get_session() as s:
    s.query(URL).filter(URL.status == "FAILED").update({"status": "DISCOVERED"})
    s.commit()
    print("Reset complete.")