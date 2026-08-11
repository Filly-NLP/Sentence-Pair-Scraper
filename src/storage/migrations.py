import click
from pathlib import Path
from src.storage.database import DatabaseManager
from src.crawler.config import CrawlerConfig

@click.command("init-db")
@click.option("--config-dir", default="config", help="Directory containing config files.")
def init_db_cmd(config_dir: str):
    """Initialize the database schema."""
    cfg_dir = Path(config_dir)
    crawler_path = cfg_dir / "crawler.yaml"
    config = CrawlerConfig(crawler_path)
    
    db_url = config.get("storage.database_url", "sqlite:///data/corpus.db")
    click.echo(f"Initializing database at: {db_url}")
    
    db_mgr = DatabaseManager(db_url)
    db_mgr.init_db()
    click.echo("Database initialization complete.")

if __name__ == "__main__":
    init_db_cmd()
