import csv
import json
from datetime import datetime

from src.storage.exporter import Exporter
from src.storage.models import Article, Sentence, Source


def test_jsonl_and_csv_share_date_filters(db_session, tmp_path):
    db_session.add(Source(source_id="test", name="Test", domain="example.com", language="filipino", enabled=True))
    db_session.add_all([
        Article(
            article_id="a1", source_id="test", url_id=1, url="https://example.com/a1",
            article_text="Una.", content_hash="a" * 64, publication_date=datetime(2022, 1, 1),
        ),
        Article(
            article_id="a2", source_id="test", url_id=2, url="https://example.com/a2",
            article_text="Ikalawa.", content_hash="b" * 64, publication_date=datetime(2022, 12, 31, 23, 59, 59),
        ),
        Article(
            article_id="a3", source_id="test", url_id=3, url="https://example.com/a3",
            article_text="Ikatlo.", content_hash="e" * 64, publication_date=datetime(2023, 1, 1),
        ),
    ])
    db_session.add_all([
        Sentence(
            sentence_id="s1", article_id="a1", source_id="test", sentence_index=0, paragraph_index=0,
            sentence_text="Una.", normalized_text="una.", language="FILIPINO", language_confidence=1.0,
            token_count=1, quality_score=1.0, content_hash="c" * 64,
        ),
        Sentence(
            sentence_id="s2", article_id="a2", source_id="test", sentence_index=0, paragraph_index=0,
            sentence_text="Ikalawa.", normalized_text="ikalawa.", language="FILIPINO", language_confidence=1.0,
            token_count=1, quality_score=1.0, content_hash="d" * 64,
        ),
        Sentence(
            sentence_id="s3", article_id="a3", source_id="test", sentence_index=0, paragraph_index=0,
            sentence_text="Ikatlo.", normalized_text="ikatlo.", language="FILIPINO", language_confidence=1.0,
            token_count=1, quality_score=1.0, content_hash="f" * 64,
        ),
    ])
    db_session.commit()

    exporter = Exporter(db_session)
    start = datetime(2022, 1, 1)
    # This is the exact midnight value produced by CLI --to-date parsing.
    end = datetime.strptime("2022-12-31", "%Y-%m-%d")
    jsonl_path = tmp_path / "filtered.jsonl"
    csv_path = tmp_path / "filtered.csv"
    assert exporter.export_to_jsonl(str(jsonl_path), from_date=start, to_date=end) == 2
    assert exporter.export_to_csv(str(csv_path), from_date=start, to_date=end) == 2

    json_records = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines()]
    with csv_path.open(newline="", encoding="utf-8") as handle:
        csv_records = list(csv.DictReader(handle))
    assert [record["article_id"] for record in json_records] == ["a1", "a2"]
    assert [record["article_id"] for record in csv_records] == ["a1", "a2"]
