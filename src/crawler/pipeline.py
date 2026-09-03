import asyncio
from dataclasses import dataclass
import hashlib
import inspect
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from sqlalchemy import case, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.crawler.archive_discovery import ArchiveDiscoveryEngine
from src.crawler.backoff import BackoffHandler
from src.crawler.cache import HTTPCache
from src.crawler.common_crawl_discovery import CommonCrawlDiscoveryEngine
from src.crawler.config import (
    CrawlerConfig,
    CommonCrawlConfig,
    TrafilaturaFallbackConfig,
    WarcConfig,
)
from src.crawler.discovery import DiscoveryEngine
from src.crawler.discovery_reporting import DiscoveryDiagnosticsStore
from src.crawler.fetcher import FetchResult, HTTPFetcher
from src.crawler.link_discovery import LinkDiscoveryBatch, LinkDiscoveryEngine
from src.crawler.rate_limiter import RateLimiter
from src.crawler.robots import RobotsManager
from src.crawler.url_normalizer import normalize_url
from src.deduplication.exact import DeduplicationEngine
from src.extraction.base import ArticleExtractor
from src.extraction.date_filter import DateEvidence, DateFilter
from src.extraction.trafilatura_fallback import extract_body as extract_trafilatura_body
from src.language.detector import FilipinoLanguageDetector
from src.sentence.quality_filter import SentenceQualityFilter, normalize_text
from src.sentence.segmenter import SentenceSegmenter
from src.sources.registry import SourceConfig, SourceRegistry
from src.storage.models import Article, CrawlEvent, CrawlRun, Sentence, Source, URL, URLDiscoveryEdge, URLStatus
from src.storage.warc_store import WarcStore


@dataclass(frozen=True)
class CrawlJob:
    url_id: int
    url: str
    source_id: str
    domain: str
    retry_count: int
    configured_delay: float
    max_concurrent: int
    publication_date_hint: Optional[datetime] = None
    sitemap_lastmod_hint: Optional[datetime] = None
    date_hint_source: Optional[str] = None
    date_hint_confidence: Optional[float] = None
    treat_lastmod_as_publication: bool = False
    discovery_depth: int = 0
    frontier_priority: int = 0
    link_discovery_enabled: bool = False
    link_candidate_limit: int = 0
    link_discovery_limits: Optional[Dict[str, Any]] = None


@dataclass
class ExtractedData:
    canonical_url: Optional[str]
    headline: str
    author: str
    publication_date_raw: str
    publication_date_source: str
    modified_date_raw: str
    modified_date_source: str
    article_text: str
    body_chars: int
    paragraph_count: int
    selector_match: Optional[str]
    extraction_method: str
    sentences: List[Dict[str, Any]]
    extractor_used: str = "primary"
    primary_outcome: str = "success"
    primary_body_chars: int = 0
    fallback_outcome: str = "disabled"
    fallback_body_chars: int = 0
    fallback_error: Optional[str] = None


@dataclass
class CrawlOutcome:
    job: CrawlJob
    fetch_result: Optional[FetchResult] = None
    extracted: Optional[ExtractedData] = None
    error: Optional[str] = None
    failure_class: Optional[str] = None
    blocked_by_robots: bool = False
    link_discovery_batch: Optional[LinkDiscoveryBatch] = None
    link_discovery_error: Optional[str] = None
    blocked_reason: Optional[str] = None


class CrawlPipeline:
    _DISCOVERY_METHODS = frozenset({
        "RSS",
        "SITEMAP",
        "ARCHIVE",
        "LINK",
        "COMMON_CRAWL",
    })

    def __init__(
        self,
        db_session: Session,
        config: CrawlerConfig,
        user_agent: str,
        source_registry: Optional[SourceRegistry] = None,
    ):
        self.session = db_session
        self.config = config
        self.user_agent = user_agent
        # The CLI injects the registry loaded alongside --config-dir.  Keep
        # the old canonical-file fallback for direct callers that predate the
        # injection point.
        self.source_registry = source_registry
        cutoff_str = self.config.get("crawler.date_cutoff", "2022-01-01")
        self.cutoff_date = datetime.strptime(cutoff_str, "%Y-%m-%d")

        robots_ttl = int(float(self.config.get("cache.robots_ttl_hours", 24)) * 3600)
        self.robots_mgr = RobotsManager(
            user_agent=user_agent,
            cache_ttl_seconds=robots_ttl,
            timeout=float(self.config.get("http.timeout_seconds", 30)),
        )
        self.rate_limiter = RateLimiter(
            default_delay=float(self.config.get("rate_limiting.default_delay_seconds", 5.0)),
            default_concurrency=int(self.config.get("rate_limiting.default_max_concurrent", 1)),
        )
        self.cache = HTTPCache(cache_dir=self.config.get("cache.http_cache_dir", "data/cache"))
        backoff = self.config.get("rate_limiting.backoff", {}) or {}
        self.fetcher = HTTPFetcher(
            user_agent=user_agent,
            cache=self.cache,
            timeout=float(self.config.get("http.timeout_seconds", 30)),
            connect_timeout=float(self.config.get("http.connect_timeout_seconds", 10)),
            read_timeout=float(self.config.get("http.read_timeout_seconds", 30)),
            write_timeout=float(self.config.get("http.write_timeout_seconds", 10)),
            pool_timeout=float(self.config.get("http.pool_timeout_seconds", 10)),
            max_connections=int(self.config.get("http.max_connections", 50)),
            max_keepalive_connections=int(self.config.get("http.max_keepalive_connections", 20)),
            max_retries=int(self.config.get("http.max_retries", 3)),
            backoff_handler=BackoffHandler(
                initial_seconds=float(backoff.get("initial_seconds", 5)),
                multiplier=float(backoff.get("multiplier", 2)),
                max_seconds=float(backoff.get("max_seconds", 300)),
                jitter=bool(backoff.get("jitter", True)),
            ),
        )
        self.quality_filter = SentenceQualityFilter(
            min_tokens=int(self.config.get("sentence.min_tokens", 3)),
            max_tokens=int(self.config.get("sentence.max_tokens", 100)),
        )
        self.lang_detector = FilipinoLanguageDetector(
            min_confidence=float(self.config.get("language.min_confidence", 0.0)),
            classify_mixed=bool(self.config.get("language.classify_mixed", True)),
        )
        self.date_filter = DateFilter(cutoff_date=self.cutoff_date)
        self.include_quotes = bool(self.config.get("sentence.include_quotes", False))
        self.language_threshold = float(self.config.get("language.min_confidence", 0.0))
        self.min_article_chars = int(self.config.get("extraction.min_body_chars", 40))
        self.trafilatura_fallback = TrafilaturaFallbackConfig.from_mapping(
            self.config.get("extraction.fallback.trafilatura", {}) or {}
        )
        self.warc_config = WarcConfig.from_mapping(self.config.get("warc", {}) or {})
        self.warc_store = WarcStore(config=self.warc_config) if self.warc_config.enabled else None
        self.processing_timeout = int(self.config.get("crawler.processing_timeout_seconds", 3600))
        self.max_lifecycle_retries = int(self.config.get("crawler.max_lifecycle_retries", 3))
        self.max_403_retries = int(self.config.get("crawler.max_403_retries", 1))
        self.queue_batch_size = int(self.config.get("crawler.queue_batch_size", 100))
        self.link_discovery_engine = LinkDiscoveryEngine()
        self._link_parent_pages_reserved: Dict[str, int] = {}
        self._link_candidates_consumed: Dict[str, int] = {}

    POLICY_PROFILES = {
        "strict": {
            "min_tokens": 6,
            "max_tokens": 60,
            "include_quotes": False,
            "include_headlines": False,
            "accepted_languages": ["FILIPINO"],
            "min_language_confidence": 0.85,
            "allow_mixed_language": False,
            "min_quality_score": 0.9,
            "noise_patterns": SentenceQualityFilter.DEFAULT_NOISE_PATTERNS,
        },
        "balanced": {
            "min_tokens": 4,
            "max_tokens": 80,
            "include_quotes": True,
            "include_headlines": False,
            "accepted_languages": ["FILIPINO"],
            "min_language_confidence": 0.70,
            "allow_mixed_language": False,
            "min_quality_score": 0.7,
            "noise_patterns": SentenceQualityFilter.DEFAULT_NOISE_PATTERNS,
        },
        "recall": {
            "min_tokens": 3,
            "max_tokens": 120,
            "include_quotes": True,
            "include_headlines": True,
            "accepted_languages": ["FILIPINO", "MIXED"],
            "min_language_confidence": 0.50,
            "allow_mixed_language": True,
            "min_quality_score": 0.5,
            "noise_patterns": SentenceQualityFilter.DEFAULT_NOISE_PATTERNS,
        },
    }

    def resolve_effective_policy(self, source_cfg: Optional[SourceConfig] = None) -> dict:
        """Resolve policy settings: base profile -> global crawler.yaml -> per-source overrides."""
        # 1. Base profile (default / migration retains historical behavior)
        profile_name = "default"
        if source_cfg and source_cfg.policy_profile:
            profile_name = source_cfg.policy_profile
        elif self.config.get("policy.profile"):
            profile_name = self.config.get("policy.profile")
        elif self.config.get("sentence.policy_profile"):
            profile_name = self.config.get("sentence.policy_profile")

        if profile_name in self.POLICY_PROFILES:
            resolved = dict(self.POLICY_PROFILES[profile_name])
            resolved["profile_name"] = profile_name
        else:
            # Migration/default profile preserves existing exact defaults
            resolved = {
                "profile_name": "default",
                "min_tokens": int(self.config.get("sentence.min_tokens", 3)),
                "max_tokens": int(self.config.get("sentence.max_tokens", 100)),
                "include_quotes": bool(self.config.get("sentence.include_quotes", False)),
                "include_headlines": bool(self.config.get("sentence.include_headlines", False)),
                "accepted_languages": self.config.get("language.accepted_languages") or ["FILIPINO"],
                "min_language_confidence": float(self.config.get("language.min_confidence", 0.0)),
                "allow_mixed_language": bool(self.config.get("language.allow_mixed", False)),
                "min_quality_score": float(self.config.get("sentence.min_quality_score", 0.0)),
                "noise_patterns": self.config.get("sentence.noise_patterns") or SentenceQualityFilter.DEFAULT_NOISE_PATTERNS,
            }

        # 2. Apply per-source overrides if provided
        if source_cfg:
            if source_cfg.min_tokens is not None:
                resolved["min_tokens"] = source_cfg.min_tokens
            if source_cfg.max_tokens is not None:
                resolved["max_tokens"] = source_cfg.max_tokens
            if source_cfg.include_quotes is not None:
                resolved["include_quotes"] = source_cfg.include_quotes
            if source_cfg.include_headlines is not None:
                resolved["include_headlines"] = source_cfg.include_headlines
            if source_cfg.accepted_languages is not None:
                resolved["accepted_languages"] = source_cfg.accepted_languages
            if source_cfg.min_language_confidence is not None:
                resolved["min_language_confidence"] = source_cfg.min_language_confidence
            if source_cfg.allow_mixed_language is not None:
                resolved["allow_mixed_language"] = source_cfg.allow_mixed_language
            if source_cfg.min_quality_score is not None:
                resolved["min_quality_score"] = source_cfg.min_quality_score
            if source_cfg.noise_patterns is not None:
                resolved["noise_patterns"] = source_cfg.noise_patterns

        return resolved

    def _source_config(self, source_id: str) -> SourceConfig:
        src = None
        if self.source_registry is not None:
            try:
                src = self.source_registry.get_source(source_id)
            except Exception:
                src = None
        else:
            try:
                cfg_path = Path("config/sources.yaml")
                if cfg_path.exists():
                    registry = SourceRegistry(cfg_path)
                    src = registry.get_source(source_id)
            except Exception:
                src = None

        if not src:
            db_source = self.session.query(Source).filter(Source.source_id == source_id).first()
            return SourceConfig(
                id=source_id,
                name=db_source.name if db_source else source_id,
                domain=db_source.domain if db_source else "unknown",
                enabled=db_source.enabled if db_source else True,
                language=db_source.language if db_source else "filipino",
            )
        return src

    def _link_discovery_settings(self) -> Dict[str, Any]:
        """Return validated-or-defaulted global Stage 4A settings."""
        settings = self.config.get("link_discovery", {}) or {}
        if not isinstance(settings, dict):
            return {}
        return settings

    def _link_discovery_gate(self, source_cfg: SourceConfig, parent_depth: int) -> bool:
        settings = self._link_discovery_settings()
        source_link_cfg = getattr(source_cfg, "link_discovery", None)
        return self._source_link_gate_enabled(source_cfg) and int(parent_depth) < int(
            settings.get("max_depth", 1)
        )

    def _source_link_gate_enabled(self, source_cfg: SourceConfig) -> bool:
        settings = self._link_discovery_settings()
        source_link_cfg = getattr(source_cfg, "link_discovery", None)
        return bool(settings.get("enabled", False)) and bool(
            getattr(source_link_cfg, "enabled", False)
        )

    def _frontier_order_source_ids(self, query) -> set[str]:
        """Return only sources whose two runtime gates opt into frontier order."""
        if not bool(self._link_discovery_settings().get("enabled", False)):
            return set()
        source_ids = [row[0] for row in query.with_entities(URL.source_id).distinct().all()]
        enabled_ids = set()
        for source_id in source_ids:
            try:
                if self._source_link_gate_enabled(self._source_config(source_id)):
                    enabled_ids.add(source_id)
            except Exception:
                # A source with an unavailable configuration keeps the legacy
                # queue order rather than receiving frontier semantics.
                continue
        return enabled_ids

    def _reserve_link_discovery_budget(
        self,
        source_cfg: SourceConfig,
        parent_depth: int,
    ) -> tuple[bool, int, Dict[str, Any]]:
        """Reserve deterministic per-run link quotas before worker dispatch."""
        settings = self._link_discovery_settings()
        if not self._link_discovery_gate(source_cfg, parent_depth):
            return False, 0, {}

        source_id = source_cfg.id
        parents_used = self._link_parent_pages_reserved.get(source_id, 0)
        max_parents = int(settings.get("max_parent_pages_per_source_run", 25))
        if parents_used >= max_parents:
            return False, 0, {}

        candidates_used = self._link_candidates_consumed.get(source_id, 0)
        max_candidates = int(settings.get("max_candidates_per_source_run", 500))
        if candidates_used >= max_candidates:
            return False, 0, {}

        page_limit = int(settings.get("max_links_per_page", 100))
        self._link_parent_pages_reserved[source_id] = parents_used + 1
        limits = {
            "max_depth": int(settings.get("max_depth", 1)),
            "max_links_per_page": page_limit,
            # The per-page limit is applied in the pure analyzer.  The
            # coordinator applies the independent per-source-run candidate
            # quota after outcomes are restored to deterministic job order.
            "max_candidates": page_limit,
            "allow_query_parameters": bool(settings.get("allow_query_parameters", False)),
        }
        return True, page_limit, limits

    def _config_hash(self) -> str:
        cfg = getattr(self.config, "config", getattr(self.config, "data", {}))
        payload = json.dumps(cfg, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _new_run(self, mode: str, source_id: Optional[str] = None) -> CrawlRun:
        policy_name = self.config.get("policy.profile") or self.config.get("sentence.policy_profile") or "default"
        run = CrawlRun(
            crawl_id=f"CRAWL_{uuid.uuid4().hex[:16]}",
            source_id=source_id,
            mode=mode,
            start_time=datetime.utcnow(),
            crawler_version=f"{self.config.get('crawler.version', '1.0.0')}:{policy_name}",
            config_hash=self._config_hash(),
        )
        self.session.add(run)
        self.session.flush()
        return run

    def _event(
        self,
        run: Optional[CrawlRun],
        source_id: Optional[str],
        url: Optional[str],
        event_type: str,
        message: Optional[str] = None,
        status_code: Optional[int] = None,
        error_message: Optional[str] = None,
    ) -> None:
        if run is None:
            return
        err = error_message or message
        self.session.add(CrawlEvent(
            crawl_id=run.crawl_id,
            source_id=source_id,
            url=url,
            event_type=event_type,
            http_status=status_code,
            error_message=err,
            timestamp=datetime.utcnow(),
        ))

    def discover_urls(self, sources: List[SourceConfig]) -> tuple[int, int]:
        """Fetch feeds/sitemaps and register in-scope article URLs."""
        total_discovered = 0
        total_found = 0
        run = self._new_run("discover")
        diagnostics = DiscoveryDiagnosticsStore.from_config(self.session, self.config)
        try:
            discovery = DiscoveryEngine(
                db_session=self.session,
                user_agent=self.user_agent,
                date_cutoff=self.cutoff_date,
                timeout=float(self.config.get("http.timeout_seconds", 30)),
                max_sitemap_depth=int(self.config.get("discovery.max_sitemap_depth", 5)),
                max_sitemap_documents=int(self.config.get("discovery.max_sitemap_documents", 1000)),
                max_sitemap_roots=int(self.config.get("discovery.max_sitemap_roots", 20)),
                max_entries_per_document=int(self.config.get("discovery.max_entries_per_document", 50000)),
                max_candidates=int(self.config.get("discovery.max_candidates", 100000)),
                max_response_bytes=int(self.config.get("discovery.max_response_bytes", 10 * 1024 * 1024)),
                sample_cap=int(self.config.get(
                    "discovery.diagnostics.sample_cap",
                    self.config.get("discovery.diagnostics.sample_urls_per_reason", 10),
                )),
                robots_mgr=self.robots_mgr,
                event_logger=lambda et, msg=None, stat=None: self._event(run, None, None, et, msg, stat),
            )
            for src in sources:
                exists = self.session.query(Source).filter(Source.source_id == src.id).first()
                if not exists:
                    self.session.add(Source(
                        source_id=src.id, name=src.name, domain=src.domain,
                        enabled=src.enabled, language=src.language,
                    ))
                    self.session.flush()
                report = discovery.discover_source(src)
                diagnostics.persist(report, crawl_id=run.crawl_id, source_id=src.id)
                found = report["candidates_found"]
                count = report["candidates_added"]
                total_found += found
                total_discovered += count
                payload = f"seen={found};added={count};duplicates={report['duplicates_skipped']};out_of_scope={report['out_of_scope_skipped']}"
                if report.get("truncated"):
                    payload += ";truncated=true"
                self._event(run, src.id, None, "discovery_source", payload)
            run.urls_discovered = total_discovered
            run.end_time = datetime.utcnow()
            diagnostics.retain_completed_runs()
            self.session.commit()
        except Exception as exc:
            self._event(run, None, None, "error:discovery", error_message=str(exc))
            run.end_time = datetime.utcnow()
            self.session.commit()
            raise
        return total_found, total_discovered

    def discover_archive_urls(
        self,
        sources: List[SourceConfig],
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
        override_granularity: Optional[str] = None,
        max_periods_override: Optional[int] = None,
        max_pages_override: Optional[int] = None,
        allow_disabled_override: bool = False,
    ) -> tuple[int, int]:
        """Traverse configured date archives and register in-scope article URLs."""
        # Archive traversal is an explicit opt-in operation. Filtering before
        # creating a crawl run also guarantees that a disabled/missing archive
        # cannot synthesize URLs or issue requests when called programmatically.
        eligible_sources = [
            src for src in sources
            if src.archive is not None
            and (src.archive.enabled or allow_disabled_override)
        ]
        if allow_disabled_override and (from_date is None or to_date is None):
            eligible_sources = []
        if not eligible_sources:
            return 0, 0

        total_discovered = 0
        total_found = 0
        run = self._new_run("discover_archive")
        try:
            archive_engine = ArchiveDiscoveryEngine(
                db_session=self.session,
                user_agent=self.user_agent,
                date_cutoff=self.cutoff_date,
                timeout=float(self.config.get("http.timeout_seconds", 30)),
                robots_mgr=self.robots_mgr,
                event_logger=lambda et, msg=None, stat=None: self._event(run, None, None, et, msg, stat),
                allow_disabled_override=allow_disabled_override,
            )
            for src in eligible_sources:
                exists = self.session.query(Source).filter(Source.source_id == src.id).first()
                if not exists:
                    self.session.add(Source(
                        source_id=src.id, name=src.name, domain=src.domain,
                        enabled=src.enabled, language=src.language,
                    ))
                    self.session.flush()
                report = archive_engine.discover_source(
                    src,
                    from_date=from_date.date() if isinstance(from_date, datetime) else from_date,
                    to_date=to_date.date() if isinstance(to_date, datetime) else to_date,
                    override_granularity=override_granularity,
                    max_periods_override=max_periods_override,
                    max_pages_override=max_pages_override,
                    allow_disabled_override=allow_disabled_override,
                )
                found = report["links_found"]
                count = report["links_added"]
                total_found += found
                total_discovered += count
                payload = f"planned={report['periods_planned']};pages={report['pages_fetched']};seen={found};added={count};loops={report['loops_detected']}"
                if report.get("truncated"):
                    payload += ";truncated=true"
                self._event(run, src.id, None, "discovery_archive_source", payload)
            run.urls_discovered = total_discovered
            run.end_time = datetime.utcnow()
            self.session.commit()
        except Exception as exc:
            self._event(run, None, None, "error:discovery_archive", error_message=str(exc))
            run.end_time = datetime.utcnow()
            self.session.commit()
            raise
        return total_found, total_discovered

    def discover_common_crawl_urls(self, sources: List[SourceConfig]) -> tuple[int, int]:
        """Seed the queue from the pinned Common Crawl index, when opted in.

        Common Crawl has two independent gates: the global provider setting and
        the per-source setting.  Filtering happens before a crawl run, cache,
        or database write is created so disabled operation is genuinely
        side-effect free.
        """
        settings = CommonCrawlConfig.from_mapping(self.config.get("common_crawl", {}) or {})
        if not settings.enabled:
            return 0, 0

        eligible_sources = []
        for source in sources:
            source_enabled, safe_patterns = CommonCrawlDiscoveryEngine._source_common_crawl(source)
            if source_enabled and safe_patterns:
                eligible_sources.append(source)
        if not eligible_sources:
            return 0, 0

        total_found = 0
        total_discovered = 0
        run = self._new_run("discover_common_crawl")
        diagnostics = DiscoveryDiagnosticsStore.from_config(self.session, self.config)
        try:
            for source in eligible_sources:
                exists = self.session.query(Source).filter(Source.source_id == source.id).first()
                if not exists:
                    self.session.add(Source(
                        source_id=source.id,
                        name=source.name,
                        domain=source.domain,
                        enabled=source.enabled,
                        language=source.language,
                    ))
                    self.session.flush()

                engine = CommonCrawlDiscoveryEngine(
                    db_session=self.session,
                    user_agent=self.user_agent,
                    config=settings,
                    date_cutoff=self.cutoff_date,
                    event_logger=lambda et, msg=None, stat=None: self._event(run, source.id, None, et, msg, stat),
                    persist_queue=True,
                )
                report = engine.discover_source(source)
                diagnostics.persist(report, crawl_id=run.crawl_id, source_id=source.id)
                found = report.candidates_found
                added = report.candidates_added
                total_found += found
                total_discovered += added
                provider_stats = report.get("common_crawl", {})
                payload = {
                    "requests": provider_stats.get("requests_made", 0),
                    "pages": provider_stats.get("pages_fetched", 0),
                    "bytes": provider_stats.get("response_bytes", 0),
                    "candidates": provider_stats.get("raw_candidates_seen", 0),
                    "new_urls": added,
                    "duplicates": report.duplicates_skipped,
                    "scope_rejections": report.out_of_scope_skipped,
                    "cache_hits": provider_stats.get("cache_hits", 0),
                    "truncation_dimensions": report.truncation_dimensions,
                }
                self._event(run, source.id, None, "discovery_common_crawl_source", json.dumps(payload, sort_keys=True))

            run.urls_discovered = total_discovered
            run.end_time = datetime.utcnow()
            diagnostics.retain_completed_runs()
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        return total_found, total_discovered

    def _recover_stale_processing(self, source_id: Optional[str]) -> None:
        """Make records left by an interrupted process eligible again."""
        query = self.session.query(URL).filter(URL.status.in_([URLStatus.PROCESSING, URLStatus.DOWNLOADED]))
        if source_id:
            query = query.filter(URL.source_id == source_id)
        stale_before = datetime.utcnow() - timedelta(seconds=self.processing_timeout)
        recovered_count = 0
        for record in query.all():
            stamp = record.processing_started_at or record.fetched_at or record.discovered_at
            if stamp is not None and stamp > stale_before:
                continue
            record.status = URLStatus.RETRY_WAIT
            record.error_reason = "recovered_interrupted_processing"
            record.failure_class = "stale_processing"
            record.next_retry_at = None
            record.retry_count = (record.retry_count or 0) + 1
            recovered_count += 1
        if recovered_count > 0:
            self.session.commit()

    def _requeue_claimed_jobs(self, jobs: List[CrawlJob]) -> None:
        """Atomically return unpersisted PROCESSING claims to the retry queue."""
        try:
            for job in jobs:
                record = self.session.query(URL).filter(
                    URL.url_id == job.url_id,
                    URL.status == URLStatus.PROCESSING,
                ).first()
                if record is None:
                    continue
                record.status = URLStatus.RETRY_WAIT
                record.retry_count = (record.retry_count or 0) + 1
                record.next_retry_at = None
                record.processing_started_at = None
                record.failure_class = "cancelled"
                record.error_reason = "requeued_after_cancellation"
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise

    async def _worker_process_job(self, job: CrawlJob, source_cfg: SourceConfig) -> CrawlOutcome:
        """Worker execution without database ORM access: HTTP + text parsing only."""
        link_batch: Optional[LinkDiscoveryBatch] = None
        link_error: Optional[str] = None
        try:
            url = job.url
            host = job.domain
            robots_delay = 0.0
            if self.robots_mgr:
                if hasattr(self.robots_mgr, "is_allowed_async"):
                    res_async = self.robots_mgr.is_allowed_async(url, client=getattr(self.fetcher, "client", None))
                    is_allowed = await res_async if inspect.isawaitable(res_async) else res_async
                else:
                    res = self.robots_mgr.is_allowed(url)
                    is_allowed = await res if inspect.isawaitable(res) else res
                if not is_allowed:
                    return CrawlOutcome(job=job, blocked_by_robots=True)

                if hasattr(self.robots_mgr, "get_crawl_delay"):
                    robots_delay = self.robots_mgr.get_crawl_delay(host)
                    if inspect.isawaitable(robots_delay):
                        robots_delay = await robots_delay
                elif hasattr(self.robots_mgr, "get_crawl_delay_async"):
                    try:
                        robots_delay = await self.robots_mgr.get_crawl_delay_async(host, client=getattr(self.fetcher, "client", None))
                    except Exception:
                        robots_delay = None
                else:
                    robots_delay = None

            delay = max(job.configured_delay, robots_delay or 0.0)
            self.rate_limiter.set_domain_limit(host, delay=delay, concurrency=max(1, job.max_concurrent))

            async with self.rate_limiter.request_slot(host, effective_delay=delay):
                fetch_result = await self.fetcher.fetch(url)

            status = int(fetch_result.get("status", 500))
            if status != 200:
                return CrawlOutcome(job=job, fetch_result=fetch_result)

            requested_url = url
            final_url = str(fetch_result.get("final_url") or requested_url)
            try:
                requested_normalized = normalize_url(requested_url)
                final_normalized = normalize_url(final_url)
            except Exception:
                requested_normalized = ""
                final_normalized = ""

            # The HTTP client follows redirects for compatibility with the
            # existing fetcher contract.  Before consuming a redirected
            # response, enforce the same source scope and robots policy on
            # the final target.  Source scope keeps the final host within the
            # source's rate-limited domain, so an accepted redirect reuses
            # the request slot already held for this fetch.
            if final_normalized != requested_normalized:
                try:
                    final_decision = source_cfg.classify_url(final_normalized)
                except Exception:
                    final_decision = None
                if not final_normalized or final_decision is None or not final_decision.accepted:
                    reason = getattr(getattr(final_decision, "reason", None), "value", "invalid_url")
                    return CrawlOutcome(
                        job=job,
                        fetch_result=fetch_result,
                        error=f"redirect_target_out_of_scope:{reason}",
                        failure_class="redirect_out_of_scope",
                    )

                if self.robots_mgr:
                    if hasattr(self.robots_mgr, "is_allowed_async"):
                        final_allowed = self.robots_mgr.is_allowed_async(
                            final_url,
                            client=getattr(self.fetcher, "client", None),
                        )
                        final_allowed = (
                            await final_allowed
                            if inspect.isawaitable(final_allowed)
                            else final_allowed
                        )
                    else:
                        final_allowed = self.robots_mgr.is_allowed(final_url)
                        final_allowed = (
                            await final_allowed
                            if inspect.isawaitable(final_allowed)
                            else final_allowed
                        )
                    if not final_allowed:
                        return CrawlOutcome(
                            job=job,
                            fetch_result=fetch_result,
                            blocked_by_robots=True,
                            blocked_reason="redirect_target_robots_disallowed",
                        )

            html_content = fetch_result.get("html", "")
            headers = fetch_result.get("headers", {}) or {}
            content_type = ""
            if hasattr(headers, "get"):
                content_type = str(
                    headers.get("content-type", headers.get("Content-Type", ""))
                ).lower()
            is_html = not content_type or "html" in content_type or "xhtml" in content_type
            if job.link_discovery_enabled and is_html:
                try:
                    link_batch = self.link_discovery_engine.analyze_html(
                        html_content,
                        final_url=fetch_result.get("final_url") or url,
                        source_config=source_cfg,
                        parent_depth=job.discovery_depth,
                        requested_url=url,
                        explicit_limits=job.link_discovery_limits or {},
                    )
                except Exception as exc:
                    # Link analysis is additive telemetry and must never turn
                    # a successfully fetched parent into an extraction error.
                    link_error = str(exc)
            extracted = ArticleExtractor.extract(html_content, source_cfg)
            primary_text = normalize_text(extracted.get("article_text", ""))
            primary_body_chars = len(primary_text)
            primary_outcome = "success" if primary_text else "empty"
            if primary_text and primary_body_chars < self.trafilatura_fallback.min_body_chars:
                primary_outcome = "short"

            article_text = primary_text
            paragraph_count = extracted.get("paragraph_count", 0)
            extraction_method = extracted.get("extraction_method", "generic")
            extractor_used = "primary"
            fallback_outcome = "disabled"
            fallback_body_chars = 0
            fallback_error = None
            source_extraction = getattr(source_cfg, "extraction", None)
            source_trafilatura = getattr(source_extraction, "trafilatura", None)
            source_fallback_enabled = bool(getattr(source_trafilatura, "enabled", False))
            if self.trafilatura_fallback.enabled and source_fallback_enabled:
                if primary_body_chars < self.trafilatura_fallback.min_body_chars:
                    fallback = extract_trafilatura_body(
                        html_content,
                        min_body_chars=self.trafilatura_fallback.min_body_chars,
                        favor_precision=self.trafilatura_fallback.favor_precision,
                    )
                    fallback_outcome = fallback.outcome
                    fallback_body_chars = fallback.body_chars
                    fallback_error = fallback.error
                    if fallback.outcome == "success":
                        # Deliberately replace the primary body; never merge
                        # the two texts because that would duplicate content.
                        article_text = normalize_text(fallback.article_text)
                        paragraph_count = fallback.paragraph_count
                        extraction_method = "trafilatura"
                        extractor_used = "trafilatura"
                        fallback_body_chars = len(article_text)
                else:
                    fallback_outcome = "not_triggered"
            elif self.trafilatura_fallback.enabled:
                fallback_outcome = "source_disabled"

            sentences = SentenceSegmenter.split_sentences(article_text) if article_text else []

            extracted_data = ExtractedData(
                canonical_url=normalize_url(extracted.get("canonical_url", "")) or None,
                headline=extracted.get("headline", ""),
                author=extracted.get("author", ""),
                publication_date_raw=extracted.get("publication_date_raw", ""),
                publication_date_source=extracted.get("publication_date_source", "missing"),
                modified_date_raw=extracted.get("modified_date_raw", ""),
                modified_date_source=extracted.get("modified_date_source", "missing"),
                article_text=article_text,
                body_chars=len(article_text),
                paragraph_count=paragraph_count,
                selector_match=extracted.get("selector_match"),
                extraction_method=extraction_method,
                sentences=sentences,
                extractor_used=extractor_used,
                primary_outcome=primary_outcome,
                primary_body_chars=primary_body_chars,
                fallback_outcome=fallback_outcome,
                fallback_body_chars=fallback_body_chars,
                fallback_error=fallback_error,
            )
            return CrawlOutcome(
                job=job,
                fetch_result=fetch_result,
                extracted=extracted_data,
                link_discovery_batch=link_batch,
                link_discovery_error=link_error,
            )
        except Exception as exc:
            return CrawlOutcome(
                job=job,
                error=str(exc),
                failure_class="unhandled_exception",
                link_discovery_batch=link_batch,
                link_discovery_error=link_error,
            )

    def _persist_link_batch(
        self,
        batch: LinkDiscoveryBatch,
        parent: URL,
        run: CrawlRun,
    ) -> None:
        """Persist link candidates and provenance in the coordinator only."""
        new_urls = 0
        new_edges = 0
        settings = self._link_discovery_settings()
        source_limit = int(settings.get("max_candidates_per_source_run", 500))
        consumed = self._link_candidates_consumed.get(parent.source_id, 0)
        available = max(0, source_limit - consumed)
        persist_candidates = batch.candidates[:available]
        parent_normalized = normalize_url(parent.url) or parent.url
        truncation_details = list(batch.truncation_details)
        if len(persist_candidates) < len(batch.candidates):
            truncation_details.append({
                "dimension": "candidates_per_source_run",
                "limit": source_limit,
                "observed": consumed + len(batch.candidates),
            })
        try:
            # A savepoint isolates an unexpected link-schema/constraint issue
            # from the already successful parent fetch and extraction state.
            with self.session.begin_nested():
                for candidate in persist_candidates:
                    # The analyzer rejects both requested and final-parent
                    # self links. Keep the coordinator invariant as a second
                    # line of defense for hand-built batches or old workers.
                    if candidate.normalized_url == parent_normalized:
                        continue
                    child = self.session.query(URL).filter(
                        URL.url == candidate.normalized_url
                    ).first()
                    if child is None:
                        child = URL(
                            url=candidate.normalized_url,
                            source_id=parent.source_id,
                            status=URLStatus.DISCOVERED,
                            discovery_method="LINK",
                            discovery_depth=candidate.depth,
                            frontier_priority=candidate.priority,
                            discovered_at=datetime.utcnow(),
                        )
                        self.session.add(child)
                        self.session.flush()
                        new_urls += 1
                    else:
                        # Existing URL ownership/status/method remain intact;
                        # only frontier ranking metadata may improve.
                        if candidate.depth < int(child.discovery_depth or 0):
                            child.discovery_depth = candidate.depth
                        if candidate.priority > int(child.frontier_priority or 0):
                            child.frontier_priority = candidate.priority

                    if child.url_id == parent.url_id:
                        continue

                    edge = self.session.query(URLDiscoveryEdge).filter_by(
                        from_url_id=parent.url_id,
                        to_url_id=child.url_id,
                        discovery_method="LINK",
                    ).first()
                    if edge is None:
                        self.session.add(URLDiscoveryEdge(
                            from_url_id=parent.url_id,
                            to_url_id=child.url_id,
                            crawl_id=run.crawl_id,
                            discovery_method="LINK",
                            depth=candidate.depth,
                            link_text=(candidate.link_text or "")[:500] or None,
                            rel=(candidate.rel or "")[:200] or None,
                            discovered_at=datetime.utcnow(),
                        ))
                        new_edges += 1
        except Exception as exc:
            self._event(
                run,
                parent.source_id,
                parent.url,
                "error:link_discovery_persist",
                error_message=str(exc),
            )
            return

        run.urls_discovered += new_urls
        self._link_candidates_consumed[parent.source_id] = consumed + len(persist_candidates)
        self._event(
            run,
            parent.source_id,
            parent.url,
            "discovery:link_batch",
            json.dumps({
                "anchors_seen": batch.counters.get("anchors_seen", 0),
                "analyzed_candidates": len(batch.candidates),
                "candidates": len(persist_candidates),
                "new_urls": new_urls,
                "new_edges": new_edges,
                "rejection_reasons": batch.rejection_reasons,
                "truncation_details": truncation_details,
            }, sort_keys=True),
        )

    @staticmethod
    def _merge_extraction_diagnostics(url_record: URL, patch: Dict[str, Any]) -> None:
        """Merge bounded diagnostics without discarding other optional metadata."""
        current: Dict[str, Any] = {}
        raw = getattr(url_record, "extraction_diagnostics", None)
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    current = parsed
            except (TypeError, ValueError, json.JSONDecodeError):
                current = {}
        current.update(patch)
        url_record.extraction_diagnostics = json.dumps(current, sort_keys=True)

    @staticmethod
    def _extraction_diagnostics(extracted: ExtractedData) -> Dict[str, Any]:
        """Return the common extraction telemetry written for every outcome."""
        return {
            "selector_match": extracted.selector_match,
            "extraction_method": extracted.extraction_method,
            "extractor_used": extracted.extractor_used,
            "primary_outcome": extracted.primary_outcome,
            "primary_body_chars": extracted.primary_body_chars,
            "fallback_outcome": extracted.fallback_outcome,
            "fallback_body_chars": extracted.fallback_body_chars,
            "fallback_error": extracted.fallback_error,
            "body_chars": extracted.body_chars,
            "paragraph_count": extracted.paragraph_count,
        }

    def _persist_outcome(self, outcome: CrawlOutcome, run: CrawlRun) -> None:
        """Coordinator persistence: deterministic database state update for a completed job."""
        job = outcome.job
        url_record = self.session.query(URL).filter(URL.url_id == job.url_id).first()
        if not url_record:
            return

        now = datetime.utcnow()
        url_record.last_attempt_at = now

        # WARC preservation is diagnostic and independent of corpus
        # persistence.  A writer failure must never change the crawl outcome.
        if self.warc_store is not None and outcome.fetch_result is not None:
            try:
                warc_reference = self.warc_store.capture(
                    job.url,
                    outcome.fetch_result,
                    run_id=run.crawl_id if run is not None else None,
                )
                self._merge_extraction_diagnostics(
                    url_record,
                    {"warc": warc_reference.as_diagnostics()},
                )
            except Exception as exc:
                self._merge_extraction_diagnostics(
                    url_record,
                    {"warc": {"outcome": "error", "reason": str(exc)}},
                )

        if outcome.link_discovery_error:
            self._event(
                run,
                url_record.source_id,
                job.url,
                "error:link_discovery",
                error_message=outcome.link_discovery_error,
            )
        if (
            outcome.link_discovery_batch is not None
            and outcome.fetch_result is not None
            and int(outcome.fetch_result.get("status", 500)) == 200
        ):
            self._persist_link_batch(outcome.link_discovery_batch, url_record, run)

        if outcome.blocked_by_robots:
            url_record.status = URLStatus.BLOCKED
            url_record.failure_class = "robots_disallowed"
            reason = outcome.blocked_reason or "robots_disallowed"
            url_record.error_reason = reason
            self._event(run, url_record.source_id, job.url, "blocked:robots", reason)
            return

        if outcome.error:
            url_record.status = URLStatus.FAILED
            url_record.failure_class = outcome.failure_class or "unhandled_exception"
            url_record.error_reason = outcome.error
            self._event(run, url_record.source_id, job.url, "error:processing", error_message=outcome.error)
            return

        fetch_result = outcome.fetch_result
        if not fetch_result:
            return

        status = int(fetch_result.get("status", 500))
        url_record.last_http_status = status
        category = fetch_result.get("error_category")
        transient_statuses = {202, 408, 425, 429, 500, 502, 503, 504}

        if status != 200:
            if status in transient_statuses or category in ("TIMEOUT", "NETWORK_ERROR", "HTTP_TRANSIENT", "RATE_LIMITED"):
                if (url_record.retry_count or 0) < self.max_lifecycle_retries:
                    url_record.status = URLStatus.RETRY_WAIT
                    url_record.retry_count = (url_record.retry_count or 0) + 1
                    backoff_delay = self.fetcher.backoff_handler.get_delay(url_record.retry_count)
                    retry_after = fetch_result.get("retry_after")
                    delay = max(backoff_delay, retry_after or 0.0)
                    url_record.next_retry_at = datetime.utcnow() + timedelta(seconds=delay)
                    url_record.failure_class = category or "http_transient"
                    url_record.error_reason = fetch_result.get("error") or f"HTTP_{status}"
                    self._event(run, url_record.source_id, job.url, f"retry_wait:http_{status}", url_record.error_reason, status)
                    return
                else:
                    url_record.status = URLStatus.TERMINAL_FAILED
                    url_record.failure_class = "retry_exhausted"
                    url_record.error_reason = f"exhausted_retries_{status}"
                    self._event(run, url_record.source_id, job.url, f"terminal:http_{status}", url_record.error_reason, status)
                    return

            if status == 403:
                source_row = self.session.query(Source).filter(Source.source_id == url_record.source_id).first()
                cooldown_duration = float(self.config.get("rate_limiting.cooldown.duration_seconds", 3600))
                if source_row:
                    source_row.error_count_403 = (source_row.error_count_403 or 0) + 1
                    cooldown_threshold = int(self.config.get("rate_limiting.cooldown.trigger_threshold", 5))
                    if source_row.error_count_403 >= cooldown_threshold:
                        source_row.status = "COOLDOWN"
                        source_row.cooldown_until = datetime.utcnow() + timedelta(seconds=cooldown_duration)
                        self._event(run, url_record.source_id, job.url, "source:cooldown", f"duration={cooldown_duration}")
                else:
                    cooldown_duration = 3600.0

                if (url_record.retry_count or 0) < self.max_403_retries:
                    url_record.status = URLStatus.RETRY_WAIT
                    url_record.retry_count = (url_record.retry_count or 0) + 1
                    retry_after = fetch_result.get("retry_after")
                    delay = retry_after if (retry_after is not None and retry_after > 0) else cooldown_duration
                    url_record.next_retry_at = datetime.utcnow() + timedelta(seconds=delay)
                    url_record.failure_class = "forbidden"
                    url_record.error_reason = "HTTP_403"
                    self._event(run, url_record.source_id, job.url, "retry_wait:http_403", "HTTP_403", 403)
                    return
                else:
                    url_record.status = URLStatus.BLOCKED
                    url_record.failure_class = "forbidden"
                    url_record.error_reason = "HTTP_403"
                    self._event(run, url_record.source_id, job.url, "blocked:http_403", "HTTP_403", 403)
                    return

            url_record.status = URLStatus.TERMINAL_FAILED
            url_record.failure_class = "http_permanent"
            url_record.error_reason = fetch_result.get("error") or f"HTTP_{status}"
            self._event(run, url_record.source_id, job.url, f"terminal:http_{status}", url_record.error_reason, status)
            return

        run.urls_fetched += 1
        url_record.status = URLStatus.DOWNLOADED
        url_record.fetched_at = datetime.utcnow()

        extracted = outcome.extracted
        if not extracted:
            return

        self._merge_extraction_diagnostics(
            url_record,
            self._extraction_diagnostics(extracted),
        )

        url_record.canonical_url = extracted.canonical_url
        raw_date = extracted.publication_date_raw

        # Build DateEvidence candidates in deterministic precedence order
        evidence_list: List[DateEvidence] = []
        if raw_date:
            parsed_html = self.date_filter.parse_date(raw_date)
            if parsed_html:
                src_key = "html_" + extracted.publication_date_source if not extracted.publication_date_source.startswith("html_") else extracted.publication_date_source
                evidence_list.append(DateEvidence(value=parsed_html, source=src_key, confidence=1.0, raw=raw_date))

        if job.date_hint_source == "news_sitemap" and job.publication_date_hint:
            evidence_list.append(DateEvidence(value=job.publication_date_hint, source="news_sitemap", confidence=job.date_hint_confidence or 0.90))

        if job.date_hint_source == "rss" and job.publication_date_hint:
            evidence_list.append(DateEvidence(value=job.publication_date_hint, source="rss", confidence=job.date_hint_confidence or 0.85))

        url_date = self.date_filter.parse_date_from_url(job.url)
        if url_date:
            evidence_list.append(DateEvidence(value=url_date, source="url", confidence=0.75))

        if job.date_hint_source == "archive_period" and job.publication_date_hint:
            evidence_list.append(DateEvidence(value=job.publication_date_hint, source="archive_period", confidence=job.date_hint_confidence or 0.70))

        if job.sitemap_lastmod_hint:
            evidence_list.append(DateEvidence(value=job.sitemap_lastmod_hint, source="lastmod", confidence=0.50))

        if job.publication_date_hint and not any(e.value == job.publication_date_hint for e in evidence_list):
            evidence_list.append(DateEvidence(value=job.publication_date_hint, source="hint", confidence=0.80))

        pub_date, date_reason, chosen_date_source, chosen_date_conf, date_diagnostics = self.date_filter.resolve_publication_evidence(
            evidence_list,
            allow_lastmod_as_publication=job.treat_lastmod_as_publication,
        )
        url_record.date_hint_source = chosen_date_source
        url_record.date_hint_confidence = chosen_date_conf

        # Parse modified date
        mod_date = None
        if extracted.modified_date_raw:
            mod_date = self.date_filter.parse_date(extracted.modified_date_raw)
        if not mod_date and job.sitemap_lastmod_hint:
            mod_date = job.sitemap_lastmod_hint
        if mod_date:
            mod_date = self.date_filter._utc_naive(mod_date)

        if date_reason != "accepted":
            url_record.status = URLStatus.REJECTED
            url_record.failure_class = "date_filter"
            url_record.error_reason = date_reason
            self._merge_extraction_diagnostics(url_record, {
                "date_reason": date_reason,
                "date_source": chosen_date_source,
                "raw_date": raw_date or None,
                "date_diagnostics": date_diagnostics,
            })
            self._event(run, url_record.source_id, job.url, f"rejected:{date_reason}", raw_date or None)
            return

        article_text = extracted.article_text
        if not article_text:
            url_record.status = URLStatus.REJECTED
            url_record.failure_class = "extraction_empty"
            url_record.error_reason = "body_empty"
            self._merge_extraction_diagnostics(url_record, {
                "reason": "body_empty",
                "selector_match": extracted.selector_match,
                "extraction_method": extracted.extraction_method,
                "body_chars": 0,
                "paragraph_count": 0,
            })
            self._event(run, url_record.source_id, job.url, "rejected:body_empty")
            return

        if len(article_text) < self.min_article_chars:
            url_record.status = URLStatus.REJECTED
            url_record.failure_class = "extraction_too_short"
            url_record.error_reason = "body_too_short"
            self._merge_extraction_diagnostics(url_record, {
                "reason": "body_too_short",
                "length": len(article_text),
                "min_chars": self.min_article_chars,
                "selector_match": extracted.selector_match,
                "extraction_method": extracted.extraction_method,
                "paragraph_count": extracted.paragraph_count,
            })
            self._event(run, url_record.source_id, job.url, "rejected:body_too_short")
            return

        run.articles_extracted += 1
        body_hash = DeduplicationEngine.compute_sha256(article_text)
        existing_article = self.session.query(Article).filter(Article.content_hash == body_hash).first()
        if existing_article:
            url_record.status = URLStatus.ACCEPTED
            url_record.content_hash = body_hash
            url_record.sentence_count = existing_article.sentence_count or 0
            self._event(run, url_record.source_id, job.url, "duplicate:article_exact")
            return

        article_id = f"ART_{hashlib.sha256(job.url.encode()).hexdigest()[:16]}"
        db_art = Article(
            article_id=article_id,
            source_id=url_record.source_id,
            url_id=url_record.url_id,
            url=job.url,
            canonical_url=url_record.canonical_url,
            headline=extracted.headline,
            author=extracted.author,
            publication_date=pub_date,
            date_source=chosen_date_source,
            modified_date=mod_date,
            sentence_count=0,
            article_text=article_text,
            content_hash=body_hash,
            is_duplicate=False,
        )
        self.session.add(db_art)
        try:
            self.session.flush()
        except IntegrityError:
            self.session.rollback()
            existing_article = self.session.query(Article).filter(Article.content_hash == body_hash).first()
            url_record = self.session.query(URL).filter(URL.url_id == url_record.url_id).one()
            if existing_article:
                url_record.status = URLStatus.ACCEPTED
                url_record.content_hash = body_hash
                url_record.sentence_count = existing_article.sentence_count or 0
                self._event(run, url_record.source_id, job.url, "duplicate:article_exact")
                return
            url_record.status = URLStatus.FAILED
            url_record.failure_class = "integrity_error"
            url_record.error_reason = "article_insert_integrity_error"
            self._event(run, url_record.source_id, job.url, "error:article_insert", url_record.error_reason)
            return

        source_cfg = self._source_config(job.source_id)
        policy = self.resolve_effective_policy(source_cfg)
        quality_filter = SentenceQualityFilter(
            min_tokens=policy["min_tokens"],
            max_tokens=policy["max_tokens"],
            min_quality_score=policy["min_quality_score"],
            noise_patterns=policy["noise_patterns"],
            include_headlines=policy["include_headlines"],
            include_quotes=policy["include_quotes"],
        )
        lang_detector = FilipinoLanguageDetector(
            min_confidence=policy["min_language_confidence"],
            classify_mixed=bool(self.config.get("language.classify_mixed", True)),
            accepted_languages=policy["accepted_languages"],
            allow_mixed=policy["allow_mixed_language"],
        )

        sentences = list(extracted.sentences)
        if policy["include_headlines"] and extracted.headline and extracted.headline.strip():
            headline_meta = {
                "sentence_text": extracted.headline.strip(),
                "sentence_index": -1,
                "paragraph_index": -1,
                "is_quote": False,
                "is_headline": True,
            }
            sentences.insert(0, headline_meta)

        candidate_rows: list[dict] = []
        candidate_hashes: set[str] = set()
        rejections = {
            "quality": 0,
            "quote": 0,
            "headline": 0,
            "language": 0,
            "duplicate_batch": 0,
            "duplicate_db": 0,
            "total_candidates": len(sentences),
        }
        for sent in sentences:
            sent_text = normalize_text(sent["sentence_text"])
            is_headline = sent.get("is_headline", False)
            is_quote = sent.get("is_quote", False)
            quality = quality_filter.evaluate(sent_text, is_headline=is_headline, is_quote=is_quote)
            if not quality["clean"]:
                if quality["reason"] == "headline_excluded":
                    rejections["headline"] += 1
                    run.sentences_rejected += 1
                    self._event(run, url_record.source_id, job.url, "rejected:headline")
                elif quality["reason"] == "quote_excluded":
                    rejections["quote"] += 1
                    run.sentences_rejected += 1
                    self._event(run, url_record.source_id, job.url, "rejected:quote")
                else:
                    rejections["quality"] += 1
                    run.sentences_rejected += 1
                    self._event(run, url_record.source_id, job.url, f"rejected:quality_{quality['reason']}")
                continue

            lang_label, lang_conf = lang_detector.detect_sentence_language(sent_text)
            if not lang_detector.is_accepted(lang_label, lang_conf):
                rejections["language"] += 1
                run.sentences_rejected += 1
                self._event(run, url_record.source_id, job.url, f"rejected:language_{lang_label.lower()}")
                continue
            sent_hash = DeduplicationEngine.compute_sha256(sent_text)
            if sent_hash in candidate_hashes:
                rejections["duplicate_batch"] += 1
                run.sentences_rejected += 1
                self._event(run, url_record.source_id, job.url, "duplicate:sentence_batch")
                continue
            candidate_hashes.add(sent_hash)
            candidate_rows.append({
                "sent": sent,
                "text": sent_text,
                "hash": sent_hash,
                "lang": lang_label,
                "conf": lang_conf,
                "score": quality.get("score", 1.0),
            })

        existing_hashes: set[str] = set()
        if candidate_hashes:
            existing_hashes = {
                row[0] for row in self.session.query(Sentence.content_hash)
                .filter(Sentence.content_hash.in_(candidate_hashes)).all()
            }
        inserted = []
        for candidate in candidate_rows:
            if candidate["hash"] in existing_hashes:
                rejections["duplicate_db"] += 1
                run.sentences_rejected += 1
                self._event(run, url_record.source_id, job.url, "duplicate:sentence_exact")
                continue
            sent = candidate["sent"]
            inserted.append(Sentence(
                sentence_id=f"SENT_{candidate['hash'][:16]}",
                article_id=article_id,
                source_id=url_record.source_id,
                sentence_index=sent["sentence_index"],
                paragraph_index=sent["paragraph_index"],
                sentence_text=candidate["text"],
                normalized_text=candidate["text"].lower(),
                language=candidate["lang"],
                language_confidence=candidate["conf"],
                token_count=len(candidate["text"].split()),
                quality_score=candidate["score"],
                is_quote=sent["is_quote"],
                content_hash=candidate["hash"],
                is_duplicate=False,
            ))
        self.session.add_all(inserted)
        db_art.sentence_count = len(inserted)
        url_record.sentence_count = len(inserted)
        url_record.content_hash = body_hash
        run.sentences_accepted += len(inserted)

        diagnostics = {
            "reason": "no_sentences" if len(inserted) == 0 else "accepted",
            "breakdown": rejections,
            "selector_match": extracted.selector_match,
            "extraction_method": extracted.extraction_method,
            "body_chars": len(article_text),
            "paragraph_count": extracted.paragraph_count,
            "segmented_count": len(sentences),
            "rejections": rejections,
            "accepted_count": len(inserted),
            "date_source": chosen_date_source,
            "date_confidence": chosen_date_conf,
        }
        self._merge_extraction_diagnostics(url_record, diagnostics)

        if len(inserted) == 0:
            url_record.status = URLStatus.NO_SENTENCES
            url_record.error_reason = "zero_sentences_accepted"
            self._event(run, url_record.source_id, job.url, "rejected:no_sentences", json.dumps(rejections))
        else:
            url_record.status = URLStatus.ACCEPTED
            self._event(run, url_record.source_id, job.url, "success:accepted", f"sentences={len(inserted)}")

    async def _process_url(self, url_record: URL, source_cfg: SourceConfig, run: CrawlRun) -> None:
        """Process a single URL record directly (used in single-URL processing and tests)."""
        url = normalize_url(url_record.url) or url_record.url
        host = (urlparse(url).netloc or source_cfg.domain).lower()
        link_enabled, link_limit, link_limits = self._reserve_link_discovery_budget(
            source_cfg,
            int(getattr(url_record, "discovery_depth", 0) or 0),
        )
        job = CrawlJob(
            url_id=url_record.url_id,
            url=url,
            source_id=url_record.source_id,
            domain=host,
            retry_count=url_record.retry_count or 0,
            configured_delay=float(source_cfg.crawl_delay_seconds or self.config.get("rate_limiting.default_delay_seconds", 5)),
            max_concurrent=max(1, source_cfg.max_concurrent),
            publication_date_hint=url_record.publication_date_hint,
            sitemap_lastmod_hint=getattr(url_record, "sitemap_lastmod_hint", None),
            date_hint_source=getattr(url_record, "date_hint_source", None),
            date_hint_confidence=getattr(url_record, "date_hint_confidence", None),
            treat_lastmod_as_publication=getattr(source_cfg, "treat_lastmod_as_publication", False),
            discovery_depth=int(getattr(url_record, "discovery_depth", 0) or 0),
            frontier_priority=int(getattr(url_record, "frontier_priority", 0) or 0),
            link_discovery_enabled=link_enabled,
            link_candidate_limit=link_limit,
            link_discovery_limits=link_limits or None,
        )
        url_record.status = URLStatus.PROCESSING
        url_record.processing_started_at = datetime.utcnow()
        url_record.error_reason = None
        self.session.commit()

        outcome = await self._worker_process_job(job, source_cfg)
        self._persist_outcome(outcome, run)

    @classmethod
    def _discovery_method_values(cls, discovery_method: Optional[str]) -> Optional[tuple[str, ...]]:
        """Translate a CLI/API provenance selector into stored method values."""
        if discovery_method is None:
            return None
        if not isinstance(discovery_method, str):
            raise ValueError("discovery_method must be a string")
        normalized = discovery_method.strip().upper().replace("-", "_")
        if normalized in {"", "ALL"}:
            return None
        if normalized == "DEFAULT":
            return ("RSS", "SITEMAP")
        if normalized not in cls._DISCOVERY_METHODS:
            allowed = ", ".join(sorted(cls._DISCOVERY_METHODS | {"ALL", "DEFAULT"}))
            raise ValueError(f"unsupported discovery_method {discovery_method!r}; expected one of {allowed}")
        return (normalized,)

    async def crawl_queued_urls(
        self,
        source_id: Optional[str] = None,
        progress_callback: Optional[callable] = None,
        limit: Optional[int] = None,
        discovery_method: Optional[str] = None,
    ) -> None:
        """Process queued URLs in bounded batches with asynchronous concurrent worker execution."""
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
        ):
            raise ValueError("limit must be a positive integer")
        method_values = self._discovery_method_values(discovery_method)
        self._recover_stale_processing(source_id)
        run = self._new_run("crawl", source_id)
        self._link_parent_pages_reserved = {}
        self._link_candidates_consumed = {}
        processed_count = 0
        start = getattr(self.fetcher, "start", None)
        if start:
            started = start()
            if inspect.isawaitable(started):
                await started
        try:
            while True:
                now = datetime.utcnow()
                # Restore expired cooldown sources
                self.session.query(Source).filter(
                    Source.status == "COOLDOWN",
                    Source.cooldown_until != None,
                    Source.cooldown_until <= now
                ).update({Source.status: "ACTIVE", Source.cooldown_until: None}, synchronize_session=False)

                # Get sources currently in active cooldown or disabled
                blocked_sources = {
                    row[0] for row in self.session.query(Source.source_id).filter(
                        or_(
                            Source.status == "DISABLED",
                            (Source.status == "COOLDOWN") & (Source.cooldown_until > now)
                        )
                    ).all()
                }

                query = self.session.query(URL).filter(
                    or_(
                        URL.status == URLStatus.DISCOVERED,
                        (URL.status == URLStatus.RETRY_WAIT) & ((URL.next_retry_at == None) | (URL.next_retry_at <= now))
                    )
                )
                if blocked_sources:
                    query = query.filter(~URL.source_id.in_(blocked_sources))
                if source_id:
                    query = query.filter(URL.source_id == source_id)
                if method_values:
                    query = query.filter(URL.discovery_method.in_(method_values))
                frontier_source_ids = self._frontier_order_source_ids(query)
                if frontier_source_ids:
                    frontier_sources = URL.source_id.in_(frontier_source_ids)
                    query = query.order_by(
                        case((frontier_sources, 0), else_=1).asc(),
                        case((frontier_sources, URL.discovery_depth), else_=0).asc(),
                        case((frontier_sources, URL.frontier_priority), else_=0).desc(),
                        case((frontier_sources, URL.url), else_="").asc(),
                        URL.url_id.asc(),
                    )
                else:
                    query = query.order_by(URL.url_id)
                remaining = None if limit is None else limit - processed_count
                if remaining is not None and remaining <= 0:
                    break
                batch_limit = self.queue_batch_size if remaining is None else min(
                    self.queue_batch_size, remaining
                )
                query = query.limit(batch_limit)
                queued = query.all()
                if not queued:
                    break

                run.urls_discovered += len(queued)
                processed_count += len(queued)
                jobs: List[CrawlJob] = []
                for url_record in queued:
                    source_cfg = self._source_config(url_record.source_id)
                    norm_url = normalize_url(url_record.url) or url_record.url
                    host = (urlparse(norm_url).netloc or source_cfg.domain).lower()
                    discovery_depth = int(getattr(url_record, "discovery_depth", 0) or 0)
                    frontier_priority = int(getattr(url_record, "frontier_priority", 0) or 0)
                    link_enabled, link_limit, link_limits = self._reserve_link_discovery_budget(
                        source_cfg,
                        discovery_depth,
                    )
                    url_record.status = URLStatus.PROCESSING
                    url_record.processing_started_at = datetime.utcnow()
                    url_record.error_reason = None
                    jobs.append(CrawlJob(
                        url_id=url_record.url_id,
                        url=norm_url,
                        source_id=url_record.source_id,
                        domain=host,
                        retry_count=url_record.retry_count or 0,
                        configured_delay=float(source_cfg.crawl_delay_seconds or self.config.get("rate_limiting.default_delay_seconds", 5)),
                        max_concurrent=max(1, source_cfg.max_concurrent),
                        publication_date_hint=url_record.publication_date_hint,
                        sitemap_lastmod_hint=getattr(url_record, "sitemap_lastmod_hint", None),
                        date_hint_source=getattr(url_record, "date_hint_source", None),
                        date_hint_confidence=getattr(url_record, "date_hint_confidence", None),
                        treat_lastmod_as_publication=getattr(source_cfg, "treat_lastmod_as_publication", False),
                        discovery_depth=discovery_depth,
                        frontier_priority=frontier_priority,
                        link_discovery_enabled=link_enabled,
                        link_candidate_limit=link_limit,
                        link_discovery_limits=link_limits or None,
                    ))
                self.session.commit()

                # Dispatch bounded worker tasks asynchronously
                tasks = [
                    asyncio.create_task(self._worker_process_job(job, self._source_config(job.source_id)))
                    for job in jobs
                ]

                # Workers remain concurrent for HTTP/extraction, while
                # coordinator persistence follows the deterministic claim
                # order.  This also makes the per-source candidate quota
                # reproducible across reruns.
                try:
                    outcomes = await asyncio.gather(*tasks)
                except asyncio.CancelledError:
                    # Cancellation must not strand the rows claimed above.
                    # Cancel and drain every worker before returning the
                    # cancellation to the caller, then requeue all claims in
                    # one database transaction.
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    try:
                        self._requeue_claimed_jobs(jobs)
                    except Exception:
                        # Never replace the caller's cancellation with a
                        # cleanup/database exception.  The next run's stale
                        # recovery remains a second safety net.
                        self.session.rollback()
                    raise
                for outcome in outcomes:
                    try:
                        self._persist_outcome(outcome, run)
                        self.session.commit()
                    except Exception as exc:
                        self.session.rollback()
                        fresh = self.session.query(URL).filter(URL.url_id == outcome.job.url_id).first()
                        if fresh:
                            fresh.status = URLStatus.FAILED
                            fresh.failure_class = "unhandled_exception"
                            fresh.error_reason = str(exc)
                            self._event(run, fresh.source_id, fresh.url, "error:processing", error_message=str(exc))
                            self.session.commit()
                    finally:
                        if progress_callback:
                            progress_callback(outcome.job.url)

            run.end_time = datetime.utcnow()
            self.session.commit()
        finally:
            if self.warc_store is not None:
                try:
                    self.warc_store.finalize(run.crawl_id)
                except Exception:
                    # WARC is an optional sidecar; finalization cannot turn
                    # an otherwise completed crawl into a failed run.
                    pass
            close = getattr(self.fetcher, "close", None)
            if close:
                closed = close()
                if inspect.isawaitable(closed):
                    await closed
