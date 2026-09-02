from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import re
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class DateEvidence:
    value: datetime
    source: str  # "html_jsonld", "html_meta", "html_selector", "news_sitemap", "rss", "url", "archive_period", "lastmod"
    confidence: float
    raw: Optional[str] = None
    rank: int = 100


class DateFilter:
    # Deterministic rank order (1 is most trustworthy, 6 is least)
    RANK_ORDER = {
        "html_jsonld": (1, 1.0),
        "html_structured": (1, 1.0),
        "html_meta": (1, 0.95),
        "html_selector": (1, 0.95),
        "news_sitemap": (2, 0.90),
        "rss": (3, 0.85),
        "url": (4, 0.75),
        "archive_period": (5, 0.70),
        "lastmod": (6, 0.50),
    }

    def __init__(self, cutoff_date: datetime):
        self.cutoff_date = cutoff_date

    @staticmethod
    def _utc_naive(value: datetime) -> datetime:
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def parse_date(self, date_str: str) -> Optional[datetime]:
        """Parse ISO, RFC 2822, and common publisher date representations."""
        if not date_str:
            return None
        value = str(date_str).strip()
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed
        except ValueError:
            pass
        try:
            parsed = parsedate_to_datetime(value)
            if parsed:
                return parsed
        except (TypeError, ValueError, IndexError):
            pass
        for fmt in (
            "%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d", "%B %d, %Y",
            "%b %d, %Y", "%d %B %Y", "%d %b %Y", "%B %d, %Y %I:%M %p",
            "%b %d, %Y %I:%M %p", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S",
        ):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
        return None

    def validation_reason(self, pub_date: Optional[datetime], max_future_days: int = 1) -> str:
        """Return a stable reason suitable for metrics and URL status fields."""
        if pub_date is None:
            return "date_missing"
        utc_val = self._utc_naive(pub_date)
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        if utc_val > now_utc + timedelta(days=max_future_days):
            return "date_in_future"
        if utc_val < self._utc_naive(self.cutoff_date):
            return "date_before_cutoff"
        return "accepted"

    def validate_raw(self, date_str: str | None) -> tuple[Optional[datetime], str]:
        """Parse a raw value and distinguish missing from malformed metadata."""
        if not date_str or not str(date_str).strip():
            return None, "date_missing"
        parsed = self.parse_date(str(date_str))
        if parsed is None:
            return None, "date_invalid"
        return parsed, self.validation_reason(parsed)

    @staticmethod
    def parse_date_from_url(url: str) -> Optional[datetime]:
        """Extract a conservative YYYY/MM/DD (or YYYY-MM-DD) path date."""
        match = re.search(r"(?:^|/)(20\d{2})[-/](0?[1-9]|1[0-2])[-/](0?[1-9]|[12]\d|3[01])(?:/|$)", url or "")
        if not match:
            return None
        try:
            return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None

    def resolve_publication_evidence(
        self,
        candidates: List[DateEvidence],
        allow_lastmod_as_publication: bool = False,
        disagreement_threshold_days: int = 30,
    ) -> Tuple[Optional[datetime], str, str, float, Optional[Dict[str, Any]]]:
        """Resolve publication date through deterministic evidence ranking.

        Returns (resolved_date, reason, chosen_source, confidence, diagnostics).
        """
        if not candidates:
            return None, "date_missing", "missing", 0.0, None

        # Filter and rank evidence
        ranked_candidates: List[Tuple[int, DateEvidence]] = []
        for c in candidates:
            if c.source == "lastmod" and not allow_lastmod_as_publication:
                continue
            rank_tuple = self.RANK_ORDER.get(c.source, (99, 0.5))
            rank = rank_tuple[0]
            conf = c.confidence or rank_tuple[1]
            c.rank = rank
            c.confidence = conf
            ranked_candidates.append((rank, c))

        ranked_candidates.sort(key=lambda x: (x[0], -x[1].confidence))

        if not ranked_candidates:
            return None, "date_missing", "missing", 0.0, None

        # Check for material disagreement among parsed dates
        disagreements: List[Dict[str, Any]] = []
        valid_pairs: List[Tuple[str, datetime]] = []
        for _, cand in ranked_candidates:
            if cand.value:
                valid_pairs.append((cand.source, self._utc_naive(cand.value)))

        if len(valid_pairs) >= 2:
            base_src, base_dt = valid_pairs[0]
            for other_src, other_dt in valid_pairs[1:]:
                diff_days = abs((base_dt - other_dt).days)
                if diff_days > disagreement_threshold_days:
                    disagreements.append({
                        "primary_source": base_src,
                        "other_source": other_src,
                        "days_difference": diff_days,
                    })

        diagnostics: Dict[str, Any] = {
            "candidate_count": len(ranked_candidates),
            "disagreements": disagreements,
        }

        # Select highest-ranked candidate that satisfies date constraints
        last_reason = "date_missing"
        last_source = "missing"
        for _, cand in ranked_candidates:
            val = cand.value
            reason = self.validation_reason(val)
            if reason == "accepted":
                return self._utc_naive(val), "accepted", cand.source, cand.confidence, diagnostics
            last_reason = reason
            last_source = cand.source

        return None, last_reason, last_source, 0.0, diagnostics

    def is_valid(self, pub_date: Optional[datetime]) -> bool:
        return self.validation_reason(pub_date) == "accepted"
