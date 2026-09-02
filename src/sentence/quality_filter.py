import re
import unicodedata
from typing import Dict, Any


def normalize_text(text: str) -> str:
    """Normalize Unicode and invisible spacing before corpus decisions."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\u00ad", "")
    text = text.replace("\u00a0", " ")
    text = "".join(" " if unicodedata.category(char) == "Zs" else char for char in text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return text.strip()


class SentenceQualityFilter:
    DEFAULT_NOISE_PATTERNS = [
        r"^photo\b", r"^larawan\b", r"^credit\b", r"^abante\s+tnt\b", r"^read\s+more\b",
        r"^click\s+here\b", r"^mag-subscribe\b", r"^i-share\b", r"^basahin\s+din\b",
        r"^sundan\s+kami\b", r"^newsletter\b", r"^advertisement\b", r"^patalastas\b",
        r"all\s+rights\s+reserved", r"copyright\b", r"^[-\u2013\u2014\u2022*]\s*",
    ]

    def __init__(
        self,
        min_tokens: int = 5,
        max_tokens: int = 80,
        min_quality_score: float = 0.0,
        noise_patterns: list[str] | None = None,
        include_headlines: bool = False,
        include_quotes: bool = False,
    ):
        self.min_tokens = min_tokens
        self.max_tokens = max_tokens
        self.min_quality_score = min_quality_score
        self.noise_patterns = list(noise_patterns) if noise_patterns is not None else list(self.DEFAULT_NOISE_PATTERNS)
        self.include_headlines = include_headlines
        self.include_quotes = include_quotes

    def evaluate(self, sentence_text: str, is_headline: bool = False, is_quote: bool = False) -> Dict[str, Any]:
        text = normalize_text(sentence_text)
        if not text:
            return {"clean": False, "reason": "empty", "text": text, "token_count": 0, "score": 0.0}

        if is_headline and not self.include_headlines:
            return {"clean": False, "reason": "headline_excluded", "text": text, "token_count": len(text.split()), "score": 0.0}

        if is_quote and not self.include_quotes:
            return {"clean": False, "reason": "quote_excluded", "text": text, "token_count": len(text.split()), "score": 0.0}

        words = text.split()
        token_count = len(words)
        if token_count < self.min_tokens:
            return {"clean": False, "reason": "too_short", "text": text, "token_count": token_count, "score": 0.0}
        if token_count > self.max_tokens:
            return {"clean": False, "reason": "too_long", "text": text, "token_count": token_count, "score": 0.0}
        if any(char in text for char in "<>{}") or "javascript:" in text.lower():
            return {"clean": False, "reason": "markup", "text": text, "token_count": token_count, "score": 0.0}
        if re.search(r"(?:https?://|www\.|\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})", text, re.IGNORECASE):
            return {"clean": False, "reason": "url_or_email", "text": text, "token_count": token_count, "score": 0.0}
        text_lower = text.lower()
        if any(re.search(pattern, text_lower) for pattern in self.noise_patterns):
            return {"clean": False, "reason": "boilerplate", "text": text, "token_count": token_count, "score": 0.0}
        if any(unicodedata.category(char) in {"Cc", "Cf"} and char not in "\n\t" for char in text):
            return {"clean": False, "reason": "control_character", "text": text, "token_count": token_count, "score": 0.0}
        alnum_chars = sum(1 for char in text if char.isalnum())
        alnum_ratio = alnum_chars / max(1, len(text))
        if alnum_ratio < 0.6:
            return {"clean": False, "reason": "symbol_noise", "text": text, "token_count": token_count, "score": 0.0}
        if not re.search(r"[.!?][\"'\u2019\u201d\u00bb)]*$", text):
            return {"clean": False, "reason": "unterminated", "text": text, "token_count": token_count, "score": 0.0}

        # Calculate quality score (1.0 default for clean text meeting heuristics)
        score = 1.0
        if score < self.min_quality_score:
            return {"clean": False, "reason": "low_quality_score", "text": text, "token_count": token_count, "score": score}

        return {"clean": True, "reason": "accepted", "text": text, "token_count": token_count, "score": score}

    def is_clean(self, sentence_text: str, is_headline: bool = False, is_quote: bool = False) -> bool:
        return bool(self.evaluate(sentence_text, is_headline=is_headline, is_quote=is_quote)["clean"])
