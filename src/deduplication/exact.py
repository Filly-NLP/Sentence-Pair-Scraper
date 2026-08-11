import hashlib
import re
from typing import Dict, Optional, Tuple
from simhash import Simhash

class DeduplicationEngine:
    @staticmethod
    def compute_sha256(text: str) -> str:
        """Calculate direct sha256 hash of normalized text for exact matching."""
        # Normalize whitespace and lowercase
        normalized = re.sub(r"\s+", " ", text.strip().lower())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def compute_simhash(text: str) -> str:
        """Calculate simhash fingerprint of text for near-duplicate identification."""
        # Simple token features
        tokens = re.findall(r"\b\w+\b", text.lower())
        # Return string hex notation of the 64-bit simhash fingerprint
        return f"{Simhash(tokens).value:016x}"

    @staticmethod
    def get_distance(hash1_hex: str, hash2_hex: str) -> int:
        """Calculate Hamming distance between two Simhash fingerprints (0-64)."""
        val1 = int(hash1_hex, 16)
        val2 = int(hash2_hex, 16)
        return Simhash(val1).distance(Simhash(val2))
