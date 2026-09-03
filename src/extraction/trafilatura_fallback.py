"""Optional, body-only Trafilatura extraction fallback.

The project deliberately does not import Trafilatura at module import time.
The fallback is an opt-in recovery path for pages where the publisher-aware
extractor produced an empty or very short body; all metadata and date
selection remain owned by :mod:`src.extraction.base`.
"""

from dataclasses import dataclass
import importlib
from typing import Any, Optional


@dataclass(frozen=True)
class FallbackExtraction:
    """Bounded result returned by the optional fallback."""

    article_text: str = ""
    body_chars: int = 0
    paragraph_count: int = 0
    outcome: str = "empty"
    error: Optional[str] = None
    extractor_used: str = "primary"


def _load_trafilatura() -> Any:
    """Load the optional dependency only when a caller explicitly requests it."""

    return importlib.import_module("trafilatura")


def _extract_text(module: Any, html: str, *, favor_precision: bool) -> Any:
    """Call supported Trafilatura APIs while keeping output body-only."""

    extract = getattr(module, "extract")
    kwargs = {
        "include_comments": False,
        "include_tables": False,
        "include_images": False,
        "include_links": False,
        "favor_precision": favor_precision,
        "output_format": "txt",
    }
    try:
        return extract(html, **kwargs)
    except TypeError:
        # A small compatibility fallback also makes the integration easy to
        # exercise with a narrow fake module in offline tests.
        return extract(html)


def _normalize_body(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    lines = [" ".join(line.split()) for line in value.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def extract_body(
    html: str,
    *,
    min_body_chars: int = 200,
    favor_precision: bool = True,
) -> FallbackExtraction:
    """Extract only article text with the optional Trafilatura dependency.

    Missing dependencies and extractor errors are represented as outcomes so
    an optional diagnostic path can never make a crawl fail.  The returned
    body is accepted only when it reaches the configured minimum.
    """

    try:
        module = _load_trafilatura()
    except ImportError as exc:
        return FallbackExtraction(outcome="dependency_missing", error=str(exc))
    except Exception as exc:
        return FallbackExtraction(outcome="dependency_error", error=str(exc))

    try:
        text = _normalize_body(_extract_text(module, html or "", favor_precision=favor_precision))
    except Exception as exc:
        return FallbackExtraction(outcome="error", error=str(exc))

    body_chars = len(text)
    if not text:
        return FallbackExtraction(outcome="empty")
    if body_chars < min_body_chars:
        return FallbackExtraction(
            article_text=text,
            body_chars=body_chars,
            paragraph_count=len(text.splitlines()),
            outcome="too_short",
        )
    return FallbackExtraction(
        article_text=text,
        body_chars=body_chars,
        paragraph_count=len(text.splitlines()),
        outcome="success",
        extractor_used="trafilatura",
    )


# Small compatibility aliases for callers that describe this as a generic
# fallback extractor rather than using the body-specific function name.
extract_fallback = extract_body
extract = extract_body
