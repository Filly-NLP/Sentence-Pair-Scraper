"""Run-local HTTP evidence capture for the authorized Bandera archive trial.

Loaded only when this rollout directory is placed on PYTHONPATH for the live
archive command. It captures the archive/robots HTTP boundary without changing
the crawler implementation or enabling any additional source.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


_EVIDENCE_DIR = Path(
    os.environ.get(
        "PHASE_B_HTTP_EVIDENCE_DIR",
        r"D:\files\Online Classes\College\4th Year\random\Sentence-Pair-Scraper\data\rollout\20260902T130828251Z\http-evidence",
    )
)
_EVENTS_PATH = _EVIDENCE_DIR / "request-response.jsonl"
_ORIGINAL_ASYNC_GET = httpx.AsyncClient.get
_SEQUENCE = 0


def _safe_headers(headers: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in dict(headers or {}).items():
        if str(key).lower() in {"authorization", "cookie", "set-cookie", "proxy-authorization"}:
            result[str(key)] = "[redacted]"
        else:
            result[str(key)] = str(value)
    return result


def _append(event: dict[str, Any]) -> None:
    _EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    with _EVENTS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


async def _captured_get(self: httpx.AsyncClient, url: Any, *args: Any, **kwargs: Any) -> Any:
    global _SEQUENCE
    _SEQUENCE += 1
    sequence = _SEQUENCE
    started = time.perf_counter()
    request_headers = _safe_headers(kwargs.get("headers", {}))
    event: dict[str, Any] = {
        "sequence": sequence,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "request_url": str(url),
        "request_method": "GET",
        "request_headers": request_headers,
    }
    try:
        response = await _ORIGINAL_ASYNC_GET(self, url, *args, **kwargs)
        body = response.content or b""
        body_path = _EVIDENCE_DIR / f"response-{sequence:03d}.body"
        body_path.write_bytes(body)
        history = [
            {
                "status_code": item.status_code,
                "url": str(item.url),
                "headers": _safe_headers(item.headers),
            }
            for item in response.history
        ]
        event.update(
            {
                "outcome": "response",
                "finished_at_utc": datetime.now(timezone.utc).isoformat(),
                "elapsed_seconds": round(time.perf_counter() - started, 6),
                "status_code": response.status_code,
                "response_url": str(response.url),
                "redirect_chain": history,
                "response_headers": _safe_headers(response.headers),
                "content_type": response.headers.get("content-type"),
                "body_file": body_path.name,
                "body_size_bytes": len(body),
                "body_sha256": hashlib.sha256(body).hexdigest(),
            }
        )
        _append(event)
        return response
    except Exception as exc:
        event.update(
            {
                "outcome": "exception",
                "finished_at_utc": datetime.now(timezone.utc).isoformat(),
                "elapsed_seconds": round(time.perf_counter() - started, 6),
                "exception_type": type(exc).__name__,
                "exception": str(exc),
            }
        )
        _append(event)
        raise


httpx.AsyncClient.get = _captured_get
