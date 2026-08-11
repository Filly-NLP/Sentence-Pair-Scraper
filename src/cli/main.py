import click
import asyncio
from pathlib import Path
from rich.console import Console
from src.sources.registry import SourceRegistry
from src.crawler.config import CrawlerConfig
from src.crawler.pipeline import CrawlPipeline
from src.storage.database import DatabaseManager

console = Console()

@click.group()
@click.option("--config-dir", default="config", help="Directory containing configuration files.")
@click.pass_context
def cli(ctx: click.Context, config_dir: str):
    """Filipino News Corpus Scraper - CLI Control Panel"""
    cfg_dir = Path(config_dir)
    sources_path = cfg_dir / "sources.yaml"
    crawler_path = cfg_dir / "crawler.yaml"
    
    try:
        ctx.obj = {
            "registry": SourceRegistry(sources_path),
            "config": CrawlerConfig(crawler_path)
        }
    except Exception as e:
        console.print(f"[red]Error loading configuration: {e}[/red]")
        ctx.exit(1)

@cli.command("discover")
@click.option("--dry-run", is_flag=True, help="Display discovery details without executing.")
@click.pass_obj
def discover_cmd(obj: dict, dry_run: bool):
    """Discover URLs from feeds/sitemaps."""
    registry: SourceRegistry = obj["registry"]
    config: CrawlerConfig = obj["config"]
    
    console.print("[yellow]Discovering sources...[/yellow]")
    if dry_run:
        console.print("[bold green]--- DRY RUN MODE ---[/bold green]")
        for source in registry.list_sources(enabled_only=True):
            console.print(f"Source: [cyan]{source.name}[/cyan] ({source.domain})")
            console.print(f"  RSS feeds: {len(source.rss)}")
            console.print(f"  Sitemaps: {len(source.sitemap)}")
            console.print(f"  Delay: {source.crawl_delay_seconds}s | Max Concurrent: {source.max_concurrent}")
    else:
        db_mgr = DatabaseManager(config.get("storage.database_url"))
        db_mgr.init_db()
        with db_mgr.get_session() as session:
            pipeline = CrawlPipeline(session, config, config.get("http.user_agent"))
            enabled_sources = registry.list_sources(enabled_only=True)
            console.print(f"Executing discovery on {len(enabled_sources)} enabled sources...")
            count = pipeline.discover_urls(enabled_sources)
            console.print(f"[green]Discovery complete. Added {count} new URLs to crawl queue.[/green]")

@cli.command("crawl")
@click.option("--source", help="Restrict crawl run to a specific source ID.")
@click.pass_obj
def crawl_cmd(obj: dict, source: str):
    """Crawl, extract, filter and store sentences."""
    config: CrawlerConfig = obj["config"]
    db_mgr = DatabaseManager(config.get("storage.database_url"))
    
    async def run_crawl():
        with db_mgr.get_session() as session:
            pipeline = CrawlPipeline(session, config, config.get("http.user_agent"))
            console.print("[green]Processing queued URLs...[/green]")
            await pipeline.crawl_queued_urls(source_id=source)
            console.print("[green]Queued processing complete.[/green]")

@cli.command("export")
@click.option("--output", required=True, help="Output file path (e.g. data/exports/corpus.jsonl)")
@click.option("--format", type=click.Choice(["jsonl", "csv"]), default="jsonl", help="Output file format.")
@click.option("--source", help="Filter exports by specific source ID.")
@click.option("--from-date", help="Cutoff start date (YYYY-MM-DD) for exported sentences.")
@click.option("--to-date", help="Cutoff end date (YYYY-MM-DD) for exported sentences.")
@click.option("--append", is_flag=True, help="Append to existing output file without rewriting header (CSV only).")
@click.option("--auto-version", is_flag=True, help="Automatically append a timestamp to the output filename.")
@click.pass_obj
def export_cmd(obj: dict, output: str, format: str, source: str, from_date: str, to_date: str, append: bool, auto_version: bool):
    """Export clean Filipino sentence corpus."""
    config: CrawlerConfig = obj["config"]
    db_mgr = DatabaseManager(config.get("storage.database_url"))
    
    # Handle auto-versioning
    if auto_version:
        from datetime import datetime as dt
        path = Path(output)
        timestamp = dt.now().strftime("%Y-%m-%dT%H%M%S")
        output = str(path.parent / f"{path.stem}_{timestamp}{path.suffix}")

    # Parse dates
    from_dt = None
    to_dt = None
    try:
        from datetime import datetime
        if from_date:
            from_dt = datetime.strptime(from_date, "%Y-%m-%d")
        if to_date:
            to_dt = datetime.strptime(to_date, "%Y-%m-%d")
    except ValueError as e:
        click.echo(f"Error parsing dates (should be YYYY-MM-DD): {e}", err=True)
        return

    from src.storage.exporter import Exporter
    with db_mgr.get_session() as session:
        exporter = Exporter(session)
        if format == "jsonl":
            count = exporter.export_to_jsonl(output, source_id=source)
        else:
            count = exporter.export_to_csv(output, source_id=source, from_date=from_dt, to_date=to_dt, append=append)
        click.echo(f"Successfully exported {count} unique Filipino sentences to {output}.")



@cli.command("stats")
@click.pass_obj
def stats_cmd(obj: dict):
    """Display Filipino News Corpus statistics."""
    config: CrawlerConfig = obj["config"]
    db_mgr = DatabaseManager(config.get("storage.database_url"))
    
    from src.storage.models import URL, Article, Sentence
    with db_mgr.get_session() as session:
        urls = session.query(URL).count()
        articles = session.query(Article).count()
        sentences = session.query(Sentence).count()
        
        console.print("[bold blue]Corpus Stats Summary[/bold blue]")
        console.print(f"Discovered URL queue: {urls}")
        console.print(f"Downloaded Articles:  {articles}")
        console.print(f"Accepted Filipino sentences: {sentences}")


if __name__ == "__main__":
    cli()
