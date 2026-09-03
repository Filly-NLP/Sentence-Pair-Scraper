from datetime import datetime
from pathlib import Path
from typing import Optional
import asyncio
import json
import click
from rich.console import Console
from sqlalchemy import MetaData, Table, func, inspect, or_, select

from src.crawler.archive_discovery import ArchiveDiscoveryEngine
from src.crawler.config import CrawlerConfig, CommonCrawlConfig
from src.crawler.common_crawl_discovery import CommonCrawlDiscoveryEngine
from src.crawler.link_discovery import LinkDiscoveryEngine
from src.crawler.pipeline import CrawlPipeline
from src.sources.registry import ArchiveConfig, SourceRegistry
from src.storage.database import DatabaseManager
from src.crawler.discovery_reporting import DiscoveryReport, RootAudit

console = Console()

_CRAWL_DISCOVERY_METHOD_VALUES = {
    "": None,
    "all": None,
    "default": ("RSS", "SITEMAP"),
    "rss": ("RSS",),
    "sitemap": ("SITEMAP",),
    "archive": ("ARCHIVE",),
    "link": ("LINK",),
    "common_crawl": ("COMMON_CRAWL",),
}


def _crawl_discovery_method_values(
    discovery_method: Optional[str],
) -> Optional[tuple[str, ...]]:
    """Translate the CLI provenance selector for its queue-count query.

    Keep this CLI-only calculation independent of the replaceable pipeline
    class so lightweight test doubles do not need private pipeline helpers.
    The pipeline still validates and applies the selector for the crawl run.
    """
    if discovery_method is None:
        return None
    if not isinstance(discovery_method, str):
        raise ValueError("discovery_method must be a string")

    normalized = discovery_method.strip().lower().replace("-", "_")
    try:
        return _CRAWL_DISCOVERY_METHOD_VALUES[normalized]
    except KeyError as exc:
        allowed = ", ".join(
            sorted(key.upper() for key in _CRAWL_DISCOVERY_METHOD_VALUES if key)
        )
        raise ValueError(
            f"unsupported discovery_method {discovery_method!r}; expected one of {allowed}"
        ) from exc

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
@click.option("--source", help="Filter discovery to a specific source ID.")
@click.option("--mode", type=click.Choice(["default", "archive", "common-crawl"]), default="default", help="Discovery mode: feeds/sitemaps (default), archive, or Common Crawl index.")
@click.option("--from-date", help="Start date for archive traversal (YYYY-MM-DD).")
@click.option("--to-date", help="End date for archive traversal (YYYY-MM-DD).")
@click.option("--granularity", type=click.Choice(["year", "month", "day"]), help="Archive period granularity override.")
@click.option("--max-periods", type=int, help="Maximum archive periods to traverse.")
@click.option("--max-pages", type=int, help="Maximum archive pages per period.")
@click.option(
    "--override-disabled",
    is_flag=True,
    help="Explicitly run a disabled archive; requires --source, --from-date, and --to-date.",
)
@click.option("--dry-run", is_flag=True, help="Display discovery details without executing.")
@click.pass_obj
def discover_cmd(
    obj: dict,
    source: Optional[str],
    mode: str,
    from_date: Optional[str],
    to_date: Optional[str],
    granularity: Optional[str],
    max_periods: Optional[int],
    max_pages: Optional[int],
    override_disabled: bool,
    dry_run: bool,
):
    """Discover URLs from feeds/sitemaps or archive traversal."""
    registry: SourceRegistry = obj["registry"]
    config: CrawlerConfig = obj["config"]

    parsed_from = datetime.strptime(from_date, "%Y-%m-%d").date() if from_date else None
    parsed_to = datetime.strptime(to_date, "%Y-%m-%d").date() if to_date else None

    console.print(f"[yellow]Discovering sources (mode: {mode})...[/yellow]")
    enabled_sources = registry.list_sources(enabled_only=True)
    if source:
        enabled_sources = [s for s in enabled_sources if s.id == source]
        if not enabled_sources:
            console.print(f"[red]Source '{source}' not found or disabled.[/red]")
            return

    if mode == "archive":
        if override_disabled and (not source or not from_date or not to_date):
            raise click.UsageError(
                "--override-disabled requires explicit --source, --from-date, and --to-date."
            )
        if not source:
            raise click.UsageError(
                "Archive discovery is bounded to one source; provide --source."
            )
        selected_source = enabled_sources[0] if enabled_sources else None
        selected_archive = selected_source.archive if selected_source else None
        if selected_source is None or selected_archive is None:
            console.print(
                "[yellow]Archive discovery refused: the selected source has no archive configuration. "
                "No HTTP requests were made.[/yellow]"
            )
            return
        if not selected_archive.enabled and not override_disabled:
            console.print(
                "[yellow]Archive discovery refused: archive is disabled for the selected source. "
                "Use --override-disabled with explicit dates only after review. "
                "No HTTP requests were made.[/yellow]"
            )
            return
        if dry_run:
            console.print("[bold green]--- ARCHIVE DISCOVERY DRY RUN ---[/bold green]")
            cutoff_dt = datetime.strptime(config.get("crawler.date_cutoff", "2022-01-01"), "%Y-%m-%d")
            engine = ArchiveDiscoveryEngine(
                db_session=None,
                user_agent=config.get("http.user_agent", "SentencePairBot/1.0"),
                date_cutoff=cutoff_dt,
                apply_delay=False,
            )
            for src in [selected_source]:
                cfg = src.archive
                is_opted_in = bool(cfg and (cfg.enabled or override_disabled))
                console.print(f"\nSource: [bold cyan]{src.name}[/bold cyan] ({src.domain}) [id={src.id}]")
                console.print(f"  Archive Configured: {cfg is not None} | Enabled in config: {cfg.enabled if cfg else False} | Selected for run: {is_opted_in}")
                if is_opted_in:
                    eff_gran = granularity or (cfg.granularity if cfg else "day")
                    eff_max_p = max_periods or (cfg.max_periods if cfg else 100)
                    s_d = parsed_from or (datetime.strptime(cfg.start_date, "%Y-%m-%d").date() if cfg and cfg.start_date else cutoff_dt.date())
                    e_d = parsed_to or (datetime.strptime(cfg.end_date, "%Y-%m-%d").date() if cfg and cfg.end_date else datetime.utcnow().date())
                    periods = list(ArchiveDiscoveryEngine.generate_periods(eff_gran, s_d, e_d, eff_max_p))
                    console.print(f"  Granularity: {eff_gran} | Planned Periods: {len(periods)} (Range: {s_d} to {e_d})")
                    if periods:
                        sample_urls = engine._format_period_urls(cfg or ArchiveConfig(granularity=eff_gran), src, periods[0][1])
                        console.print(f"  Sample Root URL: {sample_urls[0] if sample_urls else '(none)'}")
                    console.print(f"  Pagination Mode: {cfg.pagination_mode if cfg else 'none'} | Max Pages/Period: {max_pages or (cfg.max_pages_per_period if cfg else 10)}")
                    console.print(f"  Article Link Selector: {cfg.article_link_selector if cfg else 'a[href]'}")
        else:
            db_mgr = DatabaseManager(config.get("storage.database_url"))
            db_mgr.init_db()
            with db_mgr.get_session() as session:
                pipeline = CrawlPipeline(session, config, config.get("http.user_agent"))
                console.print("Executing archive discovery on 1 source...")
                found, count = pipeline.discover_archive_urls(
                    [selected_source],
                    from_date=parsed_from,
                    to_date=parsed_to,
                    override_granularity=granularity,
                    max_periods_override=max_periods,
                    max_pages_override=max_pages,
                    allow_disabled_override=override_disabled,
                )
                console.print(f"[green]Archive discovery complete. Found {found} total URLs ({count} new).[/green]")
        return

    if mode == "common-crawl":
        if not source:
            raise click.UsageError("Common Crawl discovery is bounded to one source; provide --source.")
        selected_source = enabled_sources[0] if enabled_sources else None
        if selected_source is None:
            console.print(f"[red]Source '{source}' not found or disabled.[/red]")
            return
        settings = CommonCrawlConfig.from_mapping(config.get("common_crawl", {}) or {})
        source_enabled, patterns = CommonCrawlDiscoveryEngine._source_common_crawl(selected_source)

        if dry_run:
            console.print("[bold green]--- COMMON CRAWL DISCOVERY DRY RUN ---[/bold green]")
            console.print(f"  Global gate: {settings.enabled}")
            console.print(f"  Source gate: {source_enabled}")
            console.print(f"  Collection: {settings.index_collection or '(not pinned)'}")
            console.print(f"  Source: {selected_source.id}")
            console.print(f"  URL patterns: {patterns or ['(none)']}")
            console.print(f"  Max requests/source run: {settings.max_requests_per_source_run}")
            console.print(f"  Max index pages: {settings.max_index_pages}")
            console.print(f"  Max candidates/source run: {settings.max_candidates_per_source_run}")
            console.print(f"  Max response bytes: {settings.max_response_bytes}")
            console.print(f"  Max total response bytes/source run: {settings.max_total_response_bytes_per_source_run}")
            console.print(f"  Timeout seconds: {settings.timeout_seconds}")
            console.print(f"  Cache directory: {settings.cache_dir}")
            console.print("  http_requests: false")
            console.print("  database_access: false")
            return

        if not settings.enabled or not source_enabled or not settings.index_collection or not patterns:
            console.print(
                "[yellow]Common Crawl discovery refused: both global and source gates, "
                "an explicit collection, and at least one source pattern are required. "
                "No HTTP requests or database writes were made.[/yellow]"
            )
            return

        db_mgr = DatabaseManager(config.get("storage.database_url"))
        db_mgr.init_db()
        with db_mgr.get_session() as session:
            pipeline = CrawlPipeline(session, config, config.get("http.user_agent"))
            found, count = pipeline.discover_common_crawl_urls([selected_source])
            console.print(f"[green]Common Crawl discovery complete. Found {found} index candidates ({count} new).[/green]")
        return

    if dry_run:
        console.print("[bold green]--- DRY RUN MODE ---[/bold green]")
        for src in enabled_sources:
            console.print(f"\nSource: [bold cyan]{src.name}[/bold cyan] ({src.domain}) [id={src.id}]")
            console.print(f"  RSS feeds: {len(src.rss)}")
            for rss in src.rss:
                console.print(f"    - {rss.url} (category: {rss.category})")
            console.print(f"  Configured Sitemaps: {len(src.sitemap)}")
            for sm in src.sitemap:
                console.print(f"    - {sm.url}")
            console.print(f"  Allowed Sitemap Hosts: {src.allowed_sitemap_hosts or ['(default domain only)']}")
            console.print(f"  Delay: {src.crawl_delay_seconds}s | Max Concurrent: {src.max_concurrent}")
    else:
        db_mgr = DatabaseManager(config.get("storage.database_url"))
        db_mgr.init_db()
        with db_mgr.get_session() as session:
            pipeline = CrawlPipeline(session, config, config.get("http.user_agent"))
            console.print(f"Executing discovery on {len(enabled_sources)} enabled sources...")
            found, count = pipeline.discover_urls(enabled_sources)
            console.print(f"[green]Discovery complete. Found {found} total URLs ({count} new) across enabled sources.[/green]")


@cli.command("audit-discovery")
@click.option("--source", help="Filter the audit to a source ID.")
@click.option(
    "--method",
    type=click.Choice(["rss", "sitemap", "common-crawl", "all"], case_sensitive=False),
    default="all",
    show_default=True,
    help="Show stored diagnostics/configured roots for one discovery method.",
)
@click.pass_obj
def audit_discovery_cmd(obj: dict, source: Optional[str], method: str):
    """Inspect discovery evidence without fetching any publisher URL.

    This command is intentionally separate from ``discover --dry-run``:
    it opens SQLite in URI read-only mode and reports configured roots,
    persisted diagnostic aggregates, and stored queue counts only.
    """
    config: CrawlerConfig = obj["config"]
    registry: SourceRegistry = obj["registry"]
    db_url = config.get("storage.database_url", "sqlite:///data/corpus.db")
    try:
        db_mgr = DatabaseManager.read_only(db_url)
    except (FileNotFoundError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc

    selected = registry.list_sources(enabled_only=False)
    if source:
        selected = [item for item in selected if item.id == source]
        if not selected:
            raise click.ClickException(f"Source '{source}' not found in configuration.")

    method_upper = "COMMON_CRAWL" if method.lower() == "common-crawl" else method.upper()
    payload = []
    with db_mgr.get_session() as session:
        from sqlalchemy.exc import SQLAlchemyError
        from src.storage.models import DiscoveryObservation, URL

        # Do not query the mapped URL entity here.  An active legacy corpus
        # can have a valid ``urls`` table without additive columns such as
        # discovery_depth; an ORM entity query would select those missing
        # columns before it can calculate a count.
        bind = session.get_bind()
        inspector = inspect(bind)
        table_names = set(inspector.get_table_names())
        expected_url_fields = set(URL.__table__.columns.keys())
        url_table = None
        available_url_fields = set()
        if "urls" in table_names:
            url_table = Table("urls", MetaData(), autoload_with=bind)
            available_url_fields = set(url_table.c.keys())
        unavailable_url_fields = sorted(expected_url_fields - available_url_fields)

        for src in selected:
            report = DiscoveryReport(source_id=src.id, discovery_method=method_upper)
            report.metadata["unavailable_fields"] = list(unavailable_url_fields)
            if method_upper in {"RSS", "ALL"}:
                for feed in src.rss:
                    report.roots.append(RootAudit(
                        configured_url=feed.url,
                        origin_configured=True,
                        requested_url=feed.url,
                        document_kind="not_fetched",
                    ))
            if method_upper in {"SITEMAP", "ALL"}:
                for sitemap in src.sitemap:
                    report.roots.append(RootAudit(
                        configured_url=sitemap.url,
                        origin_configured=True,
                        requested_url=sitemap.url,
                        document_kind="not_fetched",
                    ))
            if method_upper == "COMMON_CRAWL":
                cc_settings = CommonCrawlConfig.from_mapping(config.get("common_crawl", {}) or {})
                source_cc = src.common_crawl
                cc_patterns = (
                    source_cc.get("url_patterns", [])
                    if isinstance(source_cc, dict)
                    else getattr(source_cc, "url_patterns", [])
                )
                if cc_settings.index_collection:
                    cc_endpoint = f"https://index.commoncrawl.org/{cc_settings.index_collection}-index"
                    for pattern in cc_patterns if isinstance(cc_patterns, (list, tuple)) else []:
                        report.roots.append(RootAudit(
                            configured_url=cc_endpoint,
                            origin_configured=True,
                            requested_url=cc_endpoint,
                            document_kind="not_fetched",
                        ))

            report.metadata["stored_url_count"] = None
            report.metadata["stored_url_statuses"] = {}
            if url_table is not None and "source_id" in available_url_fields:
                url_filters = [url_table.c.source_id == src.id]
                method_filter_available = (
                    method_upper == "ALL" or "discovery_method" in available_url_fields
                )
                if method_upper != "ALL" and method_filter_available:
                    url_filters.append(url_table.c.discovery_method == method_upper)
                if method_filter_available:
                    report.metadata["stored_url_count"] = session.execute(
                        select(func.count()).select_from(url_table).where(*url_filters)
                    ).scalar_one()
                    if "status" in available_url_fields:
                        status_rows = session.execute(
                            select(url_table.c.status, func.count())
                            .select_from(url_table)
                            .where(*url_filters)
                            .group_by(url_table.c.status)
                        ).all()
                        report.metadata["stored_url_statuses"] = {
                            status: count for status, count in status_rows
                        }

            observations = []
            try:
                query = session.query(DiscoveryObservation).filter(
                    DiscoveryObservation.source_id == src.id
                )
                if method_upper != "ALL":
                    query = query.filter(DiscoveryObservation.discovery_method == method_upper)
                for observation in query.order_by(DiscoveryObservation.observed_at.desc()).all():
                    observations.append({
                        "observation_id": observation.observation_id,
                        "crawl_id": observation.crawl_id,
                        "discovery_method": observation.discovery_method,
                        "root_url": observation.root_url,
                        "outcome": observation.outcome,
                        "reason": observation.reason,
                        "http_status": observation.http_status,
                        "observation_count": observation.observation_count,
                        "metadata_json": observation.metadata_json,
                        "observed_at": observation.observed_at.isoformat() if observation.observed_at else None,
                    })
            except SQLAlchemyError:
                # A pre-adaptation local DB may not have diagnostic tables;
                # configured roots and stored URL counts remain useful.
                session.rollback()
                observations = []
            report.metadata["diagnostic_observations"] = observations
            payload.append(report.to_dict())

    click.echo(json.dumps({"reports": payload}, indent=2, sort_keys=True, default=str))


@cli.command("audit-link-frontier")
@click.option("--source", required=True, help="Source ID whose article classifier should be used.")
@click.option("--html-file", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path), help="Local HTML fixture to analyze.")
@click.option("--base-url", required=True, help="Final or explicit base URL used to resolve relative links.")
@click.option("--parent-depth", type=click.IntRange(min=0), default=0, show_default=True, help="Depth of the fetched parent page.")
@click.pass_obj
def audit_link_frontier_cmd(obj: dict, source: str, html_file: Path, base_url: str, parent_depth: int):
    """Audit Stage 4A links from local HTML without HTTP or database access."""
    config: CrawlerConfig = obj["config"]
    registry: SourceRegistry = obj["registry"]
    source_cfg = registry.get_source(source)
    if source_cfg is None:
        raise click.ClickException(f"Source '{source}' not found in configuration.")
    html_path = Path(html_file)
    try:
        html = html_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise click.ClickException(f"Unable to read HTML fixture '{html_path}': {exc}") from exc

    settings = config.get("link_discovery", {}) or {}
    if not isinstance(settings, dict):
        settings = {}
    source_link_cfg = getattr(source_cfg, "link_discovery", None)
    global_enabled = bool(settings.get("enabled", False))
    source_enabled = bool(getattr(source_link_cfg, "enabled", False))
    engine = LinkDiscoveryEngine()
    batch = engine.analyze_html(
        html,
        final_url=base_url,
        source_config=source_cfg,
        parent_depth=parent_depth,
        explicit_limits={
            "max_depth": int(settings.get("max_depth", 1)),
            "max_links_per_page": int(settings.get("max_links_per_page", 100)),
            "max_candidates": int(settings.get("max_candidates_per_source_run", 500)),
            "allow_query_parameters": bool(settings.get("allow_query_parameters", False)),
        },
    )
    report = batch.to_dict()
    payload = {
        "command": "audit-link-frontier",
        "source": {"id": source_cfg.id, "name": source_cfg.name, "domain": source_cfg.domain},
        "base_url": base_url,
        "parent_depth": parent_depth,
        "runtime_enablement": {
            "global_gate": global_enabled,
            "source_gate": source_enabled,
            "runtime_enabled": global_enabled and source_enabled,
            "offline_audit": True,
            "http_requests": False,
            "database_access": False,
            "note": "Offline audit runs regardless of runtime gates; no URLs or edges are persisted.",
        },
        "report": report,
        "reconciled": {
            "counters": report["counters"],
            "rejection_reasons": report["rejection_reasons"],
            "truncation_details": report["truncation_details"],
        },
        "candidates": report["candidates"],
    }
    click.echo(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))

@cli.command("crawl")
@click.option("--source", help="Restrict crawl run to a specific source ID.")
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=None,
    help="Maximum number of URLs to process in this crawl run.",
)
@click.option(
    "--discovery-method",
    type=click.Choice(
        ["all", "default", "rss", "sitemap", "archive", "link", "common-crawl"],
        case_sensitive=False,
    ),
    default="all",
    show_default=True,
    help="Filter queued URLs by provenance; 'default' means RSS or SITEMAP.",
)
@click.pass_obj
def crawl_cmd(
    obj: dict,
    source: Optional[str],
    limit: Optional[int] = None,
    discovery_method: str = "all",
):
    """Crawl, extract, filter and store sentences."""
    config: CrawlerConfig = obj["config"]
    registry: Optional[SourceRegistry] = obj.get("registry")
    method_values = _crawl_discovery_method_values(discovery_method)
    db_mgr = DatabaseManager(config.get("storage.database_url"))
    db_mgr.init_db()

    async def run_crawl():
        from src.storage.models import URL
        from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn, TimeRemainingColumn

        with db_mgr.get_session() as session:
            pipeline = CrawlPipeline(
                session,
                config,
                config.get("http.user_agent"),
                source_registry=registry,
            )

            # Count the same due states and provenance selected by the
            # coordinator so progress reflects the bounded run.
            now = datetime.utcnow()
            query = session.query(URL).filter(
                or_(
                    URL.status == "DISCOVERED",
                    (URL.status == "RETRY_WAIT")
                    & ((URL.next_retry_at == None) | (URL.next_retry_at <= now)),
                )
            )
            if source:
                query = query.filter(URL.source_id == source)
            if method_values:
                query = query.filter(URL.discovery_method.in_(method_values))
            total_queued = query.count()

            if total_queued == 0:
                console.print("[yellow]No queued URLs to crawl. Run 'discover' first.[/yellow]")
                return

            run_total = total_queued if limit is None else min(total_queued, limit)
            console.print(f"[green]Starting crawl for {run_total} queued URLs...[/green]")

            with Progress(
                SpinnerColumn(spinner_name="line"),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                TimeElapsedColumn(),
                TextColumn("/"),
                TimeRemainingColumn(),
                console=console
            ) as progress:
                task = progress.add_task("[cyan]Crawling...", total=run_total)

                def update_progress(url: str):
                    progress.advance(task, 1)
                    progress.update(task, description=f"[cyan]Crawling: {url[:40]}...")

                await pipeline.crawl_queued_urls(
                    source_id=source,
                    progress_callback=update_progress,
                    limit=limit,
                    discovery_method=discovery_method,
                )
                progress.update(task, description="[green]Crawl complete![/green]")

    asyncio.run(run_crawl())

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
    db_url = config.get("storage.database_url", "sqlite:///data/corpus.db")
    try:
        # Export is read-only by contract.  Schema changes belong to the
        # explicit init-db command and must never happen as a side effect of
        # producing an artifact.
        db_mgr = DatabaseManager.read_only(db_url)
    except (FileNotFoundError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc

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
    try:
        with db_mgr.get_session() as session:
            exporter = Exporter(session)
            if format == "jsonl":
                count = exporter.export_to_jsonl(output, source_id=source, from_date=from_dt, to_date=to_dt)
            else:
                count = exporter.export_to_csv(output, source_id=source, from_date=from_dt, to_date=to_dt, append=append)
    except Exception as exc:
        from sqlalchemy.exc import OperationalError
        if isinstance(exc, OperationalError):
            raise click.ClickException(
                "Export requires a compatible database schema. Run 'init-db' "
                "on an approved copy before retrying; export never migrates "
                "the active corpus automatically."
            ) from exc
        raise
    click.echo(f"Successfully exported {count} unique Filipino sentences to {output}.")



@cli.command("requeue")
@click.option("--source", help="Filter URLs by source ID.")
@click.option("--status", help="Filter URLs by current status (e.g. RETRY_WAIT, FAILED).")
@click.option("--due", is_flag=True, help="Filter for only due retry URLs (next_retry_at <= now).")
@click.option("--include-terminal", is_flag=True, help="Allow requeueing TERMINAL_FAILED records.")
@click.option("--include-blocked", is_flag=True, help="Allow requeueing BLOCKED records.")
@click.option("--execute", is_flag=True, help="Apply mutations (defaults to dry-run mode).")
@click.pass_obj
def requeue_cmd(obj: dict, source: Optional[str], status: Optional[str], due: bool, include_terminal: bool, include_blocked: bool, execute: bool):
    """Safely requeue URLs for crawling with dry-run-first safety."""
    from src.storage.models import URL, URLStatus
    from sqlalchemy import or_
    from datetime import datetime

    config: CrawlerConfig = obj["config"]
    db_url = config.get("storage.database_url", "sqlite:///data/corpus.db")
    if not execute:
        db_mgr = DatabaseManager.read_only(db_url)
    else:
        db_mgr = DatabaseManager(db_url)
        db_mgr.init_db()

    with db_mgr.get_session() as session:
        query = session.query(URL)

        if status:
            target_status = status.upper()
            if target_status == URLStatus.TERMINAL_FAILED and not include_terminal:
                console.print("[red]Refusing to requeue TERMINAL_FAILED without --include-terminal flag.[/red]")
                return
            if target_status == URLStatus.BLOCKED and not include_blocked:
                console.print("[red]Refusing to requeue BLOCKED without --include-blocked flag.[/red]")
                return
            query = query.filter(URL.status == target_status)
        else:
            allowed_statuses = [URLStatus.RETRY_WAIT, URLStatus.FAILED]
            if include_terminal:
                allowed_statuses.append(URLStatus.TERMINAL_FAILED)
            if include_blocked:
                allowed_statuses.append(URLStatus.BLOCKED)
            query = query.filter(URL.status.in_(allowed_statuses))

        if source:
            query = query.filter(URL.source_id == source)

        if due:
            now = datetime.utcnow()
            query = query.filter(or_(URL.next_retry_at == None, URL.next_retry_at <= now))

        try:
            matched_urls = query.all()
        except Exception as exc:
            from sqlalchemy.exc import OperationalError
            if isinstance(exc, OperationalError) and not execute:
                console.print(
                    "[yellow]Requeue audit unavailable for this legacy schema. "
                    "Run 'init-db' only on an approved copy, then retry the dry run.[/yellow]"
                )
                return
            raise
        count = len(matched_urls)

        console.print(f"[bold]Requeue Scope Analysis:[/bold]")
        console.print(f"  Matching rows: [cyan]{count}[/cyan]")
        console.print(f"  Filters: source={source or 'ALL'}, status={status or 'RETRY_WAIT, FAILED'}, due_only={due}")
        console.print(f"  Permissions: include_terminal={include_terminal}, include_blocked={include_blocked}")

        if not execute:
            console.print("[bold yellow]--- DRY RUN MODE (no changes applied) ---[/bold yellow]")
            console.print("Pass [bold green]--execute[/bold green] to reset matched records to DISCOVERED.")
            if count > 0:
                sample = matched_urls[:5]
                console.print("Sample matched URLs:")
                for u in sample:
                    console.print(f"  - [{u.status}] {u.url} (retries: {u.retry_count}, next: {u.next_retry_at})")
            return

        for u in matched_urls:
            u.status = URLStatus.DISCOVERED
            u.next_retry_at = None
            u.error_reason = None
            u.failure_class = None

        session.commit()
        console.print(f"[bold green]Successfully requeued {count} URLs to DISCOVERED status.[/bold green]")


@cli.command("init-db")
@click.option("--dry-run", is_flag=True, help="Audit missing migrations without applying changes.")
@click.option("--reclassify-legacy", is_flag=True, help="Explicitly reclassify legacy zero-sentence rows.")
@click.pass_obj
def init_db_cmd(obj: dict, dry_run: bool, reclassify_legacy: bool):
    """Initialize or migrate the database schema."""
    config: CrawlerConfig = obj["config"]
    db_url = config.get("storage.database_url", "sqlite:///data/corpus.db")
    console.print(f"Checking database at: [cyan]{db_url}[/cyan]")
    db_mgr = DatabaseManager(db_url)
    if not dry_run:
        db_mgr.init_db()
    from src.storage.migrations import apply_additive_migrations
    report = apply_additive_migrations(
        db_mgr.engine,
        dry_run=dry_run,
        reclassify_legacy=reclassify_legacy
    )
    if dry_run:
        console.print("[bold green]--- MIGRATION AUDIT (DRY RUN) ---[/bold green]")
        console.print(f"  Pending Columns: {report['columns_pending']}")
        console.print(f"  Pending Indexes: {report['indexes_pending']}")
    else:
        console.print("[green]Database initialization and migrations complete.[/green]")
        if report["columns_added"]:
            console.print(f"  Added Columns: {report['columns_added']}")
        if report["indexes_created"]:
            console.print(f"  Created Indexes: {report['indexes_created']}")
        if report["reclassified_rows"]:
            console.print(f"  Reclassified rows: {report['reclassified_rows']}")


@cli.command("stats")
@click.pass_obj
def stats_cmd(obj: dict):
    """Display Filipino News Corpus statistics."""
    config: CrawlerConfig = obj["config"]
    db_mgr = DatabaseManager.read_only(
        config.get("storage.database_url", "sqlite:///data/corpus.db")
    )

    from src.storage.models import URL, Article, Sentence, CrawlRun, CrawlEvent
    from sqlalchemy import func
    with db_mgr.get_session() as session:
        try:
            total_urls = session.query(URL).count()
        except Exception as exc:
            from sqlalchemy.exc import OperationalError
            if not isinstance(exc, OperationalError):
                raise
            # Article/sentence counts remain useful on a pre-frontier
            # database, while mapped URL queries would select columns that do
            # not exist yet.  Keep stats read-only and make the limitation
            # explicit instead of auto-migrating or crashing.
            articles = session.query(Article).count()
            sentences = session.query(Sentence).count()
            console.print("[bold blue]Corpus Stats Summary[/bold blue]")
            console.print("Total Discovered URLs: unavailable (legacy URL schema)")
            console.print(f"Downloaded Articles:   {articles}")
            console.print(f"Accepted Sentences:    {sentences}")
            console.print(
                "[yellow]URL-level statistics require 'init-db' on an approved copy; "
                "this read-only command made no schema changes.[/yellow]"
            )
            return
        articles = session.query(Article).count()
        sentences = session.query(Sentence).count()

        console.print("[bold blue]Corpus Stats Summary[/bold blue]")
        console.print(f"Total Discovered URLs: {total_urls}")
        console.print(f"Downloaded Articles:   {articles}")
        console.print(f"Accepted Sentences:    {sentences}")

        # URL States
        status_counts = (
            session.query(URL.status, func.count(URL.url_id))
            .group_by(URL.status)
            .order_by(URL.status)
            .all()
        )
        console.print("\n[bold]URL States:[/bold]")
        for status, count in status_counts:
            console.print(f"  - {status}: {count}")

        # Sources breakdown
        source_counts = (
            session.query(URL.source_id, func.count(URL.url_id))
            .group_by(URL.source_id)
            .order_by(URL.source_id)
            .all()
        )
        if source_counts:
            console.print("\n[bold]URLs by Source:[/bold]")
            for src_id, count in source_counts:
                art_count = session.query(Article).filter(Article.source_id == src_id).count()
                sent_count = session.query(Sentence).filter(Sentence.source_id == src_id).count()
                console.print(f"  - {src_id}: {count} URLs, {art_count} articles, {sent_count} sentences")

        # Discovery Methods
        disc_counts = (
            session.query(URL.discovery_method, func.count(URL.url_id))
            .group_by(URL.discovery_method)
            .order_by(URL.discovery_method)
            .all()
        )
        if disc_counts:
            console.print("\n[bold]Discovery Methods:[/bold]")
            for method, count in disc_counts:
                console.print(f"  - {method}: {count}")

        # HTTP Statuses
        http_counts = (
            session.query(URL.last_http_status, func.count(URL.url_id))
            .filter(URL.last_http_status.isnot(None))
            .group_by(URL.last_http_status)
            .order_by(URL.last_http_status)
            .all()
        )
        if http_counts:
            console.print("\n[bold]HTTP Statuses:[/bold]")
            for http_stat, count in http_counts:
                console.print(f"  - {http_stat}: {count}")

        # Date Sources
        date_counts = (
            session.query(Article.date_source, func.count(Article.article_id))
            .filter(Article.date_source.isnot(None))
            .group_by(Article.date_source)
            .order_by(Article.date_source)
            .all()
        )
        if date_counts:
            console.print("\n[bold]Article Date Sources:[/bold]")
            for d_src, count in date_counts:
                console.print(f"  - {d_src}: {count}")

        # Rejection & Error Reasons
        rejection_counts = (
            session.query(URL.error_reason, func.count(URL.url_id))
            .filter(URL.error_reason.isnot(None))
            .group_by(URL.error_reason)
            .order_by(func.count(URL.url_id).desc())
            .all()
        )
        if rejection_counts:
            console.print("\n[bold]Rejection & Error Reasons:[/bold]")
            for reason, count in rejection_counts:
                console.print(f"  - {reason}: {count}")

        # Latest Crawl Run Details
        latest_run = session.query(CrawlRun).order_by(CrawlRun.start_time.desc()).first()
        if latest_run:
            console.print(f"\n[bold]Latest Run:[/bold] {latest_run.mode} ({latest_run.crawl_id})")
            console.print(f"  URLs discovered:     {latest_run.urls_discovered}")
            console.print(f"  URLs fetched:        {latest_run.urls_fetched}")
            console.print(f"  Articles extracted:  {latest_run.articles_extracted}")
            console.print(f"  Sentences accepted:  {latest_run.sentences_accepted}")
            console.print(f"  Sentences rejected:  {latest_run.sentences_rejected}")
            reason_counts = (
                session.query(CrawlEvent.event_type, func.count(CrawlEvent.event_id))
                .filter(CrawlEvent.crawl_id == latest_run.crawl_id)
                .group_by(CrawlEvent.event_type)
                .order_by(CrawlEvent.event_type)
                .all()
            )
            if reason_counts:
                console.print("  Event Breakdown:")
                for reason, reason_count in reason_counts:
                    console.print(f"    - {reason}: {reason_count}")


@cli.command("evaluate-policy")
@click.option("--profile", type=click.Choice(["default", "strict", "balanced", "recall"]), default="default", help="Policy profile to evaluate against stored articles.")
@click.option("--source", help="Filter evaluation to a specific source ID.")
@click.option("--limit", type=int, default=100, help="Maximum number of stored articles to evaluate.")
@click.pass_obj
def evaluate_policy_cmd(obj: dict, profile: str, source: Optional[str], limit: int):
    """Non-mutating evaluation of policy profiles over sampled stored article text."""
    from src.sentence.segmenter import SentenceSegmenter
    from src.sentence.quality_filter import SentenceQualityFilter, normalize_text
    from src.language.detector import FilipinoLanguageDetector
    from src.deduplication.exact import DeduplicationEngine
    from src.storage.models import Article

    config: CrawlerConfig = obj["config"]
    registry: SourceRegistry = obj["registry"]
    db_mgr = DatabaseManager.read_only(
        config.get("storage.database_url", "sqlite:///data/corpus.db")
    )

    with db_mgr.get_session() as session:
        pipeline = CrawlPipeline(session, config, config.get("http.user_agent"))

        # Determine source config if provided
        source_cfg = registry.get_source(source) if source else None

        # Resolve policy with requested profile override
        dummy_cfg = source_cfg
        if profile != "default":
            policy = dict(CrawlPipeline.POLICY_PROFILES.get(profile, {}))
            policy["profile_name"] = profile
        else:
            policy = pipeline.resolve_effective_policy(source_cfg)

        q_filter = SentenceQualityFilter(
            min_tokens=policy["min_tokens"],
            max_tokens=policy["max_tokens"],
            min_quality_score=policy.get("min_quality_score", 0.0),
            noise_patterns=policy.get("noise_patterns"),
            include_headlines=policy.get("include_headlines", False),
            include_quotes=policy.get("include_quotes", False),
        )
        l_detector = FilipinoLanguageDetector(
            min_confidence=policy.get("min_language_confidence", 0.0),
            classify_mixed=bool(config.get("language.classify_mixed", True)),
            accepted_languages=policy.get("accepted_languages", ["FILIPINO"]),
            allow_mixed=policy.get("allow_mixed_language", False),
        )

        query = session.query(Article)
        if source:
            query = query.filter(Article.source_id == source)
        articles = query.order_by(Article.created_at.desc()).limit(limit).all()

        console.print(f"[bold cyan]--- POLICY EVALUATION ({policy.get('profile_name', profile).upper()}) ---[/bold cyan]")
        console.print(f"Sampled Articles: {len(articles)} (source={source or 'ALL'}, limit={limit})")
        console.print(f"Policy settings: min_tokens={policy['min_tokens']}, max_tokens={policy['max_tokens']}, quotes={policy.get('include_quotes')}, headlines={policy.get('include_headlines')}, min_lang_conf={policy.get('min_language_confidence')}, allow_mixed={policy.get('allow_mixed_language')}")

        total_candidates = 0
        projected_accepted = 0
        rejections = {
            "quality": 0,
            "quote": 0,
            "headline": 0,
            "language": 0,
            "duplicate_batch": 0,
        }
        seen_hashes = set()

        for art in articles:
            text = normalize_text(art.article_text or "")
            if not text:
                continue
            sentences = SentenceSegmenter.split_sentences(text)
            if policy.get("include_headlines") and art.headline and art.headline.strip():
                sentences.insert(0, {
                    "sentence_text": art.headline.strip(),
                    "sentence_index": -1,
                    "paragraph_index": -1,
                    "is_quote": False,
                    "is_headline": True,
                })

            for sent in sentences:
                total_candidates += 1
                sent_text = normalize_text(sent["sentence_text"])
                is_headline = sent.get("is_headline", False)
                is_quote = sent.get("is_quote", False)

                quality = q_filter.evaluate(sent_text, is_headline=is_headline, is_quote=is_quote)
                if not quality["clean"]:
                    if quality["reason"] == "headline_excluded":
                        rejections["headline"] += 1
                    elif quality["reason"] == "quote_excluded":
                        rejections["quote"] += 1
                    else:
                        rejections["quality"] += 1
                    continue

                lang_label, lang_conf = l_detector.detect_sentence_language(sent_text)
                if not l_detector.is_accepted(lang_label, lang_conf):
                    rejections["language"] += 1
                    continue

                sent_hash = DeduplicationEngine.compute_sha256(sent_text)
                if sent_hash in seen_hashes:
                    rejections["duplicate_batch"] += 1
                    continue
                seen_hashes.add(sent_hash)
                projected_accepted += 1

        total_rejected = sum(rejections.values())
        console.print(f"\n[bold]Projected Outcomes:[/bold]")
        console.print(f"  Total Candidates:   {total_candidates}")
        console.print(f"  Projected Accepted: {projected_accepted}")
        console.print(f"  Projected Rejected: {total_rejected}")
        console.print(f"  Rejection Breakdown:")
        for r_key, r_cnt in rejections.items():
            console.print(f"    - {r_key}: {r_cnt}")

        # Reconciliation check
        assert total_candidates == (projected_accepted + total_rejected)


if __name__ == "__main__":
    cli()
