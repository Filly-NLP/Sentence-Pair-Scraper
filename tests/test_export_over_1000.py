import csv
import json
from datetime import datetime
from pathlib import Path
from src.storage.exporter import Exporter
from src.storage.models import Article, Sentence, Source


def test_export_more_than_1000_unique_sentences_complete(db_session, tmp_path: Path):
    """Prove that yield_per(1000) does not truncate export and full count (>1000) is exported."""
    source_id = "test_src"
    db_session.add(Source(
        source_id=source_id,
        name="Test Source",
        domain="example.com",
        language="filipino",
        enabled=True
    ))
    
    total_target = 1250
    db_session.add(Article(
        article_id="art_bulk",
        source_id=source_id,
        url_id=1,
        url="https://example.com/art_bulk",
        article_text="Bulk text.",
        content_hash="bulk" * 16,
        publication_date=datetime(2023, 1, 1),
    ))
    
    sentences = []
    for i in range(total_target):
        sentences.append(Sentence(
            sentence_id=f"SENT_BULK_{i:06d}",
            article_id="art_bulk",
            source_id=source_id,
            sentence_index=i,
            paragraph_index=i // 5,
            sentence_text=f"Ito ay pangungusap bilang {i} para sa pagsubok ng maramihang pag-export.",
            normalized_text=f"ito ay pangungusap bilang {i} para sa pagsubok ng maramihang pag-export.",
            language="FILIPINO",
            language_confidence=0.99,
            token_count=11,
            quality_score=1.0,
            content_hash=f"{i:064x}",
            is_duplicate=False,
            is_quote=False,
        ))
    db_session.add_all(sentences)
    db_session.commit()

    # Verify database eligible count
    eligible_db_count = (
        db_session.query(Sentence)
        .filter(Sentence.language == "FILIPINO", Sentence.is_duplicate == False)
        .count()
    )
    assert eligible_db_count == total_target

    exporter = Exporter(db_session)
    jsonl_path = tmp_path / "bulk_export.jsonl"
    csv_path = tmp_path / "bulk_export.csv"

    exported_jsonl = exporter.export_to_jsonl(str(jsonl_path))
    exported_csv = exporter.export_to_csv(str(csv_path))

    assert exported_jsonl == total_target
    assert exported_csv == total_target

    # Verify line counts on disk
    jsonl_lines = jsonl_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(jsonl_lines) == total_target

    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = list(csv.reader(f))
        # 1 header line + 1250 rows
        assert len(reader) == total_target + 1
