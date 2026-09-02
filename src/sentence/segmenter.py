import re
from typing import List, Dict, Any

from src.sentence.quality_filter import normalize_text


class SentenceSegmenter:
    ABBREVIATIONS = {
        "dr", "mr", "ms", "mrs", "inc", "jr", "sr", "no", "etc", "vs", "prof", "gen", "rep", "sen",
        "atty", "hon", "gov", "sgt", "lt", "col", "capt", "eng", "arch", "gng", "bb", "kgg",
        "bal", "ulat", "pax", "php", "pct", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sept",
        "oct", "nov", "dec", "pnas", "a", "b", "c", "g",
    }
    TERMINAL_FRAGMENT_ABBREVIATIONS = {
        "atty", "dr", "gng", "mr", "mrs", "ms", "prof", "sen", "rep", "gov",
        "sgt", "lt", "col", "capt", "eng", "arch",
    }
    CLOSING_QUOTES = "\"'\u201d\u2019\u00bb)]}"

    @classmethod
    def _is_boundary(cls, paragraph: str, start: int, position: int) -> bool:
        char = paragraph[position]
        if char not in ".!?":
            return False
        after = paragraph[position + 1] if position + 1 < len(paragraph) else ""
        before = paragraph[position - 1] if position else ""
        if char == ".":
            if before.isdigit() and after.isdigit():
                return False
            if after == ".":
                return False
            fragment = paragraph[start : position + 1]
            if re.search(r"(?:https?://|www\.)[^\s]*$", fragment, re.IGNORECASE):
                return False
            token_match = re.search(r"([\w]+)$", paragraph[start:position])
            token = token_match.group(1).lower() if token_match else ""
            next_nonspace = position + 1
            while next_nonspace < len(paragraph) and paragraph[next_nonspace] in cls.CLOSING_QUOTES:
                next_nonspace += 1
            while next_nonspace < len(paragraph) and paragraph[next_nonspace].isspace():
                next_nonspace += 1
            if token in cls.TERMINAL_FRAGMENT_ABBREVIATIONS and next_nonspace == len(paragraph):
                return False
            if token in cls.ABBREVIATIONS and next_nonspace < len(paragraph):
                return False
            if len(token) == 1 and next_nonspace < len(paragraph) and paragraph[next_nonspace].isalpha():
                return False
        end = position + 1
        while end < len(paragraph) and paragraph[end] in cls.CLOSING_QUOTES:
            end += 1
        return end == len(paragraph) or paragraph[end].isspace() or paragraph[end].isupper()

    @classmethod
    def split_sentences(cls, text: str, allow_unterminated: bool = False) -> List[Dict[str, Any]]:
        """Split paragraphs while protecting common Filipino news abbreviations."""
        if not text:
            return []
        normalized = normalize_text(text.replace("\r\n", "\n")).replace("\n ", "\n")
        paragraphs = [p.strip() for p in normalized.split("\n") if p.strip()]
        sentences: list[dict[str, Any]] = []
        sentence_index = 0
        for paragraph_index, paragraph in enumerate(paragraphs):
            start = 0
            i = 0
            while i < len(paragraph):
                if paragraph[i] in ".!?" and cls._is_boundary(paragraph, start, i):
                    end = i + 1
                    while end < len(paragraph) and paragraph[end] in cls.CLOSING_QUOTES:
                        end += 1
                    candidate = paragraph[start:end].strip()
                    if candidate:
                        sentences.append(cls._build_sentence_meta(candidate, sentence_index, paragraph_index))
                        sentence_index += 1
                    start = end
                    while start < len(paragraph) and paragraph[start].isspace():
                        start += 1
                    i = start
                    continue
                i += 1
            remainder = paragraph[start:].strip()
            if remainder and allow_unterminated:
                sentences.append(cls._build_sentence_meta(remainder, sentence_index, paragraph_index))
                sentence_index += 1
        return sentences

    @staticmethod
    def _build_sentence_meta(sentence_text: str, idx: int, paragraph_idx: int) -> Dict[str, Any]:
        clean = sentence_text.strip()
        opening = ('"', "\u201c", "\u2018", "'")
        # Brackets/parentheses are punctuation wrappers, not quote markers.
        # They are still consumed by the scanner so ``Sentence.)`` remains a
        # single sentence, but must not cause ``is_quote`` to be true.
        closing = ('"', "\u201d", "\u2019", "\u00bb", "'")
        is_quote = clean.startswith(opening) or clean.endswith(closing)
        return {
            "sentence_text": sentence_text,
            "sentence_index": idx,
            "paragraph_index": paragraph_idx,
            "is_quote": is_quote,
        }
