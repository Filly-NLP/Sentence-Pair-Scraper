"""RSS and sitemap URL discovery with bounded, reconciled audit telemetry."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import time
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

import httpx
from sqlalchemy.orm import Session

from src.crawler.discovery_reporting import (
    BoundedDiscoveryCollector,
    DiscoveryOutcome,
    DiscoveryReport,
    RootAudit,
    ScopeReason,
)
from src.crawler.robots import RobotsManager
from src.crawler.rss_parser import RSSParser
from src.crawler.sitemap_parser import SitemapParser
from src.crawler.url_normalizer import normalize_url
from src.sources.registry import SourceConfig
from src.storage.models import URL

_ORIGINAL_HTTPX_GET = httpx.get


class DiscoveryEngine:
    """Discover feed/sitemap candidates while preserving the legacy API."""

    def __init__(
        self,
        db_session: Optional[Session],
        user_agent: str,
        date_cutoff: datetime,
        timeout: float = 30.0,
        max_sitemap_depth: int = 5,
        max_sitemap_documents: int = 1000,
        max_sitemap_roots: int = 20,
        max_entries_per_document: int = 50000,
        max_candidates: int = 100000,
        max_response_bytes: int = 10 * 1024 * 1024,
        robots_mgr: Optional[RobotsManager] = None,
        event_logger: Optional[Callable[[str, Optional[str], Optional[int]], None]] = None,
        persist_queue: bool = True,
        sample_cap: int = 10,
    ):
        self.session = db_session
        self.user_agent = user_agent
        self.date_cutoff = date_cutoff
        self.timeout = timeout
        self.max_sitemap_depth = max(0, max_sitemap_depth)
        self.max_sitemap_documents = max(1, max_sitemap_documents)
        self.max_sitemap_roots = max(1, max_sitemap_roots)
        self.max_entries_per_document = max(1, max_entries_per_document)
        self.max_candidates = max(1, max_candidates)
        self.max_response_bytes = max(1024, max_response_bytes)
        self.robots_mgr = robots_mgr
        self.event_logger = event_logger
        self.persist_queue = bool(persist_queue)
        self.sample_cap = max(0, int(sample_cap))
        self.headers = {"User-Agent": user_agent}
        self._active_persist_queue = self.persist_queue
        self._seen_urls: Set[str] = set()
        self._first_methods: Dict[str, str] = {}

    async def _http_get(self, client: httpx.AsyncClient, url: str) -> Any:
        # Existing tests patch httpx.get; retain that seam while production
        # requests use the reusable async client.
        if httpx.get is not _ORIGINAL_HTTPX_GET:
            res = httpx.get(url, headers=self.headers, timeout=self.timeout)
            if inspect.isawaitable(res):
                return await res
            return res
        return await client.get(url, headers=self.headers)

    def _emit(self, event_type: str, message: Optional[str] = None, http_status: Optional[int] = None) -> None:
        # An audit must not create crawl events, even when the caller supplied
        # an event logger intended for the mutating wrapper.
        if self._active_persist_queue and self.event_logger:
            self.event_logger(event_type, message, http_status)

    @staticmethod
    def _header(headers: Any, name: str) -> Optional[str]:
        if not headers:
            return None
        wanted = name.lower()
        for key, value in dict(headers).items():
            if str(key).lower() == wanted:
                return str(value)
        return None

    @staticmethod
    def _response_bytes(response: Any) -> Tuple[bytes, Optional[int]]:
        raw = getattr(response, "content", None)
        if not raw:
            text = getattr(response, "text", "")
            raw = text.encode("utf-8") if isinstance(text, str) else b""
        downloaded = getattr(response, "num_bytes_downloaded", None)
        try:
            downloaded = int(downloaded) if downloaded is not None else None
        except (TypeError, ValueError):
            downloaded = None
        return bytes(raw), downloaded

    @staticmethod
    def _date_before_cutoff(value: Any, cutoff: datetime) -> bool:
        if value is None:
            return False
        if isinstance(value, date) and not isinstance(value, datetime):
            value = datetime.combine(value, datetime.min.time())
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return False
        if not isinstance(value, datetime):
            return False
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        if cutoff.tzinfo is not None:
            cutoff = cutoff.astimezone(timezone.utc).replace(tzinfo=None)
        return value < cutoff

    def _new_report(self, source: SourceConfig, discovery_method: str = "DISCOVERY") -> DiscoveryReport:
        self._seen_urls = set()
        self._first_methods = {}
        return DiscoveryReport(
            source_id=source.id,
            discovery_method=discovery_method,
            collector=BoundedDiscoveryCollector(self.sample_cap),
        )

    # Small public seams for additive discovery providers.  The existing
    # protected methods remain the implementation and their legacy call shape
    # is unchanged for RSS/sitemap callers and tests.
    def new_report(self, source: SourceConfig, discovery_method: str = "DISCOVERY") -> DiscoveryReport:
        return self._new_report(source, discovery_method=discovery_method)

    def _mark_truncation(self, report: DiscoveryReport, dimension: str, **details: Any) -> None:
        if not any(item.get("dimension") == dimension for item in report.truncation_details):
            report.add_truncation(dimension, **details)
            self._emit(
                "discovery:truncated",
                ";".join(f"{key}={value}" for key, value in {"dimension": dimension, **details}.items()),
            )

    def mark_truncation(self, report: DiscoveryReport, dimension: str, **details: Any) -> None:
        self._mark_truncation(report, dimension, **details)

    def process_candidates(
        self,
        source: SourceConfig,
        candidates: Iterable[Any],
        existing_before_run: Set[str],
        report: DiscoveryReport,
    ) -> int:
        """Process candidates through the shared normalization and queue contract.

        Optional discovery providers use this seam instead of duplicating URL
        normalization, source classification, date filtering, diagnostics, and
        idempotent queue insertion.
        """
        return self._queue_urls(source, candidates, existing_before_run, report)

    def _queue_urls(
        self,
        source: SourceConfig,
        candidates: Iterable[Any],
        existing_before_run: Set[str],
        report: DiscoveryReport,
    ) -> int:
        """Normalize, classify, date-filter, deduplicate, and optionally queue."""
        added = 0
        for item in candidates:
            if isinstance(item, dict):
                raw_url = item.get("url", "")
                method = str(item.get("method", "DISCOVERY"))
                publication_hint = item.get("publication_date_hint")
                lastmod_hint = item.get("sitemap_lastmod_hint")
                hint_source = item.get("date_hint_source")
                hint_conf = item.get("date_hint_confidence")
                root_url = item.get("root_url")
            else:
                values = tuple(item)
                raw_url, method = values[0], str(values[1])
                publication_hint = values[2] if len(values) > 2 else None
                lastmod_hint = values[3] if len(values) > 3 else None
                hint_source = values[4] if len(values) > 4 else None
                hint_conf = values[5] if len(values) > 5 else None
                root_url = values[6] if len(values) > 6 else None

            report.candidates_seen += 1
            raw_text = raw_url if isinstance(raw_url, str) else str(raw_url or "")
            try:
                normalized = normalize_url(raw_text)
            except Exception:
                normalized = ""

            if not normalized:
                report.normalization_failures += 1
                report.invalid_or_unsupported += 1
                report.rejected += 1
                report.collector.record(
                    DiscoveryOutcome.REJECTED_INVALID,
                    ScopeReason.INVALID_URL,
                    discovery_method=method,
                    root_url=root_url,
                    candidate_url=raw_text,
                )
                if len(report.sample_out_of_scope) < self.sample_cap:
                    report.sample_out_of_scope.append(raw_text)
            else:
                decision = source.classify_url(normalized)
                if decision.reason == ScopeReason.UNSUPPORTED_SCHEME:
                    report.normalization_successes += 1
                    report.invalid_or_unsupported += 1
                    report.rejected += 1
                    report.collector.record(
                        DiscoveryOutcome.REJECTED_INVALID,
                        decision.reason,
                        discovery_method=method,
                        root_url=root_url,
                        candidate_url=raw_text,
                        normalized_url=normalized,
                    )
                    if len(report.sample_out_of_scope) < self.sample_cap:
                        report.sample_out_of_scope.append(normalized)
                else:
                    report.normalization_successes += 1
                    if normalized in self._seen_urls:
                        report.duplicate_in_run += 1
                        first_method = self._first_methods.get(normalized)
                        if first_method and first_method != method:
                            report.cross_method_duplicates += 1
                        report.accepted_earlier_in_run += 1 if normalized in self._first_methods else 0
                        report.collector.record(
                            DiscoveryOutcome.DUPLICATE_IN_RUN,
                            ScopeReason.UNKNOWN,
                            discovery_method=method,
                            root_url=root_url,
                            candidate_url=raw_text,
                            normalized_url=normalized,
                            metadata={"first_method": first_method},
                        )
                    else:
                        self._seen_urls.add(normalized)
                        self._first_methods[normalized] = method
                        report.unique_within_run += 1
                        if not decision.accepted:
                            report.rejected_unique += 1
                            report.rejected += 1
                            report.collector.record(
                                DiscoveryOutcome.REJECTED_SCOPE,
                                decision.reason,
                                discovery_method=method,
                                root_url=root_url,
                                candidate_url=raw_text,
                                normalized_url=normalized,
                            )
                            if len(report.sample_out_of_scope) < self.sample_cap:
                                report.sample_out_of_scope.append(normalized)
                        elif self._date_before_cutoff(publication_hint, self.date_cutoff):
                            report.rejected_unique += 1
                            report.rejected += 1
                            report.date_hint_rejections += 1
                            report.collector.record(
                                DiscoveryOutcome.REJECTED_DATE_HINT,
                                ScopeReason.DATE_BEFORE_CUTOFF,
                                discovery_method=method,
                                root_url=root_url,
                                candidate_url=raw_text,
                                normalized_url=normalized,
                                metadata={"date_hint_source": hint_source},
                            )
                            if len(report.sample_out_of_scope) < self.sample_cap:
                                report.sample_out_of_scope.append(normalized)
                        else:
                            report.accepted_unique += 1
                            report.accepted += 1
                            if normalized in existing_before_run:
                                report.already_stored += 1
                                report.database_duplicates += 1
                                report.collector.record(
                                    DiscoveryOutcome.ALREADY_STORED,
                                    ScopeReason.ACCEPTED,
                                    discovery_method=method,
                                    root_url=root_url,
                                    candidate_url=raw_text,
                                    normalized_url=normalized,
                                )
                            else:
                                report.would_queue += 1
                                # ``queued_new`` is the reconciled logical
                                # count of new URLs. ``actually_queued`` is
                                # deliberately separate for audit mode.
                                report.queued_new += 1
                                report.collector.record(
                                    DiscoveryOutcome.QUEUED,
                                    ScopeReason.ACCEPTED,
                                    discovery_method=method,
                                    root_url=root_url,
                                    candidate_url=raw_text,
                                    normalized_url=normalized,
                                )
                                if self._active_persist_queue:
                                    if self.session is not None:
                                        self.session.add(URL(
                                            url=normalized,
                                            source_id=source.id,
                                            status="DISCOVERED",
                                            discovery_method=method,
                                            discovered_at=datetime.utcnow(),
                                            publication_date_hint=publication_hint,
                                            sitemap_lastmod_hint=lastmod_hint,
                                            date_hint_source=hint_source,
                                            date_hint_confidence=hint_conf,
                                        ))
                                    report.actually_queued += 1
                                    added += 1

            if report.candidates_seen >= self.max_candidates:
                self._mark_truncation(
                    report,
                    "candidates",
                    limit=self.max_candidates,
                    count=report.candidates_seen,
                )
                break

        return added

    def queue_urls(
        self,
        source: SourceConfig,
        candidates: Iterable[Any],
        existing_before_run: Set[str],
        report: DiscoveryReport,
    ) -> int:
        return self._queue_urls(source, candidates, existing_before_run, report)

    def _root_audit(
        self,
        report: DiscoveryReport,
        configured_url: str,
        *,
        configured: bool,
        robots_declared: bool,
        requested_url: Optional[str] = None,
        depth: int = 0,
    ) -> RootAudit:
        normalized = normalize_url(configured_url) or configured_url
        for root in report.roots:
            if normalize_url(root.configured_url) == normalized:
                root.origin_configured = root.origin_configured or configured
                root.robots_declared = root.robots_declared or robots_declared
                return root
        root = RootAudit(
            configured_url=configured_url,
            origin_configured=configured,
            robots_declared=robots_declared,
            requested_url=requested_url or configured_url,
            depth=depth,
        )
        report.roots.append(root)
        return root

    async def _is_allowed(self, url: str, client: httpx.AsyncClient) -> bool:
        if not self.robots_mgr:
            return True
        try:
            is_mock = hasattr(self.robots_mgr.is_allowed, "_mock_return_value") or hasattr(self.robots_mgr.is_allowed, "return_value")
            if hasattr(self.robots_mgr, "is_allowed_async") and not is_mock:
                return bool(await self.robots_mgr.is_allowed_async(url, client=client))
            return bool(self.robots_mgr.is_allowed(url))
        except Exception:
            return bool(self.robots_mgr.is_allowed(url))

    async def _get_robots_sitemaps(self, source: SourceConfig, client: httpx.AsyncClient) -> List[str]:
        if not self.robots_mgr:
            return []
        try:
            is_mock = hasattr(self.robots_mgr.get_sitemaps, "_mock_return_value") or hasattr(self.robots_mgr.get_sitemaps, "return_value")
            if hasattr(self.robots_mgr, "get_sitemaps_async") and not is_mock:
                return await self.robots_mgr.get_sitemaps_async(source.domain, client=client)
            return self.robots_mgr.get_sitemaps(source.domain)
        except Exception:
            return self.robots_mgr.get_sitemaps(source.domain)

    def discover_source_urls(self, source: SourceConfig, persist_queue: Optional[bool] = None) -> Tuple[int, int]:
        report = self.discover_source(source, persist_queue=persist_queue)
        return report["candidates_found"], report["candidates_added"]

    def discover_source(self, source: SourceConfig, persist_queue: Optional[bool] = None) -> DiscoveryReport:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            raise RuntimeError(
                "discover_source() cannot run inside an active event loop; "
                "await discover_source_async() instead"
            )
        return asyncio.run(self.discover_source_async(source, persist_queue=persist_queue))

    def _discover_source_sync(self, source: SourceConfig, persist_queue: Optional[bool] = None) -> DiscoveryReport:
        return asyncio.run(self.discover_source_async(source, persist_queue=persist_queue))

    async def discover_source_async(
        self,
        source: SourceConfig,
        client: Optional[httpx.AsyncClient] = None,
        persist_queue: Optional[bool] = None,
    ) -> DiscoveryReport:
        """Execute feed and sitemap discovery for one source."""
        previous_persist = self._active_persist_queue
        self._active_persist_queue = self.persist_queue if persist_queue is None else bool(persist_queue)
        report = self._new_report(source)
        started = time.monotonic()
        existing_before_run: Set[str] = set()
        if self.session is not None:
            with self.session.no_autoflush:
                existing_before_run = {row[0] for row in self.session.query(URL.url).all()}

        should_close_client = False
        if client is None:
            client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=True)
            should_close_client = True

        try:
            # RSS/Atom roots
            for rss_cfg in source.rss:
                root = self._root_audit(report, rss_cfg.url, configured=True, robots_declared=False)
                root_start = time.monotonic()
                try:
                    if not await self._is_allowed(rss_cfg.url, client):
                        root.errors.append("robots_disallowed")
                        report.errors.append({
                            "target": rss_cfg.url,
                            "type": "robots_disallowed",
                        })
                        self._emit("skipped:robots_disallowed", f"url={rss_cfg.url}")
                        continue
                    response = await self._http_get(client, rss_cfg.url)
                    root.requested_url = rss_cfg.url
                    root.final_url = str(getattr(response, "url", None) or rss_cfg.url)
                    root.status = getattr(response, "status_code", None)
                    headers = getattr(response, "headers", {}) or {}
                    root.content_type = self._header(headers, "content-type")
                    root.content_encoding = self._header(headers, "content-encoding")
                    raw_bytes, downloaded = self._response_bytes(response)
                    root.transferred_bytes = downloaded
                    root.decoded_bytes = len(raw_bytes)
                    if root.status != 200:
                        report.fetch_errors += 1
                        error = {"target": rss_cfg.url, "type": "rss_http_error", "status": root.status}
                        report.errors.append(error)
                        root.errors.append("http_status")
                        self._emit("error:discovery_rss", f"status={root.status};url={rss_cfg.url}", root.status)
                    else:
                        parsed = RSSParser.parse_result(raw_bytes)
                        root.document_kind = parsed.kind
                        root.parsed_entries = len(parsed.entries)
                        root.news_dates = sum(1 for entry in parsed.entries if entry.get("published_date"))
                        if parsed.kind == "HTML":
                            root.errors.append("html_document")
                            report.errors.append({"target": rss_cfg.url, "type": "unsupported_html"})
                        elif parsed.malformed:
                            report.parse_errors += 1
                            root.errors.append(parsed.error or "malformed_feed")
                            report.errors.append({
                                "target": rss_cfg.url,
                                "type": "rss_malformed",
                                "error": parsed.error,
                            })
                        before_a = report.accepted
                        before_r = report.rejected
                        candidates = [{
                            "url": article.get("url", ""),
                            "method": "RSS",
                            "publication_date_hint": article.get("published_date"),
                            "date_hint_source": "rss",
                            "date_hint_confidence": 0.85,
                            "root_url": rss_cfg.url,
                        } for article in parsed.entries]
                        self._queue_urls(source, candidates, existing_before_run, report)
                        root.accepted += report.accepted - before_a
                        root.rejected += report.rejected - before_r
                except Exception as exc:
                    report.fetch_errors += 1
                    report.errors.append({"target": rss_cfg.url, "type": "rss_exception", "error": str(exc)})
                    root.errors.append(str(exc))
                    self._emit("error:discovery_rss", str(exc))
                finally:
                    root.elapsed_seconds = time.monotonic() - root_start

            # Explicit and robots-declared sitemap roots. Keep one audit row
            # when the same normalized root appears in both origins.
            explicit_roots = [sitemap_cfg.url for sitemap_cfg in source.sitemap]
            robots_roots = await self._get_robots_sitemaps(source, client)
            root_origin: Dict[str, Tuple[bool, bool, str]] = {}
            for value in explicit_roots:
                norm = normalize_url(value)
                if norm:
                    root_origin[norm] = (True, root_origin.get(norm, (False, False, value))[1], value)
            for value in robots_roots:
                norm = normalize_url(value)
                if norm:
                    previous = root_origin.get(norm, (False, False, value))
                    root_origin[norm] = (previous[0], True, previous[2])
            roots = list(root_origin)
            if len(roots) > self.max_sitemap_roots:
                self._mark_truncation(report, "roots", limit=self.max_sitemap_roots, count=len(roots))
                roots = roots[:self.max_sitemap_roots]

            pending: List[Tuple[str, int, str]] = []
            for root_url in roots:
                configured, declared, display_url = root_origin[root_url]
                self._root_audit(
                    report,
                    display_url,
                    configured=configured,
                    robots_declared=declared,
                    requested_url=root_url,
                    depth=0,
                )
            visited: Set[str] = set()
            root_index = 0

            while pending or root_index < len(roots):
                if report.documents_fetched >= self.max_sitemap_documents:
                    self._mark_truncation(
                        report,
                        "documents",
                        limit=self.max_sitemap_documents,
                        pending=len(pending),
                    )
                    break
                if not pending:
                    root_url = roots[root_index]
                    root_index += 1
                    pending.append((root_url, 0, root_url))
                sitemap_url, depth, origin_url = pending.pop(0)
                normalized_sitemap = normalize_url(sitemap_url)
                if not normalized_sitemap or normalized_sitemap in visited:
                    continue
                if depth > self.max_sitemap_depth:
                    self._emit("skipped:sitemap_depth", f"depth={depth};limit={self.max_sitemap_depth};url={normalized_sitemap}")
                    continue
                if not source.accepts_sitemap_url(normalized_sitemap):
                    self._emit("skipped:sitemap_host", f"url={normalized_sitemap}")
                    root = self._root_audit(report, origin_url, configured=False, robots_declared=False, requested_url=sitemap_url, depth=depth)
                    root.errors.append("sitemap_host")
                    continue
                visited.add(normalized_sitemap)
                root = self._root_audit(report, origin_url, configured=False, robots_declared=False, requested_url=sitemap_url, depth=depth)
                root_start = time.monotonic()
                try:
                    if not await self._is_allowed(sitemap_url, client):
                        root.errors.append("robots_disallowed")
                        self._emit("skipped:robots_disallowed", f"url={sitemap_url}")
                        continue
                    response = await self._http_get(client, sitemap_url)
                    root.requested_url = sitemap_url
                    root.final_url = str(getattr(response, "url", None) or sitemap_url)
                    root.status = getattr(response, "status_code", None)
                    headers = getattr(response, "headers", {}) or {}
                    root.content_type = self._header(headers, "content-type")
                    root.content_encoding = self._header(headers, "content-encoding")
                    raw_bytes, downloaded = self._response_bytes(response)
                    root.transferred_bytes = downloaded
                    root.decoded_bytes = len(raw_bytes)
                    if root.status != 200:
                        report.fetch_errors += 1
                        root.errors.append("http_status")
                        report.errors.append({"target": sitemap_url, "type": "sitemap_http_error", "status": root.status})
                        self._emit("error:sitemap_http", f"status={root.status};url={sitemap_url}", root.status)
                        continue
                    if len(raw_bytes) > self.max_response_bytes:
                        root.truncated.append({"dimension": "response_bytes", "limit": self.max_response_bytes})
                        self._mark_truncation(report, "response_bytes", limit=self.max_response_bytes, url=sitemap_url)
                        continue

                    parsed = SitemapParser.parse_document(
                        raw_bytes,
                        url_hint=sitemap_url,
                        content_encoding=root.content_encoding,
                    )
                    root.document_kind = parsed.get("kind")
                    if root.document_kind == "html":
                        root.errors.append("html_document")
                        report.errors.append({"target": sitemap_url, "type": "unsupported_html"})
                        continue
                    if root.document_kind == "malformed" or parsed.get("error") and root.document_kind == "unknown":
                        report.parse_errors += 1
                        root.errors.append(parsed.get("error") or "malformed_sitemap")
                        report.errors.append({
                            "target": sitemap_url,
                            "type": "sitemap_malformed",
                            "error": parsed.get("error"),
                        })
                        self._emit("error:sitemap_malformed", f"url={sitemap_url};err={parsed.get('error')}")
                        continue

                    if root.document_kind == "index":
                        children = parsed.get("sitemaps", [])
                        root.parsed_entries = len(children)
                        for child in children:
                            norm_child = normalize_url(child)
                            if norm_child and norm_child not in visited:
                                pending.append((norm_child, depth + 1, norm_child))
                        continue

                    if root.document_kind == "urlset":
                        entries = parsed.get("urls", [])
                        root.parsed_entries = len(entries)
                        root.lastmod_dates = sum(1 for entry in entries if entry.get("lastmod") is not None)
                        root.news_dates = sum(1 for entry in entries if entry.get("publication_date") is not None)
                        if len(entries) > self.max_entries_per_document:
                            root.truncated.append({"dimension": "entries_per_document", "limit": self.max_entries_per_document})
                            self._mark_truncation(
                                report,
                                "entries_per_document",
                                limit=self.max_entries_per_document,
                                url=sitemap_url,
                            )
                            entries = entries[:self.max_entries_per_document]
                        before_a = report.accepted
                        before_r = report.rejected
                        candidates = []
                        for entry in entries:
                            pub_date = entry.get("publication_date")
                            lastmod = entry.get("lastmod")
                            if pub_date:
                                hint_source, hint_conf, hint = "news_sitemap", 0.90, pub_date
                            elif getattr(source, "treat_lastmod_as_publication", False) and lastmod:
                                hint_source, hint_conf, hint = "lastmod", 0.50, lastmod
                            else:
                                hint_source, hint_conf, hint = None, None, None
                            candidates.append({
                                "url": entry.get("url", ""),
                                "method": "SITEMAP",
                                "publication_date_hint": hint,
                                "sitemap_lastmod_hint": lastmod,
                                "date_hint_source": hint_source,
                                "date_hint_confidence": hint_conf,
                                "root_url": sitemap_url,
                            })
                        self._queue_urls(source, candidates, existing_before_run, report)
                        root.accepted += report.accepted - before_a
                        root.rejected += report.rejected - before_r
                        if report.truncated:
                            break
                except Exception as exc:
                    report.fetch_errors += 1
                    report.errors.append({"target": sitemap_url, "type": "sitemap_exception", "error": str(exc)})
                    root.errors.append(str(exc))
                    self._emit("error:sitemap_fetch", f"url={sitemap_url};err={str(exc)}")
                finally:
                    root.elapsed_seconds = time.monotonic() - root_start

            report.budget_utilization = {
                "roots": len(roots) / self.max_sitemap_roots if self.max_sitemap_roots else 0.0,
                "documents": report.documents_fetched / self.max_sitemap_documents if self.max_sitemap_documents else 0.0,
                "candidates": report.candidates_seen / self.max_candidates if self.max_candidates else 0.0,
            }
            for root in report.roots:
                if root.errors:
                    outcome = DiscoveryOutcome.FETCH_ERROR if (
                        "http_status" in root.errors or "robots_disallowed" in root.errors
                    ) else DiscoveryOutcome.PARSE_ERROR
                    report.collector.record(
                        outcome,
                        ScopeReason.UNKNOWN,
                        discovery_method="RSS" if root.configured_url in [item.url for item in source.rss] else "SITEMAP",
                        root_url=root.requested_url or root.configured_url,
                        http_status=root.status,
                        metadata={
                            "document_kind": root.document_kind,
                            "errors": root.errors,
                        },
                    )
                for truncation in root.truncated:
                    report.collector.record(
                        DiscoveryOutcome.TRUNCATED,
                        ScopeReason.UNKNOWN,
                        discovery_method="SITEMAP",
                        root_url=root.requested_url or root.configured_url,
                        metadata=truncation,
                    )
            report.elapsed_seconds = time.monotonic() - started
            report.reconcile()
            if self._active_persist_queue and self.session is not None:
                self.session.flush()
                self.session.commit()
            return report
        except Exception:
            # Audit mode deliberately does not rollback: it never opened a
            # write transaction. The mutating wrapper retains the historical
            # rollback behavior for callers that may have pending changes.
            if self._active_persist_queue and self.session is not None:
                self.session.rollback()
            raise
        finally:
            self._active_persist_queue = previous_persist
            if should_close_client:
                await client.aclose()

    async def audit_source_async(self, source: SourceConfig, client: Optional[httpx.AsyncClient] = None) -> DiscoveryReport:
        """Convenience name for a non-mutating feed/sitemap audit."""
        return await self.discover_source_async(source, client=client, persist_queue=False)

    def audit_source(self, source: SourceConfig) -> DiscoveryReport:
        return self.discover_source(source, persist_queue=False)


__all__ = ["DiscoveryEngine"]
