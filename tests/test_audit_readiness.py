import json
from contextlib import contextmanager

from click.testing import CliRunner
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from audit import language_counts
from src.cli.main import audit_discovery_cmd, cli
from src.sources.registry import SourceConfig
from src.storage.database import DatabaseManager
from src.storage.models import Base, Sentence


def test_language_audit_reports_null_and_other_without_writes(tmp_path, capsys):
    db_path = tmp_path / "language-audit.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        session.add_all([
            Sentence(
                sentence_id="S_FIL",
                article_id="A1",
                source_id="source",
                sentence_index=0,
                paragraph_index=0,
                sentence_text="Pilipino.",
                normalized_text="pilipino.",
                language="FILIPINO",
                token_count=1,
                quality_score=1.0,
                content_hash="hash-fil",
            ),
            Sentence(
                sentence_id="S_NULL",
                article_id="A1",
                source_id="source",
                sentence_index=1,
                paragraph_index=0,
                sentence_text="Walang wika.",
                normalized_text="walang wika.",
                language=None,
                token_count=2,
                quality_score=1.0,
                content_hash="hash-null",
            ),
            Sentence(
                sentence_id="S_OTHER",
                article_id="A1",
                source_id="source",
                sentence_index=2,
                paragraph_index=0,
                sentence_text="English.",
                normalized_text="english.",
                language="ENGLISH",
                token_count=1,
                quality_score=1.0,
                content_hash="hash-other",
            ),
        ])
        session.commit()
    finally:
        session.close()

    counts = language_counts(f"sqlite:///{db_path}")

    assert counts == {"total": 3, "filipino": 1, "null": 1, "other": 1}
    with DatabaseManager.read_only(f"sqlite:///{db_path}").get_session() as read_only:
        assert read_only.execute(text("PRAGMA query_only")).scalar() == 1
    assert capsys.readouterr().out == ""


class _AuditConfig:
    def __init__(self, db_url):
        self.db_url = db_url

    def get(self, key, default=None):
        if key == "storage.database_url":
            return self.db_url
        return default


class _AuditRegistry:
    def __init__(self, source):
        self.source = source

    def list_sources(self, enabled_only=False):
        return [self.source]


def test_audit_discovery_reads_legacy_urls_with_core_queries(tmp_path, capsys):
    db_path = tmp_path / "legacy-corpus.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE sources ("
            "source_id VARCHAR(50) PRIMARY KEY, name VARCHAR(100), "
            "domain VARCHAR(100), enabled BOOLEAN, status VARCHAR(20))"
        ))
        connection.execute(text(
            "CREATE TABLE urls ("
            "url_id INTEGER PRIMARY KEY, url TEXT NOT NULL, "
            "canonical_url TEXT, source_id VARCHAR(50) NOT NULL, "
            "status VARCHAR(20) NOT NULL, discovery_method VARCHAR(20) NOT NULL)"
        ))
        connection.execute(text(
            "INSERT INTO urls (url_id, url, source_id, status, discovery_method) "
            "VALUES (1, 'https://example.com/one', 'legacy', 'DISCOVERED', 'RSS'), "
            "       (2, 'https://example.com/two', 'legacy', 'ACCEPTED', 'RSS')"
        ))

    source = SourceConfig(id="legacy", name="Legacy", domain="example.com")
    obj = {
        "config": _AuditConfig(f"sqlite:///{db_path}"),
        "registry": _AuditRegistry(source),
    }

    audit_discovery_cmd.callback.__wrapped__(obj, source=None, method="all")
    payload = json.loads(capsys.readouterr().out)
    report = payload["reports"][0]

    assert report["stored_url_count"] == 2
    assert report["stored_url_statuses"] == {"ACCEPTED": 1, "DISCOVERED": 1}
    assert "discovery_depth" in report["unavailable_fields"]
    assert "frontier_priority" in report["unavailable_fields"]
    assert report["diagnostic_observations"] == []

    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM urls")).scalar_one() == 2
        assert "discovery_depth" not in {
            row[1] for row in connection.execute(text("PRAGMA table_info(urls)"))
        }
