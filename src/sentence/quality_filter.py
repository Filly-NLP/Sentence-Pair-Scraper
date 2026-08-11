import re
from typing import Dict, Any

class SentenceQualityFilter:
    def __init__(self, min_tokens: int = 5, max_tokens: int = 80):
        self.min_tokens = min_tokens
        self.max_tokens = max_tokens
        # Common news site navigation snippets, cookie warnings, photo credits, social UI hooks
        self.noise_patterns = [
            r"^photo\b", r"^larawan\b", r"^credit\b", r"^abante\s+tnt\b", r"^read\s+more\b",
            r"^click\s+here\b", r"^mag-subscribe\b", r"^i-share\b", r"^basahin\s+din\b",
            r"^sundan\s+kami\b", r"^newsletter\b", r"^advertisement\b", r"^patalastas\b",
            r"all\s+rights\s+reserved", r"copyright\b"
        ]

    def is_clean(self, sentence_text: str) -> bool:
        """Evaluate sentence content quality checks."""
        text = sentence_text.strip()
        if not text:
            return False

        # 1. Check token (word) count bounds
        words = text.split()
        token_count = len(words)
        if token_count < self.min_tokens or token_count > self.max_tokens:
            return False

        # 2. Filter obvious HTML leftovers or code snippets
        if "<" in text or ">" in text or "{" in text or "}" in text or "javascript:" in text:
            return False

        # 3. Filter URLs or email addresses
        if "http://" in text or "https://" in text or "www." in text or "@" in text:
            return False

        # 4. Filter navigation / credit noise
        text_lower = text.lower()
        for pattern in self.noise_patterns:
            if re.search(pattern, text_lower):
                return False

        # 5. Check symbol ratio (reject strings consisting of excess non-alphanumeric noise)
        alnum_chars = sum(1 for c in text if c.isalnum())
        if alnum_chars / len(text) < 0.6:
            return False

        return True
