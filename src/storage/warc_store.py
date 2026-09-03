"""Opt-in, bounded WARC response preservation.

This module intentionally uses only the standard library.  The WARC feature
is optional and the crawler's base installation must not need ``warcio`` (or
create a WARC directory) just to run.  The records emitted here are ordinary
gzip-compressed WARC response records and can be consumed by standard WARC
readers; keeping the writer small also makes the offline safety tests
deterministic.
"""

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import gzip
import hashlib
from http import client as http_client
import os
from pathlib import Path
import threading
import uuid
from typing import Any, Dict, Mapping, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_SENSITIVE_HEADER_PARTS = (
    "auth",
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "api-key",
    "token",
    "password",
    "secret",
)
_SENSITIVE_QUERY_PARTS = (
    "auth",
    "api_key",
    "apikey",
    "credential",
    "key",
    "password",
    "secret",
    "session",
    "token",
)


@dataclass(frozen=True)
class WarcReference:
    """Small, JSON-safe reference returned after a record is selected."""

    outcome: str
    path: Optional[str] = None
    record_id: Optional[str] = None
    digest: Optional[str] = None
    captured_at: Optional[str] = None
    bytes_written: int = 0
    truncated: bool = False
    reason: Optional[str] = None
    network_status: Optional[int] = None
    url: Optional[str] = None

    def as_diagnostics(self) -> Dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


class WarcStore:
    """Write selected HTTP responses to atomically updated WARC segments."""

    def __init__(
        self,
        directory: str | Path = "data/warc",
        *,
        enabled: bool = True,
        preserve_failures: bool = True,
        success_sample_rate: float = 0.0,
        max_response_bytes: int = 10 * 1024 * 1024,
        max_total_bytes_per_run: int = 1024 * 1024 * 1024,
        max_disk_bytes: int = 10 * 1024 * 1024 * 1024,
        rotate_bytes: int = 1024 * 1024 * 1024,
        retention_mode: str = "manual",
        config: Any = None,
    ):
        if config is not None:
            directory = getattr(config, "directory", directory)
            enabled = getattr(config, "enabled", enabled)
            preserve_failures = getattr(config, "preserve_failures", preserve_failures)
            success_sample_rate = getattr(config, "success_sample_rate", success_sample_rate)
            max_response_bytes = getattr(config, "max_response_bytes", max_response_bytes)
            max_total_bytes_per_run = getattr(
                config, "max_total_bytes_per_run", max_total_bytes_per_run
            )
            max_disk_bytes = getattr(config, "max_disk_bytes", max_disk_bytes)
            rotate_bytes = getattr(config, "rotate_bytes", rotate_bytes)
            retention_mode = getattr(config, "retention_mode", retention_mode)
        self.directory = Path(directory)
        self.enabled = bool(enabled)
        self.preserve_failures = bool(preserve_failures)
        self.success_sample_rate = float(success_sample_rate)
        self.max_response_bytes = int(max_response_bytes)
        self.max_total_bytes_per_run = int(max_total_bytes_per_run)
        self.max_disk_bytes = int(max_disk_bytes)
        self.rotate_bytes = int(rotate_bytes)
        self.retention_mode = str(retention_mode)
        self._lock = threading.Lock()
        self._run_id: Optional[str] = None
        self._run_bytes = 0
        self._finalized = False
        if self.enabled:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._quarantine_partials()

    @staticmethod
    def sanitize_url(url: str) -> str:
        """Remove userinfo and redact credential-like query parameters."""
        try:
            parts = urlsplit(str(url))
            host = parts.hostname or ""
            if parts.port:
                host = f"{host}:{parts.port}"
            safe_query = []
            for key, value in parse_qsl(parts.query, keep_blank_values=True):
                key_lower = key.lower().replace("-", "_")
                if any(part in key_lower for part in _SENSITIVE_QUERY_PARTS):
                    safe_query.append((key, "[REDACTED]"))
                else:
                    safe_query.append((key, value))
            return urlunsplit((parts.scheme, host, parts.path, urlencode(safe_query), "")).replace("\r", "").replace("\n", "")
        except Exception:
            return "[INVALID_URL]"

    @staticmethod
    def _safe_header_name(value: Any) -> str:
        return (
            "-".join(str(value).split())
            .replace("\r", "")
            .replace("\n", "")
            .replace(":", "")[:100]
        )

    @staticmethod
    def _safe_header_value(value: Any) -> str:
        return " ".join(str(value).replace("\r", " ").replace("\n", " ").split())[:2000]

    @classmethod
    def redact_headers(cls, headers: Optional[Mapping[str, Any]]) -> Dict[str, str]:
        safe: Dict[str, str] = {}
        for raw_name, raw_value in (headers or {}).items():
            name = cls._safe_header_name(raw_name)
            lowered = name.lower()
            if not name or any(part in lowered for part in _SENSITIVE_HEADER_PARTS):
                continue
            safe[name] = cls._safe_header_value(raw_value)
        return safe

    def _quarantine_partials(self) -> None:
        """Keep interrupted atomic writes for manual inspection/recovery."""
        for partial in self.directory.glob("*.part"):
            quarantine = partial.with_name(
                f"{partial.name}.partial.recovered.{uuid.uuid4().hex[:8]}"
            )
            try:
                os.replace(partial, quarantine)
            except OSError:
                # A stale partial must never prevent a crawl from starting.
                continue

    def _disk_bytes(self) -> int:
        return sum(
            item.stat().st_size
            for item in self.directory.glob("segment-*.warc.gz")
            if item.is_file()
        )

    def _next_segment(self, addition: int) -> Path:
        segments = sorted(self.directory.glob("segment-*.warc.gz"))
        if segments:
            current = segments[-1]
            if current.stat().st_size + addition <= self.rotate_bytes:
                return current
        number = 1
        if segments:
            try:
                number = max(int(item.stem.split("-")[1]) for item in segments) + 1
            except (IndexError, ValueError):
                number = len(segments) + 1
        return self.directory / f"segment-{number:06d}.warc.gz"

    @staticmethod
    def _wire_body(fetch_result: Any) -> bytes:
        raw = getattr(fetch_result, "response_bytes", None)
        if raw:
            if isinstance(raw, bytes):
                return raw
            if isinstance(raw, bytearray):
                return bytes(raw)
            if isinstance(raw, str):
                return raw.encode("utf-8", errors="replace")
        body = getattr(fetch_result, "body", "") or getattr(fetch_result, "html", "")
        if isinstance(body, bytes):
            return body
        return str(body).encode("utf-8", errors="replace") if body else b""

    @staticmethod
    def _wire_status(fetch_result: Any) -> int:
        network_status = getattr(fetch_result, "network_status", None)
        return int(network_status if network_status is not None else getattr(fetch_result, "status", 500))

    def _selected(self, url: str, fetch_result: Any) -> tuple[bool, str, int]:
        logical_status = int(getattr(fetch_result, "status", 500))
        wire_status = self._wire_status(fetch_result)
        # A cached 304 has no new response body to preserve.  Its logical 200
        # body is already represented by the HTTP cache, not this capture.
        if getattr(fetch_result, "cached", False) and wire_status == 304:
            return False, "cached_not_wire_response", wire_status
        is_success = logical_status == 200 and wire_status == 200
        if is_success:
            if self.success_sample_rate <= 0:
                return False, "success_not_sampled", wire_status
            digest_value = int(hashlib.sha256(url.encode("utf-8")).hexdigest()[:16], 16)
            threshold = digest_value / float(16**16)
            return (
                threshold < self.success_sample_rate,
                "sampled_success" if threshold < self.success_sample_rate else "success_not_sampled",
                wire_status,
            )
        if self.preserve_failures:
            return True, "failure_selected", wire_status
        return False, "failure_preservation_disabled", wire_status

    @classmethod
    def _serialize_record(
        cls,
        *,
        url: str,
        status: int,
        response_headers: Mapping[str, str],
        request_headers: Mapping[str, str],
        body: bytes,
        record_id: str,
        captured_at: str,
    ) -> bytes:
        safe_url = cls.sanitize_url(url)
        safe_response_headers = cls.redact_headers(response_headers)
        safe_response_headers.pop("Content-Length", None)
        safe_response_headers.pop("content-length", None)
        safe_response_headers["Content-Length"] = str(len(body))
        reason = http_client.responses.get(status, "")
        status_line = f"HTTP/1.1 {status} {reason}\r\n".encode("ascii", errors="replace")
        http_header_bytes = b"".join(
            f"{name}: {value}\r\n".encode("utf-8", errors="replace")
            for name, value in safe_response_headers.items()
        )
        payload = status_line + http_header_bytes + b"\r\n" + body
        payload_digest = hashlib.sha1(payload).hexdigest()
        safe_request = cls.redact_headers(request_headers)
        warc_headers = [
            "WARC/1.0",
            "WARC-Type: response",
            f"WARC-Target-URI: {safe_url}",
            f"WARC-Date: {captured_at}",
            f"WARC-Record-ID: <{record_id}>",
            "Content-Type: application/http; msgtype=response",
            f"Content-Length: {len(payload)}",
            f"WARC-Payload-Digest: sha1:{payload_digest}",
        ]
        # Request headers are retained only as sanitized, bounded metadata.
        for name, value in safe_request.items():
            metadata_name = "X-Codex-Request-" + name
            warc_headers.append(f"{metadata_name}: {value}")
        raw_record = ("\r\n".join(warc_headers) + "\r\n\r\n").encode("utf-8") + payload + b"\r\n\r\n"
        return gzip.compress(raw_record, mtime=0)

    def capture(self, url: str, fetch_result: Any, run_id: Optional[str] = None) -> WarcReference:
        """Select and atomically write one response without raising to callers."""
        safe_url = self.sanitize_url(url)
        if not self.enabled:
            return WarcReference(outcome="disabled", url=safe_url)
        if run_id != self._run_id:
            self._run_id = run_id
            self._run_bytes = 0
            self._finalized = False

        selected, reason, status = self._selected(url, fetch_result)
        if not selected:
            return WarcReference(outcome="skipped", reason=reason, network_status=status, url=safe_url)
        body = self._wire_body(fetch_result)
        if not body and getattr(fetch_result, "network_status", None) is None:
            return WarcReference(outcome="skipped", reason="no_wire_response", network_status=status, url=safe_url)

        truncated = False
        remaining = self.max_total_bytes_per_run - self._run_bytes
        if remaining <= 0:
            return WarcReference(outcome="skipped", reason="run_budget_exhausted", network_status=status, url=safe_url)
        if len(body) > self.max_response_bytes:
            body = body[: self.max_response_bytes]
            truncated = True
        if len(body) > remaining:
            body = body[:remaining]
            truncated = True

        if not self._lock.acquire(blocking=False):
            return WarcReference(outcome="skipped", reason="lock_busy", network_status=status, url=safe_url)
        try:
            captured_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            record_id = f"urn:uuid:{uuid.uuid4()}"
            record = self._serialize_record(
                url=url,
                status=status,
                response_headers=getattr(fetch_result, "headers", {}) or {},
                request_headers=getattr(fetch_result, "request_headers", {}) or {},
                body=body,
                record_id=record_id,
                captured_at=captured_at,
            )
            segment = self._next_segment(len(record))
            existing = segment.read_bytes() if segment.exists() else b""
            new_size = self._disk_bytes() + len(record)
            if new_size > self.max_disk_bytes:
                return WarcReference(outcome="skipped", reason="disk_budget_exhausted", network_status=status, url=safe_url)
            temp = self.directory / f"{segment.name}.{uuid.uuid4().hex}.part"
            try:
                with open(temp, "wb") as stream:
                    stream.write(existing)
                    stream.write(record)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, segment)
            except Exception as exc:
                if temp.exists():
                    failed = temp.with_name(f"{temp.name}.partial.failed")
                    try:
                        os.replace(temp, failed)
                    except OSError:
                        pass
                return WarcReference(
                    outcome="error",
                    reason=str(exc),
                    network_status=status,
                    url=safe_url,
                )
            self._run_bytes += len(body)
            return WarcReference(
                outcome="written",
                path=str(segment.relative_to(self.directory)),
                record_id=record_id,
                digest=f"sha1:{hashlib.sha1(body).hexdigest()}",
                captured_at=captured_at,
                bytes_written=len(record),
                truncated=truncated,
                network_status=status,
                url=safe_url,
            )
        except Exception as exc:
            return WarcReference(outcome="error", reason=str(exc)[:500], network_status=status, url=safe_url)
        finally:
            self._lock.release()

    def finalize(self, run_id: Optional[str] = None) -> Dict[str, Any]:
        """Finish a run; retain any partial artifacts for manual recovery."""
        if not self.enabled:
            return {"outcome": "disabled"}
        if run_id is not None and self._run_id not in (None, run_id):
            return {"outcome": "different_run", "run_id": run_id}
        self._finalized = True
        return {
            "outcome": "finalized",
            "run_id": self._run_id,
            "run_bytes": self._run_bytes,
            "partial_files": len(list(self.directory.glob("*.part"))),
        }


# Compatibility spellings for integrations that use the acronym as an
# initialism.
WARCStore = WarcStore
WARCReference = WarcReference
