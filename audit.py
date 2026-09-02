# Save this as scratch/audit.py and run it:
from src.storage.database import DatabaseManager
from src.storage.models import Sentence

db = DatabaseManager("sqlite:///data/corpus.db")
with db.get_session() as session:
    # See if there are non-Filipino sentences detected
    total_sentences = session.query(Sentence).count()
    filipino = session.query(Sentence).filter(Sentence.language == "FILIPINO").count()
    other = session.query(Sentence).filter(Sentence.language != "FILIPINO").count()
    print(f"Total Sentences in DB: {total_sentences}")
    print(f"Filipino Sentences: {filipino}")
    print(f"Other/English/Mixed Sentences: {other}")
