from typing import Any, Dict, List
import click
from pathlib import Path
from sqlalchemy import inspect, text
from src.storage.database import DatabaseManager
from src.crawler.config import CrawlerConfig


def apply_additive_migrations(
    engine,
    dry_run: bool = False,
    backfill_counts: bool = True,
    reclassify_legacy: bool = False,
) -> Dict[str, Any]:
    """Apply small, idempotent schema changes and backfills.

    In dry-run mode, checks and reports missing columns, indexes, and
    backfill work without executing mutating DDL/DML.
    """
    inspector = inspect(engine)
    dialect = engine.dialect.name
    datetime_type = "TIMESTAMP" if dialect not in {"sqlite", "mysql"} else "DATETIME"

    additions = {
        "sources": {
            "cooldown_until": f"{datetime_type} NULL",
        },
        "urls": {
            "publication_date_hint": f"{datetime_type} NULL",
            "sitemap_lastmod_hint": f"{datetime_type} NULL",
            "date_hint_source": "VARCHAR(50) NULL",
            "date_hint_confidence": "FLOAT NULL",
            "last_http_status": "INTEGER NULL",
            "failure_class": "VARCHAR(50) NULL",
            "last_attempt_at": f"{datetime_type} NULL",
            "processing_started_at": f"{datetime_type} NULL",
            "sentence_count": "INTEGER DEFAULT 0",
            "extraction_diagnostics": "TEXT NULL",
            "discovery_depth": "INTEGER NOT NULL DEFAULT 0",
            "frontier_priority": "INTEGER NOT NULL DEFAULT 0",
        },
        "articles": {
            "date_source": "VARCHAR(20) NULL",
            "sentence_count": "INTEGER DEFAULT 0",
        },
    }

    report: Dict[str, Any] = {
        "dry_run": dry_run,
        "columns_added": [],
        "columns_pending": [],
        "tables_created": [],
        "tables_pending": [],
        "indexes_created": [],
        "indexes_pending": [],
        "articles_backfilled": 0,
        "urls_backfilled": 0,
        "reclassified_rows": 0,
    }

    table_names = set(inspector.get_table_names())

    diagnostic_tables = {
        "discovery_observations": (
            "CREATE TABLE IF NOT EXISTS discovery_observations ("
            "observation_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "crawl_id VARCHAR(50) NULL, source_id VARCHAR(50) NULL, "
            "discovery_method VARCHAR(30) NOT NULL, root_url TEXT NULL, "
            "outcome VARCHAR(30) NOT NULL, reason VARCHAR(80) NULL, "
            "http_status INTEGER NULL, observation_count INTEGER NOT NULL DEFAULT 0, "
            "metadata_json TEXT NULL, detail_json TEXT NULL, "
            "observed_at DATETIME NOT NULL, "
            "FOREIGN KEY(crawl_id) REFERENCES crawl_runs(crawl_id), "
            "FOREIGN KEY(source_id) REFERENCES sources(source_id)"
            ")"
        ),
        "discovery_observation_samples": (
            "CREATE TABLE IF NOT EXISTS discovery_observation_samples ("
            "sample_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "observation_id INTEGER NOT NULL, candidate_url TEXT NULL, "
            "normalized_url TEXT NULL, observed_at DATETIME NOT NULL, "
            "FOREIGN KEY(observation_id) REFERENCES discovery_observations(observation_id)"
            ")"
        ),
        "url_discovery_edges": (
            "CREATE TABLE IF NOT EXISTS url_discovery_edges ("
            "edge_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "from_url_id INTEGER NOT NULL, to_url_id INTEGER NOT NULL, "
            "crawl_id VARCHAR(50) NULL, discovery_method VARCHAR(20) NOT NULL DEFAULT 'LINK', "
            "depth INTEGER NOT NULL DEFAULT 0, link_text VARCHAR(500) NULL, "
            "rel VARCHAR(200) NULL, discovered_at DATETIME NOT NULL, "
            "FOREIGN KEY(from_url_id) REFERENCES urls(url_id), "
            "FOREIGN KEY(to_url_id) REFERENCES urls(url_id), "
            "FOREIGN KEY(crawl_id) REFERENCES crawl_runs(crawl_id), "
            "UNIQUE(from_url_id, to_url_id, discovery_method)"
            ")"
        ),
    }

    with engine.begin() as connection:
        if dry_run:
            report["tables_pending"].extend(
                table for table in diagnostic_tables if table not in table_names
            )
        for table, ddl in diagnostic_tables.items():
            if table not in table_names and not dry_run:
                connection.execute(text(ddl))
                report["tables_created"].append(table)

        conn_inspector = inspect(connection)
        for table, columns in additions.items():
            if table not in table_names:
                continue
            existing = {column["name"] for column in conn_inspector.get_columns(table)}
            for column, definition in columns.items():
                if column not in existing:
                    if dry_run:
                        report["columns_pending"].append(f"{table}.{column}")
                    else:
                        connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {definition}"))
                        report["columns_added"].append(f"{table}.{column}")

        # Index check / creation
        if "urls" in table_names:
            existing_indexes = {idx["name"] for idx in conn_inspector.get_indexes("urls")}
            url_indexes = {
                "ix_urls_due_work": "CREATE INDEX ix_urls_due_work ON urls (status, next_retry_at, source_id, url_id)",
                "ix_urls_frontier_order": (
                    "CREATE INDEX ix_urls_frontier_order "
                    "ON urls (status, discovery_depth, frontier_priority, url)"
                ),
            }
            for index_name, ddl in url_indexes.items():
                if index_name in existing_indexes:
                    continue
                if dry_run:
                    report["indexes_pending"].append(index_name)
                else:
                    connection.execute(text(ddl))
                    report["indexes_created"].append(index_name)

        diagnostic_indexes = {
            "ix_discovery_observations_crawl_source_outcome": (
                "CREATE INDEX ix_discovery_observations_crawl_source_outcome "
                "ON discovery_observations (crawl_id, source_id, outcome)"
            ),
            "ix_discovery_observations_source_reason_observed_at": (
                "CREATE INDEX ix_discovery_observations_source_reason_observed_at "
                "ON discovery_observations (source_id, reason, observed_at)"
            ),
            "ix_discovery_observation_samples_observation_id": (
                "CREATE INDEX ix_discovery_observation_samples_observation_id "
                "ON discovery_observation_samples (observation_id)"
            ),
            "ix_url_discovery_edges_from_depth": (
                "CREATE INDEX ix_url_discovery_edges_from_depth "
                "ON url_discovery_edges (from_url_id, depth)"
            ),
            "ix_url_discovery_edges_to_url": (
                "CREATE INDEX ix_url_discovery_edges_to_url "
                "ON url_discovery_edges (to_url_id)"
            ),
            "ix_url_discovery_edges_crawl_method": (
                "CREATE INDEX ix_url_discovery_edges_crawl_method "
                "ON url_discovery_edges (crawl_id, discovery_method)"
            ),
        }
        for index_name, ddl in diagnostic_indexes.items():
            if index_name.startswith("ix_discovery_observation_samples"):
                table = "discovery_observation_samples"
            elif index_name.startswith("ix_url_discovery_edges"):
                table = "url_discovery_edges"
            else:
                table = "discovery_observations"
            if dry_run and table not in table_names:
                report["indexes_pending"].append(index_name)
                continue
            if table not in table_names and not dry_run and table in diagnostic_tables:
                # The table may have just been created in this transaction.
                table_exists = True
            else:
                table_exists = table in table_names
            if not table_exists:
                continue
            current_indexes = {idx["name"] for idx in inspect(connection).get_indexes(table)}
            if index_name not in current_indexes:
                if dry_run:
                    report["indexes_pending"].append(index_name)
                else:
                    connection.execute(text(ddl))
                    report["indexes_created"].append(index_name)

        # Backfill sentence counts if requested
        if backfill_counts and "articles" in table_names and "sentences" in table_names:
            if not dry_run:
                # Update articles.sentence_count
                connection.execute(text(
                    "UPDATE articles SET sentence_count = ("
                    "  SELECT COUNT(*) FROM sentences WHERE sentences.article_id = articles.article_id"
                    ") WHERE sentence_count IS NULL OR sentence_count = 0"
                ))
                if "urls" in table_names:
                    connection.execute(text(
                        "UPDATE urls SET sentence_count = ("
                        "  SELECT COALESCE(articles.sentence_count, 0) "
                        "  FROM articles WHERE articles.url_id = urls.url_id"
                        ") WHERE EXISTS (SELECT 1 FROM articles WHERE articles.url_id = urls.url_id)"
                    ))

        # Explicit legacy reclassification (only when explicitly requested)
        if reclassify_legacy and "articles" in table_names and "urls" in table_names:
            if not dry_run:
                # Reclassify articles with zero sentences that were marked ACCEPTED
                res = connection.execute(text(
                    "UPDATE urls SET status = 'NO_SENTENCES' "
                    "WHERE status = 'ACCEPTED' AND sentence_count = 0 "
                    "AND url_id IN (SELECT url_id FROM articles WHERE sentence_count = 0)"
                ))
                report["reclassified_rows"] = res.rowcount if hasattr(res, "rowcount") else 0

    return report


@click.command("init-db")
@click.option("--config-dir", default="config", help="Directory containing config files.")
@click.option("--dry-run", is_flag=True, help="Audit missing migrations without applying changes.")
@click.option("--reclassify-legacy", is_flag=True, help="Explicitly reclassify legacy zero-sentence rows.")
def init_db_cmd(config_dir: str, dry_run: bool = False, reclassify_legacy: bool = False):
    """Initialize or migrate the database schema."""
    cfg_dir = Path(config_dir)
    crawler_path = cfg_dir / "crawler.yaml"
    config = CrawlerConfig(crawler_path)
    
    db_url = config.get("storage.database_url", "sqlite:///data/corpus.db")
    click.echo(f"Checking database at: {db_url}")

    db_mgr = DatabaseManager(db_url)
    if not dry_run:
        db_mgr.init_db()

    report = apply_additive_migrations(
        db_mgr.engine,
        dry_run=dry_run,
        reclassify_legacy=reclassify_legacy
    )
    if dry_run:
        click.echo("Migration Audit (Dry Run):")
        click.echo(f"  Pending Columns: {report['columns_pending']}")
        click.echo(f"  Pending Indexes: {report['indexes_pending']}")
    else:
        click.echo("Database initialization and migrations complete.")
        if report["columns_added"]:
            click.echo(f"  Added Columns: {report['columns_added']}")
        if report["indexes_created"]:
            click.echo(f"  Created Indexes: {report['indexes_created']}")


if __name__ == "__main__":
    init_db_cmd()
