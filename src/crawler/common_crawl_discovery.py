"""Bounded, metadata-only Common Crawl index discovery.

Common Crawl is treated as a seed provider here.  It can contribute publisher
URLs to the existing queue, but it is never used as an article-body source.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import asdict, dataclass, replace
from datetime import datetime
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from src.crawler.config import CommonCrawlConfig, is_common_crawl_collection
from src.crawler.discovery import DiscoveryEngine
from src.crawler.discovery_reporting import DiscoveryOutcome, DiscoveryReport, RootAudit, ScopeReason
from src.sources.registry import SourceConfig
from src.storage.models import URL


COMMON_CRAWL_INDEX_HOST = "index.commoncrawl.org"
COMMON_CRAWL_INDEX_BASE = f"https://{COMMON_CRAWL_INDEX_HOST}"
_CACHE_VERSION = 1


class CommonCrawlError(RuntimeError):
    """Base error for a Common Crawl index operation."""


class CommonCrawlNetworkError(CommonCrawlError):
    """Raised when an index request cannot be completed."""


class _BudgetStop(Exception):
    """Internal control flow for an exhausted provider budget."""


@dataclass(frozen=True)
class CommonCrawlStats:
    """Counters exposed in ``DiscoveryReport.metadata['common_crawl']``."""

    requests_made: int = 0
    metadata_requests: int = 0
    page_requests: int = 0
    pages_fetched: int = 0
    pages_failed: int = 0
    cache_hits: int = 0
    response_bytes: int = 0
    raw_candidates_seen: int = 0
    invalid_records: int = 0
    malformed_lines: int = 0
    cache_write_failures: int = 0


class CommonCrawlCache:
    """Atomic, integrity-checked cache for complete index responses.

    The constructor is intentionally side-effect free.  In particular, a
    disabled provider must not create its cache directory.
    """

    def __init__(self, cache_dir: str | Path, version: int = _CACHE_VERSION):
        self.cache_dir = Path(cache_dir)
        self.version = int(version)

    @staticmethod
    def _canonical_identity(identity: Mapping[str, Any]) -> str:
        if not isinstance(identity, Mapping):
            raise TypeError("Common Crawl cache identity must be a mapping")
        return json.dumps(dict(identity), sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    def key_for(self, identity: Mapping[str, Any]) -> str:
        canonical = self._canonical_identity(identity).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def path_for(self, identity: Mapping[str, Any]) -> Path:
        # The filename is a digest rather than user-controlled URL material,
        # so it cannot escape cache_dir or create nested paths.
        return self.cache_dir / f"{self.key_for(identity)}.json"

    # Friendly aliases used by callers and tests.
    cache_path = path_for
    get_path = path_for

    def get(self, identity: Mapping[str, Any]) -> Optional[bytes]:
        path = self.path_for(identity)
        try:
            with path.open("r", encoding="utf-8") as handle:
                envelope = json.load(handle)
            if not isinstance(envelope, dict):
                return None
            if envelope.get("version") != self.version:
                return None
            if envelope.get("request") != dict(identity):
                return None
            encoded = envelope.get("body_base64")
            if not isinstance(encoded, str):
                return None
            body = base64.b64decode(encoded.encode("ascii"), validate=True)
            if envelope.get("byte_count") != len(body):
                return None
            if envelope.get("sha256") != hashlib.sha256(body).hexdigest():
                return None
            return body
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None

    read = get
    load = get

    def set(self, identity: Mapping[str, Any], body: bytes) -> bool:
        if not isinstance(body, (bytes, bytearray)):
            raise TypeError("Common Crawl cache bodies must be bytes")
        body_bytes = bytes(body)
        path = self.path_for(identity)
        temporary_path: Optional[Path] = None
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{path.stem}.", suffix=".tmp", dir=str(self.cache_dir)
            )
            temporary_path = Path(temporary_name)
            envelope = {
                "version": self.version,
                "request": dict(identity),
                "body_base64": base64.b64encode(body_bytes).decode("ascii"),
                "byte_count": len(body_bytes),
                "sha256": hashlib.sha256(body_bytes).hexdigest(),
            }
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(envelope, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temporary_path), str(path))
            temporary_path = None
            return True
        except Exception:
            return False
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    store = set


class CommonCrawlDiscoveryEngine:
    """Discover publisher URL seeds from one pinned Common Crawl collection."""

    def __init__(
        self,
        db_session: Optional[Session],
        user_agent: str,
        config: Optional[CommonCrawlConfig | Mapping[str, Any]] = None,
        common_crawl_config: Optional[CommonCrawlConfig | Mapping[str, Any]] = None,
        date_cutoff: Optional[datetime] = None,
        timeout: Optional[float] = None,
        event_logger: Optional[Callable[[str, Optional[str], Optional[int]], None]] = None,
        persist_queue: bool = True,
        cache: Optional[CommonCrawlCache] = None,
        cache_dir: Optional[str | Path] = None,
        index_collection: Optional[str] = None,
        **overrides: Any,
    ):
        # Accept a full CrawlerConfig-like object as a convenience for direct
        # callers while keeping the provider's runtime contract explicit.
        if isinstance(config, datetime) and date_cutoff is None:
            date_cutoff = config
            config = None
        if config is None and common_crawl_config is not None:
            config = common_crawl_config
        if config is not None and not isinstance(config, (CommonCrawlConfig, Mapping)) and hasattr(config, "get"):
            config = config.get("common_crawl", {}) or {}
        if config is None:
            config_values: Dict[str, Any] = {}
            if index_collection is not None:
                config_values["index_collection"] = index_collection
            config_values.update(overrides)
            config = config_values
        elif isinstance(config, Mapping):
            config_values = dict(config)
            if index_collection is not None:
                config_values["index_collection"] = index_collection
            config_values.update(overrides)
            config = config_values

        if isinstance(config, CommonCrawlConfig):
            config_values = asdict(config)
            if index_collection is not None:
                config_values["index_collection"] = index_collection
            config_values.update(overrides)
            self.config = CommonCrawlConfig.from_mapping(config_values)
        else:
            self.config = CommonCrawlConfig.from_mapping(config)
        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
                raise ValueError("Common Crawl timeout must be a positive number")
            self.config = replace(self.config, timeout_seconds=float(timeout))
        if self.config.enabled and not is_common_crawl_collection(self.config.index_collection):
            raise ValueError("Common Crawl requires an explicit CC-MAIN-YYYY-WW index collection")

        self.session = db_session
        self.user_agent = user_agent
        self.date_cutoff = date_cutoff or datetime.min
        self.event_logger = event_logger
        self.persist_queue = bool(persist_queue)
        self.headers = {"User-Agent": user_agent, "Accept": "application/json"}
        self.cache = cache or CommonCrawlCache(cache_dir or self.config.cache_dir)
        self.last_stats: Dict[str, Any] = {}
        self._active_persist_queue = self.persist_queue

    @property
    def endpoint(self) -> str:
        return f"{COMMON_CRAWL_INDEX_BASE}/{self.config.index_collection}-index"

    def _emit(self, event_type: str, message: Optional[str] = None, status: Optional[int] = None) -> None:
        if self._active_persist_queue and self.event_logger:
            self.event_logger(event_type, message, status)

    @staticmethod
    def _source_common_crawl(source: SourceConfig) -> Tuple[bool, list[str]]:
        raw = getattr(source, "common_crawl", None)
        if isinstance(raw, Mapping):
            enabled = raw.get("enabled", False)
            patterns = raw.get("url_patterns", [])
        else:
            enabled = getattr(raw, "enabled", False)
            patterns = getattr(raw, "url_patterns", [])
        if not isinstance(patterns, (list, tuple)):
            patterns = []
        expected_host = (getattr(source, "domain", "") or "").lower().removeprefix("www.").split(":", 1)[0]
        safe_patterns = []
        for pattern in patterns:
            if not isinstance(pattern, str) or not pattern.strip():
                continue
            parsed = urlparse(pattern.strip())
            actual_host = (parsed.hostname or "").lower().removeprefix("www.")
            if parsed.scheme not in {"http", "https"} or actual_host != expected_host:
                continue
            if parsed.query or parsed.fragment or parsed.params:
                continue
            path = parsed.path or "/"
            literal_path = re.sub(r"[*?]", "", path).strip("/")
            if not literal_path and expected_host in {"philstar.com"}:
                continue
            safe_patterns.append(pattern.strip())
        return bool(enabled), safe_patterns

    def _new_disabled_report(self, source: SourceConfig, reason: str) -> DiscoveryReport:
        report = DiscoveryReport(source_id=source.id, discovery_method="COMMON_CRAWL")
        report.metadata["common_crawl"] = {"enabled": False, "reason": reason}
        return report

    def _request_params(self, pattern: str, kind: str, page: Optional[int] = None) -> Dict[str, str]:
        params = {"url": pattern, "output": "json"}
        if kind == "metadata":
            params["showNumPages"] = "true"
        elif page is not None:
            params["page"] = str(page)
        return params

    def request_url(self, pattern: str, kind: str, page: Optional[int] = None) -> str:
        return str(httpx.URL(self.endpoint, params=self._request_params(pattern, kind, page)))

    @staticmethod
    def _response_status(response: Any) -> Optional[int]:
        try:
            return int(response.status_code)
        except (AttributeError, TypeError, ValueError):
            return None

    @staticmethod
    def _response_headers(response: Any) -> Mapping[str, str]:
        headers = getattr(response, "headers", {}) or {}
        return dict(headers)

    @staticmethod
    def _response_url(response: Any, fallback: str) -> str:
        value = getattr(response, "url", None)
        return str(value) if value else fallback

    @staticmethod
    def _known_index_url(value: str) -> bool:
        parsed = urlparse(value)
        return parsed.scheme == "https" and (parsed.hostname or "").lower() == COMMON_CRAWL_INDEX_HOST

    async def _open_stream(self, client: Any, params: Mapping[str, str]) -> Any:
        kwargs = {
            "params": dict(params),
            "headers": self.headers,
            "timeout": self.config.timeout_seconds,
            "follow_redirects": False,
        }
        stream = getattr(client, "stream", None)
        if callable(stream):
            context = stream("GET", self.endpoint, **kwargs)
            if inspect.isawaitable(context):
                context = await context
            if not hasattr(context, "__aenter__"):
                return _SingleResponseContext(context)
            return context
        # A small compatibility seam for simple fake clients in unit tests.
        response = await client.get(self.endpoint, **kwargs)
        return _SingleResponseContext(response)

    async def _read_response(
        self,
        client: Any,
        params: Mapping[str, str],
        stats: Dict[str, Any],
    ) -> Tuple[Any, bytes, bool, Optional[str], int]:
        stats["requests_made"] += 1
        if stats.get("request_kind") == "metadata":
            stats["metadata_requests"] += 1
        else:
            stats["page_requests"] += 1
        fallback_url = self.request_url(params["url"], stats.get("request_kind", "page"), int(params["page"]) if "page" in params else None)
        try:
            async with await self._open_stream(client, params) as response:
                status = self._response_status(response)
                response_url = self._response_url(response, fallback_url)
                if status != 200:
                    return response, b"", False, "http_status", 0
                if not self._known_index_url(response_url):
                    return response, b"", False, "redirect_out_of_scope", 0

                headers = self._response_headers(response)
                content_length = headers.get("content-length") or headers.get("Content-Length")
                try:
                    declared_length = int(content_length) if content_length is not None else None
                except (TypeError, ValueError):
                    declared_length = None
                if declared_length is not None and declared_length > self.config.max_response_bytes:
                    return response, b"", False, "response_bytes", 0
                if declared_length is not None and declared_length > self.config.max_total_response_bytes_per_source_run - stats["response_bytes"]:
                    return response, b"", False, "total_response_bytes", 0

                chunks: list[bytes] = []
                response_size = 0
                consumed = 0
                aiter_bytes = getattr(response, "aiter_bytes", None)
                if callable(aiter_bytes):
                    async for chunk in aiter_bytes():
                        chunk = bytes(chunk)
                        consumed += len(chunk)
                        if response_size + len(chunk) > self.config.max_response_bytes:
                            return response, b"".join(chunks), False, "response_bytes", consumed
                        if stats["response_bytes"] + response_size + len(chunk) > self.config.max_total_response_bytes_per_source_run:
                            return response, b"".join(chunks), False, "total_response_bytes", consumed
                        chunks.append(chunk)
                        response_size += len(chunk)
                else:
                    raw = getattr(response, "content", None)
                    if not raw:
                        text = getattr(response, "text", "")
                        raw = text.encode("utf-8") if isinstance(text, str) else b""
                    raw = bytes(raw)
                    consumed = len(raw)
                    if len(raw) > self.config.max_response_bytes:
                        return response, raw[: self.config.max_response_bytes], False, "response_bytes", consumed
                    if stats["response_bytes"] + len(raw) > self.config.max_total_response_bytes_per_source_run:
                        return response, raw[: max(0, self.config.max_total_response_bytes_per_source_run - stats["response_bytes"])], False, "total_response_bytes", consumed
                    chunks.append(raw)
                body = b"".join(chunks)
                return response, body, True, None, consumed
        except (httpx.HTTPError, OSError, TimeoutError) as exc:
            raise CommonCrawlNetworkError(str(exc)) from exc

    @staticmethod
    def _page_count(body: bytes) -> Optional[int]:
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if isinstance(value, dict):
            value = value.get("pages")
        try:
            count = int(value)
        except (TypeError, ValueError):
            return None
        return count if count >= 0 else None

    @staticmethod
    def _record_root(
        report: DiscoveryReport,
        request_url: str,
        response: Any,
        body_size: int,
        *,
        error: Optional[str] = None,
        document_kind: Optional[str] = None,
    ) -> None:
        headers = CommonCrawlDiscoveryEngine._response_headers(response)
        root = RootAudit(
            configured_url=request_url,
            origin_configured=True,
            requested_url=request_url,
            final_url=CommonCrawlDiscoveryEngine._response_url(response, request_url),
            status=CommonCrawlDiscoveryEngine._response_status(response),
            content_type=headers.get("content-type") or headers.get("Content-Type"),
            transferred_bytes=body_size,
            decoded_bytes=body_size,
            document_kind=document_kind,
        )
        if error:
            root.errors.append(error)
        report.roots.append(root)

    async def discover_source_async(
        self,
        source: SourceConfig,
        client: Optional[httpx.AsyncClient] = None,
        persist_queue: Optional[bool] = None,
    ) -> DiscoveryReport:
        active_persist = self.persist_queue if persist_queue is None else bool(persist_queue)
        self._active_persist_queue = active_persist
        source_enabled, patterns = self._source_common_crawl(source)
        if not self.config.enabled:
            return self._new_disabled_report(source, "global_disabled")
        if not source_enabled:
            return self._new_disabled_report(source, "source_disabled")
        if not patterns:
            return self._new_disabled_report(source, "source_patterns_missing")

        started = time.monotonic()
        stats: Dict[str, Any] = {
            **asdict(CommonCrawlStats()),
            "enabled": True,
            "collection": self.config.index_collection,
            "patterns": list(patterns),
            "truncation_dimensions": [],
        }
        report: Optional[DiscoveryReport] = None
        discovery: Optional[DiscoveryEngine] = None
        existing_before_run: set[str] = set()
        if active_persist:
            discovery = DiscoveryEngine(
                db_session=self.session,
                user_agent=self.user_agent,
                date_cutoff=self.date_cutoff,
                timeout=self.config.timeout_seconds,
                max_candidates=self.config.max_candidates_per_source_run,
                max_response_bytes=self.config.max_response_bytes,
                persist_queue=True,
            )
            report = discovery.new_report(source, discovery_method="COMMON_CRAWL")
            if self.session is not None:
                with self.session.no_autoflush:
                    existing_before_run = {row[0] for row in self.session.query(URL.url).all()}
        else:
            discovery = DiscoveryEngine(
                db_session=None,
                user_agent=self.user_agent,
                date_cutoff=self.date_cutoff,
                timeout=self.config.timeout_seconds,
                max_candidates=self.config.max_candidates_per_source_run,
                max_response_bytes=self.config.max_response_bytes,
                persist_queue=False,
            )
            report = discovery.new_report(source, discovery_method="COMMON_CRAWL")

        nested = self.session.begin_nested() if active_persist and self.session is not None else None
        should_close = False
        if client is None:
            client = httpx.AsyncClient(timeout=self.config.timeout_seconds, follow_redirects=False)
            should_close = True
        stop_all = False
        try:
            for pattern in patterns:
                if stop_all:
                    break
                metadata_identity = {
                    "provider": "common_crawl",
                    "collection": self.config.index_collection,
                    "pattern": pattern,
                    "kind": "metadata",
                    "page": None,
                }
                body = self.cache.get(metadata_identity)
                response: Any = _SyntheticResponse(self.request_url(pattern, "metadata"), 200) if body is not None else None
                if body is not None:
                    stats["cache_hits"] += 1
                    if len(body) > self.config.max_response_bytes:
                        discovery.mark_truncation(report, "response_bytes", limit=self.config.max_response_bytes, pattern=pattern)
                        stats["truncation_dimensions"].append("response_bytes")
                        break
                    if stats["response_bytes"] + len(body) > self.config.max_total_response_bytes_per_source_run:
                        discovery.mark_truncation(report, "total_response_bytes", limit=self.config.max_total_response_bytes_per_source_run, pattern=pattern)
                        stats["truncation_dimensions"].append("total_response_bytes")
                        break
                    stats["response_bytes"] += len(body)
                    self._record_root(report, self.request_url(pattern, "metadata"), response, len(body), document_kind="pagination")
                else:
                    if stats["requests_made"] >= self.config.max_requests_per_source_run:
                        discovery.mark_truncation(report, "requests", limit=self.config.max_requests_per_source_run, count=stats["requests_made"])
                        stats["truncation_dimensions"].append("requests")
                        break
                    stats["request_kind"] = "metadata"
                    response, body, complete, reason, consumed = await self._read_response(
                        client, self._request_params(pattern, "metadata"), stats
                    )
                    stats["response_bytes"] += consumed
                    if self._response_status(response) != 200:
                        report.fetch_errors += 1
                        stats["pages_failed"] += 1
                        self._record_root(report, self.request_url(pattern, "metadata"), response, consumed, error=reason or "http_status")
                        report.errors.append({"target": self.request_url(pattern, "metadata"), "type": "common_crawl_http_error", "status": self._response_status(response)})
                        report.collector.record(
                            DiscoveryOutcome.FETCH_ERROR,
                            ScopeReason.UNKNOWN,
                            discovery_method="COMMON_CRAWL",
                            root_url=self.request_url(pattern, "metadata"),
                            http_status=self._response_status(response),
                        )
                        continue
                    self._record_root(report, self.request_url(pattern, "metadata"), response, len(body), error=reason, document_kind="pagination")
                    if not complete:
                        discovery.mark_truncation(report, reason or "response_bytes", limit=self.config.max_response_bytes, pattern=pattern)
                        stats["truncation_dimensions"].append(reason or "response_bytes")
                        stop_all = True
                        break
                    if not self.cache.set(metadata_identity, body):
                        stats["cache_write_failures"] += 1

                page_count = self._page_count(body)
                if page_count is None:
                    report.parse_errors += 1
                    report.errors.append({"target": self.request_url(pattern, "metadata"), "type": "common_crawl_pagination_malformed"})
                    report.collector.record(
                        DiscoveryOutcome.PARSE_ERROR,
                        ScopeReason.UNKNOWN,
                        discovery_method="COMMON_CRAWL",
                        root_url=self.request_url(pattern, "metadata"),
                    )
                    continue

                for page in range(page_count):
                    if stats["pages_fetched"] >= self.config.max_index_pages:
                        discovery.mark_truncation(report, "pages", limit=self.config.max_index_pages, count=stats["pages_fetched"])
                        stats["truncation_dimensions"].append("pages")
                        stop_all = True
                        break
                    if stats["requests_made"] >= self.config.max_requests_per_source_run:
                        discovery.mark_truncation(report, "requests", limit=self.config.max_requests_per_source_run, count=stats["requests_made"])
                        stats["truncation_dimensions"].append("requests")
                        stop_all = True
                        break

                    page_identity = {
                        "provider": "common_crawl",
                        "collection": self.config.index_collection,
                        "pattern": pattern,
                        "kind": "page",
                        "page": page,
                    }
                    page_body = self.cache.get(page_identity)
                    page_response: Any = _SyntheticResponse(self.request_url(pattern, "page", page), 200) if page_body is not None else None
                    if page_body is not None:
                        stats["cache_hits"] += 1
                        if len(page_body) > self.config.max_response_bytes:
                            discovery.mark_truncation(report, "response_bytes", limit=self.config.max_response_bytes, pattern=pattern, page=page)
                            stats["truncation_dimensions"].append("response_bytes")
                            stop_all = True
                            break
                        if stats["response_bytes"] + len(page_body) > self.config.max_total_response_bytes_per_source_run:
                            discovery.mark_truncation(report, "total_response_bytes", limit=self.config.max_total_response_bytes_per_source_run, pattern=pattern, page=page)
                            stats["truncation_dimensions"].append("total_response_bytes")
                            stop_all = True
                            break
                        stats["response_bytes"] += len(page_body)
                        self._record_root(report, self.request_url(pattern, "page", page), page_response, len(page_body), document_kind="ndjson")
                    else:
                        stats["request_kind"] = "page"
                        page_response, page_body, complete, reason, consumed = await self._read_response(
                            client, self._request_params(pattern, "page", page), stats
                        )
                        stats["response_bytes"] += consumed
                        if self._response_status(page_response) != 200:
                            report.fetch_errors += 1
                            stats["pages_failed"] += 1
                            self._record_root(report, self.request_url(pattern, "page", page), page_response, consumed, error=reason or "http_status")
                            report.errors.append({"target": self.request_url(pattern, "page", page), "type": "common_crawl_http_error", "status": self._response_status(page_response)})
                            report.collector.record(
                                DiscoveryOutcome.FETCH_ERROR,
                                ScopeReason.UNKNOWN,
                                discovery_method="COMMON_CRAWL",
                                root_url=self.request_url(pattern, "page", page),
                                http_status=self._response_status(page_response),
                            )
                            continue
                        self._record_root(report, self.request_url(pattern, "page", page), page_response, len(page_body), error=reason, document_kind="ndjson")
                        if not complete:
                            dimension = reason or "response_bytes"
                            discovery.mark_truncation(report, dimension, limit=(self.config.max_total_response_bytes_per_source_run if dimension == "total_response_bytes" else self.config.max_response_bytes), pattern=pattern, page=page)
                            stats["truncation_dimensions"].append(dimension)
                            stop_all = True
                            break
                        if not self.cache.set(page_identity, page_body):
                            stats["cache_write_failures"] += 1

                    stats["pages_fetched"] += 1
                    candidates: list[dict[str, Any]] = []
                    for line_number, raw_line in enumerate(page_body.splitlines(), start=1):
                        if not raw_line.strip():
                            continue
                        if stats["raw_candidates_seen"] >= self.config.max_candidates_per_source_run:
                            discovery.mark_truncation(report, "candidates", limit=self.config.max_candidates_per_source_run, count=stats["raw_candidates_seen"])
                            stats["truncation_dimensions"].append("candidates")
                            stop_all = True
                            break
                        stats["raw_candidates_seen"] += 1
                        try:
                            record = json.loads(raw_line.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                            stats["malformed_lines"] += 1
                            report.parse_errors += 1
                            report.errors.append({"target": self.request_url(pattern, "page", page), "type": "common_crawl_record_malformed", "line": line_number, "error": str(exc)})
                            report.collector.record(
                                DiscoveryOutcome.PARSE_ERROR,
                                ScopeReason.UNKNOWN,
                                discovery_method="COMMON_CRAWL",
                                root_url=self.request_url(pattern, "page", page),
                            )
                            continue
                        if not isinstance(record, dict) or not isinstance(record.get("url"), str) or not record["url"].strip():
                            stats["invalid_records"] += 1
                            report.collector.record(
                                DiscoveryOutcome.REJECTED_INVALID,
                                ScopeReason.INVALID_URL,
                                discovery_method="COMMON_CRAWL",
                                root_url=self.request_url(pattern, "page", page),
                            )
                            continue
                        candidates.append({
                            "url": record["url"],
                            "method": "COMMON_CRAWL",
                            "root_url": self.request_url(pattern, "page", page),
                        })

                    if candidates:
                        discovery.process_candidates(source, candidates, existing_before_run, report)
                    if stop_all:
                        break
                if stop_all:
                    break

            provider_metadata = {key: value for key, value in stats.items() if key != "request_kind"}
            provider_metadata.update({
                "requests": stats["requests_made"],
                "pages": stats["pages_fetched"],
                "bytes": stats["response_bytes"],
                "candidates": stats["raw_candidates_seen"],
            })
            report.metadata["common_crawl"] = provider_metadata
            report.budget_utilization = {
                "requests": stats["requests_made"] / self.config.max_requests_per_source_run,
                "pages": stats["pages_fetched"] / self.config.max_index_pages,
                "candidates": stats["raw_candidates_seen"] / self.config.max_candidates_per_source_run,
                "response_bytes": stats["response_bytes"] / self.config.max_total_response_bytes_per_source_run,
            }
            report.elapsed_seconds = time.monotonic() - started
            report.reconcile()
            if nested is not None:
                nested.commit()
            self.last_stats = dict(report.metadata["common_crawl"])
            return report
        except Exception:
            if nested is not None:
                nested.rollback()
            raise
        finally:
            if should_close and client is not None:
                await client.aclose()
            self._active_persist_queue = self.persist_queue

    def discover_source(
        self,
        source: SourceConfig,
        persist_queue: Optional[bool] = None,
    ) -> DiscoveryReport:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.discover_source_async(source, persist_queue=persist_queue))
        raise RuntimeError("discover_source() cannot run inside an active event loop; await discover_source_async() instead")

    async def audit_source_async(
        self,
        source: SourceConfig,
        client: Optional[httpx.AsyncClient] = None,
    ) -> DiscoveryReport:
        return await self.discover_source_async(source, client=client, persist_queue=False)

    def audit_source(self, source: SourceConfig) -> DiscoveryReport:
        return self.discover_source(source, persist_queue=False)


class _SingleResponseContext:
    """Async context wrapper for fake clients that only implement ``get``."""

    def __init__(self, response: Any):
        self.response = response

    async def __aenter__(self) -> Any:
        return self.response

    async def __aexit__(self, *_args: Any) -> None:
        close = getattr(self.response, "aclose", None)
        if close:
            result = close()
            if inspect.isawaitable(result):
                await result


class _SyntheticResponse:
    def __init__(self, url: str, status_code: int):
        self.url = url
        self.status_code = status_code
        self.headers: Dict[str, str] = {}


__all__ = [
    "COMMON_CRAWL_INDEX_BASE",
    "CommonCrawlCache",
    "CommonCrawlConfig",
    "CommonCrawlDiscoveryEngine",
    "CommonCrawlDiscovery",
    "CommonCrawlIndexDiscovery",
    "CommonCrawlError",
    "CommonCrawlNetworkError",
    "CommonCrawlStats",
]

# Compatibility aliases keep the provider discoverable under the concise
# names used by earlier design notes without creating a second implementation.
CommonCrawlDiscovery = CommonCrawlDiscoveryEngine
CommonCrawlIndexDiscovery = CommonCrawlDiscoveryEngine
