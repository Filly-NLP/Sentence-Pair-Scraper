"""Pure, bounded extraction of in-scope article links from one HTML page.

This module deliberately has no HTTP or database dependency.  The pipeline
hands its immutable result to the coordinator, which is the only component
allowed to persist frontier state.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional, Tuple
from urllib.parse import parse_qsl, urljoin, urlparse

from bs4 import BeautifulSoup

from src.crawler.discovery_reporting import ScopeReason
from src.crawler.url_normalizer import normalize_url


_TRACKING_PARAMETERS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "yclid", "_hsenc", "_hsmi", "mc_cid", "mc_eid",
}


@dataclass(frozen=True)
class LinkCandidate:
    """One normalized article URL found on a parent page."""

    raw_url: str
    normalized_url: str
    depth: int
    priority: int
    link_text: Optional[str] = None
    rel: Optional[str] = None


@dataclass
class LinkDiscoveryBatch:
    """Reconciled output from analyzing one HTML document.

    For normally parsed anchors, ``unique_normalized`` reconciles as
    ``accepted_unique + rejected_unique``.  ``candidates_emitted`` may be
    smaller than ``accepted_unique`` when the candidate budget truncates the
    accepted set; ``candidates_truncated`` records that difference.  Parse
    and depth exits contain zero normalized URLs and report their work in
    ``parse_errors`` or ``links_skipped_by_depth`` instead.
    """

    candidates: Tuple[LinkCandidate, ...] = ()
    counters: dict[str, int] = field(default_factory=dict)
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    truncation_details: list[dict[str, Any]] = field(default_factory=list)

    @property
    def reconciled_counters(self) -> dict[str, int]:
        return self.counters

    @property
    def rejections(self) -> dict[str, int]:
        return self.rejection_reasons

    @property
    def accepted_count(self) -> int:
        return self.counters.get("accepted_unique", len(self.candidates))

    @property
    def rejected_count(self) -> int:
        return self.counters.get("rejected_unique", sum(self.rejection_reasons.values()))

    @property
    def truncated(self) -> bool:
        return bool(self.truncation_details)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates": [asdict(candidate) for candidate in self.candidates],
            "counters": dict(self.counters),
            "reconciled_counters": dict(self.counters),
            "rejection_reasons": dict(self.rejection_reasons),
            "truncation_details": list(self.truncation_details),
            "truncated": self.truncated,
        }


class LinkDiscoveryEngine:
    """Analyze anchors while keeping all work deterministic and bounded."""

    DEFAULT_MAX_DEPTH = 1
    DEFAULT_MAX_LINKS_PER_PAGE = 100
    DEFAULT_MAX_CANDIDATES = 500

    def __init__(
        self,
        *,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_links_per_page: int = DEFAULT_MAX_LINKS_PER_PAGE,
        max_candidates: int = DEFAULT_MAX_CANDIDATES,
        allow_query_parameters: bool = False,
    ):
        self.max_depth = max(0, int(max_depth))
        self.max_links_per_page = max(1, int(max_links_per_page))
        self.max_candidates = max(1, int(max_candidates))
        self.allow_query_parameters = bool(allow_query_parameters)

    @staticmethod
    def _counter_defaults(**overrides: int) -> dict[str, int]:
        counters = {
            "anchors_seen": 0,
            "links_considered": 0,
            "links_skipped_by_page_limit": 0,
            "links_skipped_by_depth": 0,
            "normalization_successes": 0,
            "normalization_failures": 0,
            "unique_normalized": 0,
            "duplicates": 0,
            "accepted": 0,
            "rejected": 0,
            "accepted_unique": 0,
            "rejected_unique": 0,
            "candidates_emitted": 0,
            "candidates_truncated": 0,
            "parse_errors": 0,
        }
        counters.update(overrides)
        return counters

    @staticmethod
    def _limit(explicit: Mapping[str, Any], name: str, default: Any) -> Any:
        value = explicit.get(name, default)
        return default if value is None else value

    @staticmethod
    def _record_rejection(
        rejection_reasons: Counter[str],
        counters: Counter[str],
        reason: ScopeReason | str,
    ) -> None:
        reason_value = reason.value if isinstance(reason, ScopeReason) else str(reason)
        rejection_reasons[reason_value] += 1
        counters["rejected"] += 1

    @staticmethod
    def _anchor_value(anchor: Any, attribute: str, limit: int) -> Optional[str]:
        value = anchor.get(attribute)
        if value is None:
            return None
        if isinstance(value, (list, tuple)):
            value = " ".join(str(item) for item in value)
        value = " ".join(str(value).split())
        return value[:limit] or None

    @staticmethod
    def _anchor_text(anchor: Any, limit: int) -> Optional[str]:
        value = " ".join(anchor.stripped_strings)
        value = " ".join(value.split())
        return value[:limit] or None

    @staticmethod
    def _has_disallowed_query(url: str, allow_query_parameters: bool) -> bool:
        if allow_query_parameters:
            return False
        query = urlparse(url).query
        if not query:
            return False
        pairs = parse_qsl(query, keep_blank_values=True)
        # Tracking-only query strings are intentionally normalized away by
        # the shared normalizer and do not make an otherwise valid link a
        # query trap.
        return not pairs or any(key.lower() not in _TRACKING_PARAMETERS for key, _ in pairs)

    @staticmethod
    def _resolved_base(html: str, final_url: str, requested_base: Optional[str]) -> str:
        base = requested_base or final_url
        try:
            soup = BeautifulSoup(html or "", "html.parser")
            base_tag = soup.find("base", href=True)
            if base_tag:
                base_href = str(base_tag.get("href") or "").strip()
                if base_href:
                    base = urljoin(base, base_href)
        except Exception:
            # Parsing errors are isolated by the caller; an invalid base will
            # simply cause candidate-level invalid URL decisions below.
            pass
        return base

    def analyze_html(
        self,
        html: str,
        final_url: Optional[str] = None,
        source_config: Any = None,
        parent_depth: int = 0,
        explicit_limits: Optional[Mapping[str, Any]] = None,
        *,
        requested_url: Optional[str] = None,
        limits: Optional[Mapping[str, Any]] = None,
        base_url: Optional[str] = None,
        max_depth: Optional[int] = None,
        max_links_per_page: Optional[int] = None,
        max_candidates: Optional[int] = None,
        allow_query_parameters: Optional[bool] = None,
    ) -> LinkDiscoveryBatch:
        """Return accepted article links from *html* without side effects.

        ``final_url`` is the redirect-aware response URL.  ``base_url`` is an
        optional explicit HTML resolution base and is useful for offline
        audits.  Limits may be supplied as a mapping or as keyword overrides.
        """
        if source_config is None:
            raise ValueError("source_config is required")
        final_url = final_url or base_url or ""
        explicit: dict[str, Any] = dict(limits or {})
        explicit.update(dict(explicit_limits or {}))
        if max_depth is not None:
            explicit["max_depth"] = max_depth
        if max_links_per_page is not None:
            explicit["max_links_per_page"] = max_links_per_page
        if max_candidates is not None:
            explicit["max_candidates"] = max_candidates
        if allow_query_parameters is not None:
            explicit["allow_query_parameters"] = allow_query_parameters

        depth_limit = max(0, int(self._limit(explicit, "max_depth", self.max_depth)))
        page_limit = max(1, int(self._limit(explicit, "max_links_per_page", self.max_links_per_page)))
        candidate_limit = max(1, int(self._limit(explicit, "max_candidates", self.max_candidates)))
        query_allowed = bool(self._limit(explicit, "allow_query_parameters", self.allow_query_parameters))
        parent_depth = max(0, int(parent_depth))

        counters: Counter[str] = Counter()
        rejection_reasons: Counter[str] = Counter()
        truncation_details: list[dict[str, Any]] = []
        try:
            soup = BeautifulSoup(html or "", "html.parser")
            anchors = soup.select("a[href]")
        except Exception as exc:
            return LinkDiscoveryBatch(
                counters=self._counter_defaults(parse_errors=1),
                rejection_reasons={},
                truncation_details=[{"dimension": "parse", "error": str(exc)}],
            )

        counters = Counter(self._counter_defaults(anchors_seen=len(anchors)))
        if parent_depth >= depth_limit:
            if anchors:
                truncation_details.append({
                    "dimension": "depth",
                    "limit": depth_limit,
                    "parent_depth": parent_depth,
                })
            counters["links_skipped_by_depth"] = len(anchors)
            return LinkDiscoveryBatch(
                candidates=(),
                counters=dict(counters),
                rejection_reasons={},
                truncation_details=truncation_details,
            )

        considered_anchors = anchors[:page_limit]
        counters["links_considered"] = len(considered_anchors)
        counters["links_skipped_by_page_limit"] = max(0, len(anchors) - len(considered_anchors))
        if len(anchors) > page_limit:
            truncation_details.append({
                "dimension": "links_per_page",
                "limit": page_limit,
                "observed": len(anchors),
            })

        parent_normalized_urls: set[str] = set()
        for parent_url in (requested_url, final_url):
            try:
                normalized_parent = normalize_url(str(parent_url or ""))
            except Exception:
                normalized_parent = ""
            if normalized_parent:
                parent_normalized_urls.add(normalized_parent)
        resolution_base = self._resolved_base(html or "", final_url, base_url)
        accepted: dict[str, LinkCandidate] = {}
        seen_normalized: set[str] = set()
        for anchor in considered_anchors:
            raw_url = str(anchor.get("href") or "").strip()
            if not raw_url:
                self._record_rejection(rejection_reasons, counters, ScopeReason.INVALID_URL)
                counters["normalization_failures"] += 1
                continue

            try:
                parsed_raw = urlparse(raw_url)
                if parsed_raw.scheme and parsed_raw.scheme.lower() not in {"http", "https"}:
                    self._record_rejection(rejection_reasons, counters, ScopeReason.UNSUPPORTED_SCHEME)
                    counters["normalization_failures"] += 1
                    continue
                resolved = urljoin(resolution_base, raw_url)
                resolved_parts = urlparse(resolved)
                if resolved_parts.scheme.lower() not in {"http", "https"}:
                    self._record_rejection(rejection_reasons, counters, ScopeReason.UNSUPPORTED_SCHEME)
                    counters["normalization_failures"] += 1
                    continue
                normalized = normalize_url(resolved)
            except (TypeError, ValueError):
                normalized = ""

            if not normalized:
                self._record_rejection(rejection_reasons, counters, ScopeReason.INVALID_URL)
                counters["normalization_failures"] += 1
                continue
            counters["normalization_successes"] += 1
            # Track every normalized URL, including rejected URLs, so the
            # reconciliation distinguishes unique links from repeated anchors.
            if normalized in seen_normalized:
                counters["duplicates"] += 1
                continue
            seen_normalized.add(normalized)
            counters["unique_normalized"] += 1

            if normalized in parent_normalized_urls:
                self._record_rejection(rejection_reasons, counters, ScopeReason.SELF_LINK)
                continue
            if self._has_disallowed_query(resolved, query_allowed):
                self._record_rejection(rejection_reasons, counters, ScopeReason.QUERY_PARAMETERS_DISALLOWED)
                continue

            try:
                decision = source_config.classify_url(normalized)
            except Exception:
                self._record_rejection(rejection_reasons, counters, ScopeReason.INVALID_URL)
                continue
            if not decision.accepted:
                self._record_rejection(rejection_reasons, counters, decision.reason)
                continue

            link_discovery = getattr(source_config, "link_discovery", None)
            priority = int(getattr(link_discovery, "article_priority", 100))
            candidate = LinkCandidate(
                raw_url=raw_url[:2000],
                normalized_url=normalized,
                depth=parent_depth + 1,
                priority=priority,
                link_text=self._anchor_text(anchor, 500),
                rel=self._anchor_value(anchor, "rel", 200),
            )
            # The first anchor context is retained; output ordering is still
            # sorted below by priority and normalized URL.
            previous = accepted.get(normalized)
            if previous is None or (
                candidate.raw_url,
                candidate.link_text or "",
                candidate.rel or "",
            ) < (
                previous.raw_url,
                previous.link_text or "",
                previous.rel or "",
            ):
                accepted[normalized] = candidate
            counters["accepted"] += 1

        ordered = sorted(
            accepted.values(),
            key=lambda item: (-item.priority, item.normalized_url, item.raw_url),
        )
        if len(ordered) > candidate_limit:
            truncation_details.append({
                "dimension": "candidates",
                "limit": candidate_limit,
                "observed": len(ordered),
            })
        candidates = tuple(ordered[:candidate_limit])
        counters["candidates_emitted"] = len(candidates)
        counters["accepted_unique"] = len(ordered)
        counters["candidates_truncated"] = len(ordered) - len(candidates)
        # Invalid/unsupported schemes are rejected before normalization and
        # therefore belong outside the normalized-URL conservation equation.
        counters["rejected_unique"] = max(
            0, counters["unique_normalized"] - counters["accepted_unique"]
        )
        return LinkDiscoveryBatch(
            candidates=candidates,
            counters=dict(counters),
            rejection_reasons=dict(sorted(rejection_reasons.items())),
            truncation_details=truncation_details,
        )


__all__ = ["LinkCandidate", "LinkDiscoveryBatch", "LinkDiscoveryEngine"]
