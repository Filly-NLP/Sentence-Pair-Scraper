"""Read-only language audit for the local corpus database."""

from src.storage.database import DatabaseManager
from src.storage.models import Sentence


DEFAULT_DATABASE_URL = "sqlite:///data/corpus.db"


def language_counts(db_url: str = DEFAULT_DATABASE_URL) -> dict[str, int]:
    """Return sentence language counts without opening a writable connection."""
    db = DatabaseManager.read_only(db_url)
    with db.get_session() as session:
        sentence_id = Sentence.sentence_id
        language = Sentence.language
        return {
            "total": session.query(sentence_id).count(),
            "filipino": session.query(sentence_id).filter(language == "FILIPINO").count(),
            "null": session.query(sentence_id).filter(language.is_(None)).count(),
            "other": session.query(sentence_id).filter(
                language.is_not(None), language != "FILIPINO"
            ).count(),
        }


def main() -> None:
    counts = language_counts()
    print(f"Total Sentences in DB: {counts['total']}")
    print(f"Filipino Sentences: {counts['filipino']}")
    print(f"NULL Language Sentences: {counts['null']}")
    print(f"Other/English/Mixed Sentences: {counts['other']}")


if __name__ == "__main__":
    main()
