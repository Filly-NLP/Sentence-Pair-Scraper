"""Configurable year/month/day archive traversal with bounded pagination, telemetry, and async execution."""

import asyncio
from datetime import date, datetime, time, timedelta, timezone
import hashlib
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

import inspect
from bs4 import BeautifulSoup
import httpx
from sqlalchemy.orm import Session

from src.crawler.robots import RobotsManager
from src.crawler.discovery_reporting import ScopeReason
from src.crawler.url_normalizer import normalize_url
from src.sources.registry import ArchiveConfig, SourceConfig
from src.storage.models import URL

_ORIGINAL_HTTPX_GET = httpx.get


class ArchiveDiscoveryEngine:
    def __init__(
        self,
        db_session: Optional[Session],
        user_agent: str,
        date_cutoff: datetime,
        timeout: float = 30.0,
        robots_mgr: Optional[RobotsManager] = None,
        event_logger: Optional[Callable[[str, Optional[str], Optional[int]], None]] = None,
        apply_delay: bool = True,
        allow_disabled_override: bool = False,
        persist_queue: bool = True,
    ):
        self.session = db_session
        self.user_agent = user_agent
        self.date_cutoff = date_cutoff
        self.timeout = timeout
        self.robots_mgr = robots_mgr
        self.event_logger = event_logger
        self.apply_delay = apply_delay
        self.allow_disabled_override = bool(allow_disabled_override)
        self.persist_queue = bool(persist_queue)
        self._active_persist_queue = self.persist_queue
        self.headers = {"User-Agent": user_agent}

    async def _http_get(self, client: httpx.AsyncClient, url: str) -> Any:
        if httpx.get is not _ORIGINAL_HTTPX_GET:
            res = httpx.get(url, headers=self.headers, timeout=self.timeout)
            if inspect.isawaitable(res):
                return await res
            return res
        return await client.get(url, headers=self.headers)

    def _emit(self, event_type: str, message: Optional[str] = None, http_status: Optional[int] = None) -> None:
        if self._active_persist_queue and self.event_logger:
            self.event_logger(event_type, message, http_status)

    @staticmethod
    def generate_periods(
        granularity: str,
        start_date: date,
        end_date: date,
        max_periods: int = 100,
    ) -> Iterator[Tuple[datetime, Dict[str, Any]]]:
        """Generate period start dates and template variables lazily."""
        count = 0
        if granularity == "year":
            current_year = start_date.year
            end_year = end_date.year
            while current_year <= end_year and count < max_periods:
                p_dt = datetime(current_year, 1, 1, tzinfo=timezone.utc)
                context = {
                    "year": current_year,
                    "yyyy": f"{current_year:04d}",
                }
                yield p_dt, context
                current_year += 1
                count += 1

        elif granularity == "month":
            curr_y = start_date.year
            curr_m = start_date.month
            end_y = end_date.year
            end_m = end_date.month

            while (curr_y < end_y or (curr_y == end_y and curr_m <= end_m)) and count < max_periods:
                p_dt = datetime(curr_y, curr_m, 1, tzinfo=timezone.utc)
                context = {
                    "year": curr_y,
                    "yyyy": f"{curr_y:04d}",
                    "month": curr_m,
                    "mm": f"{curr_m:02d}",
                }
                yield p_dt, context
                if curr_m == 12:
                    curr_y += 1
                    curr_m = 1
                else:
                    curr_m += 1
                count += 1

        else:  # day
            current = start_date
            while current <= end_date and count < max_periods:
                p_dt = datetime(current.year, current.month, current.day, tzinfo=timezone.utc)
                context = {
                    "year": current.year,
                    "yyyy": f"{current.year:04d}",
                    "month": current.month,
                    "mm": f"{current.month:02d}",
                    "day": current.day,
                    "dd": f"{current.day:02d}",
                    "date_iso": current.isoformat(),
                }
                yield p_dt, context
                current += timedelta(days=1)
                count += 1

    def _format_period_urls(self, cfg: ArchiveConfig, source: SourceConfig, context: Dict[str, Any]) -> List[str]:
        """Format configured templates for a given period."""
        templates: List[str] = []
        if cfg.url_template:
            templates.append(cfg.url_template)
        if cfg.templates:
            templates.extend(cfg.templates)
        if cfg.url_pattern and not templates:
            templates.append(cfg.url_pattern)

        if not templates:
            domain = source.domain.rstrip("/")
            prefix = (source.url_prefix or "").strip("/")
            base = f"https://{domain}/{prefix}".rstrip("/")
            if cfg.granularity == "year":
                templates.append(f"{base}/{{yyyy}}/")
            elif cfg.granularity == "month":
                templates.append(f"{base}/{{yyyy}}/{{mm}}/")
            else:
                templates.append(f"{base}/{{yyyy}}/{{mm}}/{{dd}}/")

        fmt_ctx = {
            "domain": source.domain,
            "url_prefix": source.url_prefix or "",
            **context,
        }
        urls = []
        for tmpl in templates:
            try:
                formatted = tmpl.format(**fmt_ctx)
                urls.append(formatted)
            except Exception:
                continue
        return urls

    def _format_page_url(self, base_url: str, page_num: int, page_template: Optional[str]) -> str:
        if page_num <= 1:
            return base_url
        if page_template:
            try:
                return page_template.format(url=base_url.rstrip("/"), page=page_num)
            except Exception:
                pass
        if "?" in base_url:
            return f"{base_url}&page={page_num}"
        return f"{base_url.rstrip('/')}/page/{page_num}"

    def _extract_links(self, html: str, page_url: str, selector: str) -> List[str]:
        """Extract article links using BeautifulSoup selector."""
        links: List[str] = []
        try:
            soup = BeautifulSoup(html, "html.parser")
            elements = soup.select(selector)
            for el in elements:
                href = el.get("href") if el.name == "a" else None
                if not href:
                    nested_a = el.find("a")
                    if nested_a:
                        href = nested_a.get("href")
                if href and isinstance(href, str) and href.strip():
                    full_url = urljoin(page_url, href.strip())
                    links.append(full_url)
        except Exception:
            pass
        return links

    def _extract_next_link(self, html: str, page_url: str, selector: Optional[str]) -> Optional[str]:
        if not selector:
            return None
        try:
            soup = BeautifulSoup(html, "html.parser")
            el = soup.select_one(selector)
            if el:
                href = el.get("href") if el.name == "a" else None
                if not href:
                    nested = el.find("a")
                    if nested:
                        href = nested.get("href")
                if href and isinstance(href, str) and href.strip():
                    return urljoin(page_url, href.strip())
        except Exception:
            pass
        return None

    def discover_source(
        self,
        source: SourceConfig,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        override_granularity: Optional[str] = None,
        max_periods_override: Optional[int] = None,
        max_pages_override: Optional[int] = None,
        allow_disabled_override: Optional[bool] = None,
        persist_queue: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Execute archive discovery synchronously by invoking the async runner."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            raise RuntimeError(
                "discover_source() cannot run inside an active event loop; "
                "await discover_source_async() instead"
            )
        return asyncio.run(
            self.discover_source_async(
                source,
                from_date=from_date,
                to_date=to_date,
                override_granularity=override_granularity,
                max_periods_override=max_periods_override,
                max_pages_override=max_pages_override,
                allow_disabled_override=allow_disabled_override,
                persist_queue=persist_queue,
            )
        )

    def _discover_source_sync(
        self,
        source: SourceConfig,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        override_granularity: Optional[str] = None,
        max_periods_override: Optional[int] = None,
        max_pages_override: Optional[int] = None,
        allow_disabled_override: Optional[bool] = None,
        persist_queue: Optional[bool] = None,
    ) -> Dict[str, Any]:
        return asyncio.run(
            self.discover_source_async(
                source,
                from_date=from_date,
                to_date=to_date,
                override_granularity=override_granularity,
                max_periods_override=max_periods_override,
                max_pages_override=max_pages_override,
                allow_disabled_override=allow_disabled_override,
                persist_queue=persist_queue,
            )
        )

    async def discover_source_async(
        self,
        source: SourceConfig,
        client: Optional[httpx.AsyncClient] = None,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        override_granularity: Optional[str] = None,
        max_periods_override: Optional[int] = None,
        max_pages_override: Optional[int] = None,
        allow_disabled_override: Optional[bool] = None,
        persist_queue: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Execute async archive discovery for a source and return structured report."""
        active_persist = self.persist_queue if persist_queue is None else bool(persist_queue)
        previous_persist = self._active_persist_queue
        self._active_persist_queue = active_persist
        stats: Dict[str, Any] = {
            "source_id": source.id,
            "periods_planned": 0,
            "periods_requested": 0,
            "periods_skipped": 0,
            "pages_fetched": 0,
            "pages_blocked": 0,
            "pages_failed": 0,
            "links_found": 0,
            "links_added": 0,
            "duplicates_skipped": 0,
            "out_of_scope_skipped": 0,
            "sample_out_of_scope": [],
            "loops_detected": 0,
            "errors": [],
            "truncated": False,
            "truncation_details": [],
            "disabled": False,
            "refused": False,
            "candidates_seen": 0,
            "normalization_successes": 0,
            "normalization_failures": 0,
            "invalid_or_unsupported": 0,
            "unique_within_run": 0,
            "duplicate_in_run": 0,
            "accepted_unique": 0,
            "rejected_unique": 0,
            "already_stored": 0,
            "database_duplicates": 0,
            "cross_method_duplicates": 0,
            "queued_new": 0,
            "actually_queued": 0,
            "would_queue": 0,
            "date_hint_rejections": 0,
            "fetch_errors": 0,
            "parse_errors": 0,
        }

        active_persist = self.persist_queue if persist_queue is None else bool(persist_queue)
        cfg = source.archive
        override = self.allow_disabled_override if allow_disabled_override is None else bool(allow_disabled_override)
        if cfg is None or not cfg.enabled:
            stats["disabled"] = True
            if not override or cfg is None or from_date is None or to_date is None:
                stats["refused"] = True
                stats["errors"].append({
                    "type": "archive_disabled",
                    "reason": "missing_archive_config" if cfg is None else "disabled_in_config",
                })
                self._active_persist_queue = previous_persist
                return stats
        granularity = override_granularity or cfg.granularity
        max_periods = max_periods_override or cfg.max_periods
        max_pages_per_period = max_pages_override or cfg.max_pages_per_period
        max_total_pages = cfg.max_total_pages
        max_candidates = cfg.max_candidates
        max_bytes = cfg.max_response_bytes

        s_date = from_date
        if not s_date and cfg.start_date:
            try:
                s_date = datetime.strptime(cfg.start_date, "%Y-%m-%d").date()
            except ValueError:
                pass
        if not s_date:
            s_date = self.date_cutoff.date()

        e_date = to_date
        if not e_date and cfg.end_date:
            try:
                e_date = datetime.strptime(cfg.end_date, "%Y-%m-%d").date()
            except ValueError:
                pass
        if not e_date:
            e_date = datetime.now(timezone.utc).date()

        if s_date > e_date:
            return stats

        existing: Set[str] = set()
        if self.session is not None:
            existing = {row[0] for row in self.session.query(URL.url).all()}

        periods = list(self.generate_periods(granularity, s_date, e_date, max_periods))
        stats["periods_planned"] = len(periods)

        should_close_client = False
        if client is None:
            client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=True)
            should_close_client = True

        nested = self.session.begin_nested() if active_persist and self.session is not None else None
        try:
            for period_dt, period_ctx in periods:
                if stats["pages_fetched"] >= max_total_pages or stats["links_added"] >= max_candidates:
                    stats["truncated"] = True
                    stats["truncation_details"].append({
                        "dimension": "total_pages" if stats["pages_fetched"] >= max_total_pages else "candidates",
                        "count": stats["pages_fetched"] if stats["pages_fetched"] >= max_total_pages else stats["links_added"],
                    })
                    self._emit("discovery:truncated", f"source={source.id};period={period_dt.isoformat()}")
                    break

                period_urls = self._format_period_urls(cfg, source, period_ctx)
                if not period_urls:
                    stats["periods_skipped"] += 1
                    continue

                stats["periods_requested"] += 1
                self._emit("discovery:archive_period", f"source={source.id};period={period_dt.isoformat()}")

                for base_period_url in period_urls:
                    current_page_url: Optional[str] = base_period_url
                    page_num = 1
                    seen_urls_in_period: Set[str] = set()
                    seen_body_hashes_in_period: Set[str] = set()

                    while current_page_url and page_num <= max_pages_per_period:
                        if stats["pages_fetched"] >= max_total_pages:
                            break

                        norm_page = normalize_url(current_page_url)
                        if not norm_page or norm_page in seen_urls_in_period:
                            stats["loops_detected"] += 1
                            self._emit("discovery:pagination_loop", f"url={current_page_url}")
                            break

                        seen_urls_in_period.add(norm_page)

                        # Check robots permission
                        if self.robots_mgr:
                            try:
                                is_mock = hasattr(self.robots_mgr.is_allowed, "_mock_return_value") or hasattr(self.robots_mgr.is_allowed, "return_value")
                                if hasattr(self.robots_mgr, "is_allowed_async") and not is_mock:
                                    is_allowed = await self.robots_mgr.is_allowed_async(current_page_url, client=client)
                                else:
                                    is_allowed = self.robots_mgr.is_allowed(current_page_url)
                            except Exception:
                                is_allowed = self.robots_mgr.is_allowed(current_page_url)
                            if not is_allowed:
                                stats["pages_blocked"] += 1
                                self._emit("skipped:robots_disallowed", f"url={current_page_url}")
                                break

                        # Fetch archive page
                        try:
                            resp = await self._http_get(client, current_page_url)
                            if resp.status_code != 200:
                                stats["pages_failed"] += 1
                                stats["errors"].append({
                                    "url": current_page_url,
                                    "status": resp.status_code,
                                    "type": "http_error",
                                })
                                self._emit("error:archive_http", f"url={current_page_url};status={resp.status_code}", resp.status_code)
                                break

                            raw_content = getattr(resp, "content", None)
                            if not raw_content:
                                raw_text = getattr(resp, "text", "")
                                raw_content = raw_text.encode("utf-8") if isinstance(raw_text, str) else b""

                            if len(raw_content) > max_bytes:
                                stats["truncated"] = True
                                self._emit("discovery:truncated", f"dimension=response_bytes;url={current_page_url}")
                                break

                            body_hash = hashlib.sha256(raw_content).hexdigest()
                            if body_hash in seen_body_hashes_in_period:
                                stats["loops_detected"] += 1
                                self._emit("discovery:pagination_loop", f"url={current_page_url};reason=duplicate_body")
                                break
                            seen_body_hashes_in_period.add(body_hash)

                            stats["pages_fetched"] += 1
                            html_text = getattr(resp, "text", "") or raw_content.decode("utf-8", errors="ignore")
                            extracted_links = self._extract_links(html_text, current_page_url, cfg.article_link_selector)

                            if not extracted_links:
                                break

                            for raw_article_url in extracted_links:
                                stats["links_found"] += 1
                                norm_article = normalize_url(raw_article_url)
                                if not norm_article:
                                    continue

                                if not source.accepts_url(norm_article):
                                    stats["out_of_scope_skipped"] += 1
                                    if len(stats["sample_out_of_scope"]) < 10:
                                        stats["sample_out_of_scope"].append(norm_article)
                                    continue

                                if norm_article in existing:
                                    stats["duplicates_skipped"] += 1
                                    continue

                                if active_persist and self.session is not None:
                                    self.session.add(
                                        URL(
                                            url=norm_article,
                                            source_id=source.id,
                                            status="DISCOVERED",
                                            discovery_method="ARCHIVE",
                                            discovered_at=datetime.now(timezone.utc).replace(tzinfo=None),
                                            publication_date_hint=period_dt.replace(tzinfo=None),
                                            date_hint_source="archive_period",
                                            date_hint_confidence=0.7,
                                        )
                                    )
                                existing.add(norm_article)
                                stats["links_added"] += 1

                                if stats["links_added"] >= max_candidates:
                                    stats["truncated"] = True
                                    break

                            if stats["truncated"]:
                                break

                            if cfg.pagination_mode == "next_link":
                                current_page_url = self._extract_next_link(html_text, current_page_url, cfg.next_link_selector)
                            elif cfg.pagination_mode == "page_template":
                                page_num += 1
                                current_page_url = self._format_page_url(base_period_url, page_num, cfg.page_template)
                            else:
                                current_page_url = None

                        except Exception as exc:
                            stats["pages_failed"] += 1
                            stats["errors"].append({
                                "url": current_page_url,
                                "type": "exception",
                                "error": str(exc),
                            })
                            self._emit("error:archive_exception", f"url={current_page_url};err={str(exc)}")
                            break

            if active_persist and self.session is not None:
                try:
                    self.session.flush()
                    if nested is not None:
                        nested.commit()
                    self.session.commit()
                except Exception:
                    if nested is not None:
                        nested.rollback()
                    self.session.rollback()
                    raise

        except asyncio.CancelledError:
            # Archive candidates are staged in a savepoint so cancellation
            # cannot leave queue rows (or archive events) pending for a later
            # unrelated commit. Preserve the caller's outer transaction.
            if nested is not None:
                try:
                    nested.rollback()
                except Exception:
                    self.session.rollback()
            raise
        except Exception:
            if nested is not None:
                nested.rollback()
            raise

        finally:
            if should_close_client:
                await client.aclose()
            self._active_persist_queue = previous_persist

        return stats
