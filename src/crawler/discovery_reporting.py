"""Stable, bounded reporting contracts for offline-auditable discovery.

The crawler historically returned a loose dictionary of counters.  The public
``DiscoveryReport`` below intentionally supports both dictionary access and
attributes so callers can adopt the structured contract without breaking the
existing CLI, pipeline, or tests.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from sqlalchemy.orm import Session

from src.storage.models import (
    CrawlRun,
    DiscoveryObservation,
    DiscoveryObservationSample,
)


class DiscoveryOutcome(str, Enum):
    """Stable outcome names used by aggregate diagnostics."""

    QUEUED = "QUEUED"
    ALREADY_STORED = "ALREADY_STORED"
    DUPLICATE_IN_RUN = "DUPLICATE_IN_RUN"
    REJECTED_SCOPE = "REJECTED_SCOPE"
    REJECTED_INVALID = "REJECTED_INVALID"
    REJECTED_DATE_HINT = "REJECTED_DATE_HINT"
    FETCH_ERROR = "FETCH_ERROR"
    PARSE_ERROR = "PARSE_ERROR"
    TRUNCATED = "TRUNCATED"


class ScopeReason(str, Enum):
    """Stable URL-scope and date-rejection reasons.

    Aliases retain the terminology used by the adaptation notes and earlier
    integrations while keeping one canonical serialized value per reason.
    """

    INVALID_URL = "invalid_url"
    UNSUPPORTED_SCHEME = "unsupported_scheme"
    HOST_MISMATCH = "host_mismatch"
    NON_ARTICLE_ASSET = "non_article_asset"
    NON_HTML_ASSET = "non_article_asset"
    NON_HTML_ASSET_UTILITY = "non_article_asset"
    SECTION_LANDING_PAGE = "section_landing_page"
    CROSS_BRAND = "cross_brand"
    SHARED_DOMAIN_CROSS_BRAND = "cross_brand"
    PATH_PATTERN_MISMATCH = "path_pattern_mismatch"
    ARTICLE_PATTERN_MISMATCH = "path_pattern_mismatch"
    SELF_LINK = "self_link"
    QUERY_PARAMETERS_DISALLOWED = "query_parameters_disallowed"
    QUERY_PARAMETERS = "query_parameters_disallowed"
    DATE_BEFORE_CUTOFF = "date_before_cutoff"
    DATE_HINT_BEFORE_CUTOFF = "date_before_cutoff"
    UNKNOWN = "unknown"
    ACCEPTED = "accepted"


@dataclass(frozen=True)
class ScopeDecision:
    """Result of classifying one candidate URL before date filtering."""

    accepted: bool
    reason: ScopeReason
    normalized_url: Optional[str] = None
    detail: Optional[str] = None

    @property
    def is_accepted(self) -> bool:
        return self.accepted

    def __bool__(self) -> bool:
        return self.accepted

    def to_dict(self) -> Dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reason": self.reason.value,
            "normalized_url": self.normalized_url,
            "detail": self.detail,
        }


@dataclass
class RootAudit:
    """Fetch, parse, and budget metadata for one feed/sitemap document."""

    configured_url: str
    origin_configured: bool = False
    robots_declared: bool = False
    requested_url: Optional[str] = None
    final_url: Optional[str] = None
    status: Optional[int] = None
    content_type: Optional[str] = None
    content_encoding: Optional[str] = None
    transferred_bytes: Optional[int] = None
    decoded_bytes: Optional[int] = None
    document_kind: Optional[str] = None
    depth: int = 0
    parsed_entries: int = 0
    accepted: int = 0
    rejected: int = 0
    lastmod_dates: int = 0
    news_dates: int = 0
    errors: List[str] = field(default_factory=list)
    truncated: List[Dict[str, Any]] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    @property
    def origin(self) -> str:
        if self.origin_configured and self.robots_declared:
            return "configured+robots"
        if self.robots_declared:
            return "robots"
        return "configured"

    @property
    def response_url(self) -> Optional[str]:
        return self.final_url

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["origin"] = self.origin
        data["response_url"] = self.final_url
        data["parsed_entries"] = self.parsed_entries
        return data


@dataclass
class _Aggregate:
    discovery_method: str
    root_url: Optional[str]
    outcome: str
    reason: Optional[str]
    http_status: Optional[int]
    observation_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    samples: List[Tuple[Optional[str], Optional[str]]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "discovery_method": self.discovery_method,
            "root_url": self.root_url,
            "outcome": self.outcome,
            "reason": self.reason,
            "http_status": self.http_status,
            "observation_count": self.observation_count,
            "metadata": dict(self.metadata),
            "samples": [
                {"candidate_url": candidate, "normalized_url": normalized}
                for candidate, normalized in self.samples
            ],
        }


class BoundedDiscoveryCollector:
    """Aggregate every observation while retaining only bounded URL samples."""

    def __init__(self, sample_cap: int = 10):
        self.sample_cap = max(0, int(sample_cap))
        self._aggregates: Dict[Tuple[str, Optional[str], str, Optional[str], Optional[int]], _Aggregate] = {}

    def record(
        self,
        outcome: DiscoveryOutcome | str,
        reason: ScopeReason | str | None = None,
        *,
        discovery_method: str = "DISCOVERY",
        root_url: Optional[str] = None,
        http_status: Optional[int] = None,
        candidate_url: Optional[str] = None,
        normalized_url: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> None:
        outcome_value = outcome.value if isinstance(outcome, Enum) else str(outcome)
        reason_value = reason.value if isinstance(reason, Enum) else (str(reason) if reason is not None else None)
        key = (str(discovery_method), root_url, outcome_value, reason_value, http_status)
        aggregate = self._aggregates.get(key)
        if aggregate is None:
            aggregate = _Aggregate(
                discovery_method=str(discovery_method),
                root_url=root_url,
                outcome=outcome_value,
                reason=reason_value,
                http_status=http_status,
            )
            self._aggregates[key] = aggregate
        aggregate.observation_count += 1
        if metadata:
            for name, value in metadata.items():
                if value is not None:
                    aggregate.metadata[name] = value
        if candidate_url is not None and len(aggregate.samples) < self.sample_cap:
            aggregate.samples.append((candidate_url, normalized_url))

    @property
    def aggregates(self) -> List[Dict[str, Any]]:
        return [aggregate.to_dict() for aggregate in self._aggregates.values()]

    @property
    def samples(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for aggregate in self._aggregates.values():
            for candidate_url, normalized_url in aggregate.samples:
                rows.append({
                    "discovery_method": aggregate.discovery_method,
                    "root_url": aggregate.root_url,
                    "outcome": aggregate.outcome,
                    "reason": aggregate.reason,
                    "http_status": aggregate.http_status,
                    "candidate_url": candidate_url,
                    "normalized_url": normalized_url,
                })
        return rows

    def __len__(self) -> int:
        return len(self._aggregates)


# Descriptive aliases for callers that used the terminology from the plan.
BoundedAggregateCollector = BoundedDiscoveryCollector
DiscoveryCollector = BoundedDiscoveryCollector


@dataclass
class DiscoveryReport:
    """Reconciled discovery result with backwards-compatible mapping access."""

    source_id: str = ""
    discovery_method: str = "ALL"
    roots: List[RootAudit] = field(default_factory=list)
    candidates_seen: int = 0
    normalization_successes: int = 0
    normalization_failures: int = 0
    invalid_or_unsupported: int = 0
    unique_within_run: int = 0
    duplicate_in_run: int = 0
    accepted_unique: int = 0
    rejected_unique: int = 0
    accepted: int = 0
    rejected: int = 0
    already_stored: int = 0
    database_duplicates: int = 0
    accepted_earlier_in_run: int = 0
    cross_method_duplicates: int = 0
    queued_new: int = 0
    actually_queued: int = 0
    would_queue: int = 0
    date_hint_rejections: int = 0
    fetch_errors: int = 0
    parse_errors: int = 0
    errors: List[Dict[str, Any]] = field(default_factory=list)
    sample_out_of_scope: List[str] = field(default_factory=list)
    truncation_details: List[Dict[str, Any]] = field(default_factory=list)
    budget_utilization: Dict[str, float] = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    collector: BoundedDiscoveryCollector = field(default_factory=BoundedDiscoveryCollector, repr=False)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def add_truncation(self, dimension: str, **details: Any) -> None:
        row = {"dimension": dimension, **details}
        self.truncation_details.append(row)

    @property
    def truncated(self) -> bool:
        return bool(self.truncation_details)

    @property
    def truncation_dimensions(self) -> List[str]:
        return [str(row.get("dimension")) for row in self.truncation_details]

    @property
    def documents_fetched(self) -> int:
        return sum(1 for root in self.roots if root.status is not None)

    @property
    def candidates_found(self) -> int:
        return self.candidates_seen

    @property
    def normalized_unique(self) -> int:
        return self.unique_within_run

    @property
    def candidates_added(self) -> int:
        return self.actually_queued

    @property
    def duplicates_skipped(self) -> int:
        return self.database_duplicates + self.duplicate_in_run

    @property
    def out_of_scope_skipped(self) -> int:
        return self.rejected_unique

    def reconcile(self) -> None:
        """Validate the three core conservation equations."""
        if self.normalized_unique != self.accepted_unique + self.rejected_unique:
            raise ValueError(
                "normalized_unique must equal accepted_unique + rejected_unique"
            )
        if self.accepted_unique != self.queued_new + self.already_stored:
            raise ValueError("accepted_unique must equal queued_new + already_stored")
        if self.candidates_seen != self.invalid_or_unsupported + self.duplicate_in_run + self.normalized_unique:
            raise ValueError(
                "candidates_seen must equal invalid_or_unsupported + duplicate_in_run + normalized_unique"
            )

    def to_dict(self) -> Dict[str, Any]:
        roots = [root.to_dict() for root in self.roots]
        data: Dict[str, Any] = {
            "source_id": self.source_id,
            "discovery_method": self.discovery_method,
            "root_audits": roots,
            # Legacy callers expect roots to be URL strings.
            "roots": [root.requested_url or root.configured_url for root in self.roots],
            "documents_fetched": self.documents_fetched,
            "candidates_seen": self.candidates_seen,
            "candidates_found": self.candidates_found,
            "normalization_successes": self.normalization_successes,
            "normalization_failures": self.normalization_failures,
            "invalid_or_unsupported": self.invalid_or_unsupported,
            "unique_within_run": self.unique_within_run,
            "normalized_unique": self.normalized_unique,
            "duplicate_in_run": self.duplicate_in_run,
            "accepted_unique": self.accepted_unique,
            "rejected_unique": self.rejected_unique,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "already_stored": self.already_stored,
            "database_duplicates": self.database_duplicates,
            "accepted_earlier_in_run": self.accepted_earlier_in_run,
            "cross_method_duplicates": self.cross_method_duplicates,
            "queued_new": self.queued_new,
            "actually_queued": self.actually_queued,
            "would_queue": self.would_queue,
            "candidates_added": self.candidates_added,
            "duplicates_skipped": self.duplicates_skipped,
            "out_of_scope_skipped": self.out_of_scope_skipped,
            "sample_out_of_scope": list(self.sample_out_of_scope),
            "date_hint_rejections": self.date_hint_rejections,
            "fetch_errors": self.fetch_errors,
            "parse_errors": self.parse_errors,
            "errors": list(self.errors),
            "truncated": self.truncated,
            "truncation_details": list(self.truncation_details),
            "truncation_dimensions": self.truncation_dimensions,
            "budget_utilization": dict(self.budget_utilization),
            "elapsed_seconds": self.elapsed_seconds,
            "aggregates": self.collector.aggregates,
            "samples": self.collector.samples,
            **self.metadata,
        }
        return data

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def keys(self):
        return self.to_dict().keys()


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


class DiscoveryDiagnosticsStore:
    """Persist bounded report aggregates and samples when explicitly enabled."""

    def __init__(
        self,
        session: Session,
        *,
        enabled: bool = False,
        persist_aggregates: bool = True,
        sample_cap: int = 10,
        retention_runs: int = 20,
    ):
        self.session = session
        self.enabled = bool(enabled)
        self.persist_aggregates = bool(persist_aggregates)
        self.sample_cap = max(0, int(sample_cap))
        self.retention_runs = max(0, int(retention_runs))

    @classmethod
    def from_config(cls, session: Session, config: Any) -> "DiscoveryDiagnosticsStore":
        def get(key: str, default: Any) -> Any:
            if hasattr(config, "get"):
                value = config.get(key, None)
                if value is not None:
                    return value
                # Plain dictionaries used by offline callers do not provide
                # the dotted-key behavior of CrawlerConfig.
                current = config
                for part in key.split("."):
                    if not isinstance(current, Mapping) or part not in current:
                        return default
                    current = current[part]
                return current
            return default

        return cls(
            session,
            enabled=bool(get("discovery.diagnostics.enabled", False)),
            persist_aggregates=bool(get(
                "discovery.diagnostics.persist_aggregates",
                get("discovery.diagnostics.aggregates.enabled", True),
            )),
            sample_cap=int(get(
                "discovery.diagnostics.sample_cap",
                get("discovery.diagnostics.sample_urls_per_reason", 10),
            )),
            retention_runs=int(get(
                "discovery.diagnostics.retention_runs",
                get("discovery.diagnostics.retention_runs_per_source", 20),
            )),
        )

    def persist(
        self,
        report: DiscoveryReport,
        *,
        crawl_id: Optional[str],
        source_id: Optional[str] = None,
        observed_at: Optional[datetime] = None,
    ) -> int:
        if not self.enabled or not self.persist_aggregates:
            return 0
        observed_at = observed_at or datetime.utcnow()
        source_id = source_id or report.source_id
        created = 0
        for aggregate in report.collector.aggregates:
            metadata_json = json.dumps(
                {key: _json_safe(value) for key, value in (aggregate.get("metadata") or {}).items()},
                sort_keys=True,
            )
            query = self.session.query(DiscoveryObservation).filter(
                DiscoveryObservation.crawl_id == crawl_id,
                DiscoveryObservation.source_id == source_id,
                DiscoveryObservation.discovery_method == aggregate["discovery_method"],
                DiscoveryObservation.outcome == aggregate["outcome"],
                DiscoveryObservation.reason == aggregate.get("reason"),
                DiscoveryObservation.http_status == aggregate.get("http_status"),
            )
            root_url = aggregate.get("root_url")
            if root_url is None:
                query = query.filter(DiscoveryObservation.root_url.is_(None))
            else:
                query = query.filter(DiscoveryObservation.root_url == root_url)
            observation = query.first()
            if observation is None:
                observation = DiscoveryObservation(
                    crawl_id=crawl_id,
                    source_id=source_id,
                    discovery_method=aggregate["discovery_method"],
                    root_url=root_url,
                    outcome=aggregate["outcome"],
                    reason=aggregate.get("reason"),
                    http_status=aggregate.get("http_status"),
                    observation_count=0,
                    metadata_json=metadata_json,
                    detail_json=metadata_json,
                    observed_at=observed_at,
                )
                self.session.add(observation)
                self.session.flush()
                created += 1
            observation.observation_count += int(aggregate["observation_count"])
            observation.metadata_json = metadata_json
            observation.detail_json = metadata_json
            current_samples = self.session.query(DiscoveryObservationSample).filter(
                DiscoveryObservationSample.observation_id == observation.observation_id
            ).count()
            for sample in aggregate.get("samples", []):
                if current_samples >= self.sample_cap:
                    break
                self.session.add(DiscoveryObservationSample(
                    observation_id=observation.observation_id,
                    candidate_url=sample.get("candidate_url"),
                    normalized_url=sample.get("normalized_url"),
                    observed_at=observed_at,
                ))
                current_samples += 1
        return created

    def retain_completed_runs(self, source_id: Optional[str] = None) -> int:
        """Delete only old completed diagnostic runs, never corpus rows."""
        if not self.enabled or self.retention_runs <= 0:
            return 0
        if source_id is None:
            source_ids = [row[0] for row in self.session.query(
                DiscoveryObservation.source_id
            ).filter(DiscoveryObservation.source_id.isnot(None)).distinct().all()]
            return sum(self.retain_completed_runs(item) for item in source_ids)

        # A pipeline discovery run can cover several sources, so use the
        # observation's source rather than CrawlRun.source_id for retention.
        query = self.session.query(CrawlRun).join(
            DiscoveryObservation,
            DiscoveryObservation.crawl_id == CrawlRun.crawl_id,
        ).filter(
            CrawlRun.end_time.isnot(None),
            DiscoveryObservation.source_id == source_id,
        ).distinct()
        completed = query.order_by(CrawlRun.end_time.desc()).all()
        old_ids = [run.crawl_id for run in completed[self.retention_runs:]]
        if not old_ids:
            return 0
        observations = self.session.query(DiscoveryObservation).filter(
            DiscoveryObservation.crawl_id.in_(old_ids),
            DiscoveryObservation.source_id == source_id,
        ).all()
        deleted = 0
        for observation in observations:
            self.session.query(DiscoveryObservationSample).filter(
                DiscoveryObservationSample.observation_id == observation.observation_id
            ).delete(synchronize_session=False)
            self.session.delete(observation)
            deleted += 1
        return deleted


def persist_discovery_diagnostics(
    session: Session,
    report: DiscoveryReport,
    *,
    crawl_id: Optional[str],
    config: Any,
) -> int:
    """Convenience wrapper used by the pipeline and offline tests."""
    return DiscoveryDiagnosticsStore.from_config(session, config).persist(
        report, crawl_id=crawl_id
    )


__all__ = [
    "BoundedAggregateCollector",
    "BoundedDiscoveryCollector",
    "DiscoveryCollector",
    "DiscoveryDiagnosticsStore",
    "DiscoveryOutcome",
    "DiscoveryReport",
    "RootAudit",
    "ScopeDecision",
    "ScopeReason",
    "persist_discovery_diagnostics",
]
