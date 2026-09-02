from sqlalchemy import create_engine, inspect, text

from src.storage.migrations import apply_additive_migrations


def test_additive_migrations_are_idempotent():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE sources (source_id VARCHAR(50) PRIMARY KEY, name VARCHAR(100), domain VARCHAR(100), enabled BOOLEAN, status VARCHAR(20))"))
        connection.execute(text("CREATE TABLE urls (url_id INTEGER PRIMARY KEY, url TEXT NOT NULL, status VARCHAR(20) DEFAULT 'DISCOVERED', next_retry_at DATETIME, source_id VARCHAR(50))"))
        connection.execute(text("CREATE TABLE articles (article_id VARCHAR(50) PRIMARY KEY, url_id INTEGER)"))
        connection.execute(text("CREATE TABLE sentences (sentence_id VARCHAR(50) PRIMARY KEY, article_id VARCHAR(50))"))

    # Test dry run audit first
    dry_report = apply_additive_migrations(engine, dry_run=True)
    assert "sources.cooldown_until" in dry_report["columns_pending"]
    assert "urls.last_http_status" in dry_report["columns_pending"]
    assert "ix_urls_due_work" in dry_report["indexes_pending"]

    # First migration run
    report1 = apply_additive_migrations(engine, dry_run=False)
    assert "sources.cooldown_until" in report1["columns_added"]
    assert "urls.last_http_status" in report1["columns_added"]
    assert "ix_urls_due_work" in report1["indexes_created"]

    # Second migration run (idempotent)
    report2 = apply_additive_migrations(engine, dry_run=False)
    assert report2["columns_added"] == []
    assert report2["indexes_created"] == []

    source_cols = {c["name"] for c in inspect(engine).get_columns("sources")}
    assert "cooldown_until" in source_cols

    url_cols = {c["name"] for c in inspect(engine).get_columns("urls")}
    assert "publication_date_hint" in url_cols
    assert "sitemap_lastmod_hint" in url_cols
    assert "date_hint_source" in url_cols
    assert "date_hint_confidence" in url_cols
    assert "last_http_status" in url_cols
    assert "failure_class" in url_cols
    assert "last_attempt_at" in url_cols
    assert "processing_started_at" in url_cols
    assert "sentence_count" in url_cols
    assert "extraction_diagnostics" in url_cols

    article_cols = {c["name"] for c in inspect(engine).get_columns("articles")}
    assert "date_source" in article_cols
    assert "sentence_count" in article_cols

    indexes = {idx["name"] for idx in inspect(engine).get_indexes("urls")}
    assert "ix_urls_due_work" in indexes


def test_sentence_count_backfill():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE urls (url_id INTEGER PRIMARY KEY, url TEXT NOT NULL, status VARCHAR(20), next_retry_at DATETIME, source_id VARCHAR(50))"))
        connection.execute(text("CREATE TABLE articles (article_id VARCHAR(50) PRIMARY KEY, url_id INTEGER)"))
        connection.execute(text("CREATE TABLE sentences (sentence_id VARCHAR(50) PRIMARY KEY, article_id VARCHAR(50))"))
        connection.execute(text("INSERT INTO urls (url_id, url) VALUES (1, 'https://example.com/art1')"))
        connection.execute(text("INSERT INTO articles (article_id, url_id) VALUES ('art1', 1)"))
        connection.execute(text("INSERT INTO sentences (sentence_id, article_id) VALUES ('s1', 'art1'), ('s2', 'art1')"))

    apply_additive_migrations(engine, dry_run=False, backfill_counts=True)

    with engine.connect() as connection:
        art_count = connection.execute(text("SELECT sentence_count FROM articles WHERE article_id = 'art1'")).scalar()
        url_count = connection.execute(text("SELECT sentence_count FROM urls WHERE url_id = 1")).scalar()
        assert art_count == 2
        assert url_count == 2

