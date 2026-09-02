"""RSS/Atom parsing with a compatibility list-returning API."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import feedparser


@dataclass
class RSSParseResult:
    """Structured result for a single RSS or Atom document."""

    kind: str
    entries: List[Dict[str, Any]]
    bozo: bool = False
    malformed: bool = False
    error: Optional[str] = None

    @property
    def parsed_entries(self) -> int:
        return len(self.entries)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "entries": self.entries,
            "bozo": self.bozo,
            "malformed": self.malformed,
            "error": self.error,
            "parsed_entries": self.parsed_entries,
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)


class RSSParser:
    @staticmethod
    def _looks_like_html(content: Any) -> bool:
        if isinstance(content, bytes):
            text = content[:2048].decode("utf-8", errors="ignore")
        else:
            text = str(content or "")[:2048]
        stripped = text.lstrip().lower()
        return stripped.startswith("<!doctype html") or stripped.startswith("<html") or "<html" in stripped[:200]

    @staticmethod
    def _entry_date(entry: Any) -> Optional[datetime]:
        for date_key in ("published_parsed", "updated_parsed", "created_parsed"):
            date_struct = entry.get(date_key)
            if date_struct:
                try:
                    return datetime.fromtimestamp(
                        calendar.timegm(date_struct), tz=timezone.utc
                    )
                except Exception:
                    pass
        return None

    @classmethod
    def parse_result(cls, feed_content: Any) -> RSSParseResult:
        """Parse RSS/Atom content and retain bozo/malformed information."""
        if feed_content is None or feed_content == "" or feed_content == b"":
            return RSSParseResult("MALFORMED", [], bozo=True, malformed=True, error="empty_content")
        if cls._looks_like_html(feed_content):
            return RSSParseResult("HTML", [], malformed=False, error="html_document")

        feed = feedparser.parse(feed_content)
        version = str(feed.get("version") or "").lower()
        feed_type = str(feed.get("feed", {}).get("type") or "").lower()
        if "atom" in version or "atom" in feed_type:
            kind = "ATOM"
        elif version or feed.entries:
            kind = "RSS"
        else:
            kind = "MALFORMED"

        articles: List[Dict[str, Any]] = []
        for entry in feed.entries:
            articles.append({
                "title": entry.get("title", ""),
                "url": entry.get("link", ""),
                "published_date": cls._entry_date(entry),
                "category": entry.get("category", ""),
            })

        bozo = bool(getattr(feed, "bozo", False))
        bozo_exception = getattr(feed, "bozo_exception", None)
        error = str(bozo_exception) if bozo_exception else None
        return RSSParseResult(
            kind,
            articles,
            bozo=bozo,
            malformed=bozo and not articles,
            error=error,
        )

    @classmethod
    def parse_feed(cls, feed_content: Any) -> List[Dict[str, Any]]:
        """Compatibility wrapper returning only parsed entries."""
        return cls.parse_result(feed_content).entries


__all__ = ["RSSParseResult", "RSSParser"]
