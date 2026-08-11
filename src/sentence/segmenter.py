import re
from typing import List, Dict, Any

class SentenceSegmenter:
    # Common abbreviations to prevent false splits
    ABBREVIATIONS = {
        "dr", "mr", "ms", "mrs", "inc", "jr", "sr", "no", "etc", "vs", "prof", "gen", "rep", "sen",
        "bal", "ulat", "pax", "php", "pct", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sept",
        "oct", "nov", "dec", "pnas", "gng", "bb", "kgg", "dr.", "g."
    }

    @classmethod
    def split_sentences(cls, text: str) -> List[Dict[str, Any]]:
        """Split article paragraphs into individual sentences respecting abbreviations and quotes."""
        if not text:
            return []

        # Split into paragraphs first
        paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
        sentences_list = []

        sentence_index = 0
        for p_idx, paragraph in enumerate(paragraphs):
            # Using regex to split at sentence terminators (. ! ?) followed by whitespace
            raw_splits = re.split(r"([.!?]\s+)", paragraph)
            
            current_sentence = ""
            for token in raw_splits:
                if not token:
                    continue
                
                # Check if token is a boundary marker (like ". " or "! ")
                if re.match(r"^[.!?]\s*$", token):
                    current_sentence += token.strip()
                    
                    # Verify if the preceding word is an abbreviation
                    words = re.findall(r"\b\w+\b", current_sentence)
                    if words and words[-1].lower() in cls.ABBREVIATIONS:
                        # False split: continue accumulating sentence structure
                        current_sentence += " "
                    else:
                        # Valid sentence completion
                        clean_sent = current_sentence.strip()
                        if clean_sent:
                            sentences_list.append(cls._build_sentence_meta(clean_sent, sentence_index, p_idx))
                            sentence_index += 1
                        current_sentence = ""
                else:
                    current_sentence += token

            # Catch final trailing sentence in the paragraph
            clean_sent = current_sentence.strip()
            if clean_sent:
                sentences_list.append(cls._build_sentence_meta(clean_sent, sentence_index, p_idx))
                sentence_index += 1

        return sentences_list

    @staticmethod
    def _build_sentence_meta(sentence_text: str, idx: int, paragraph_idx: int) -> Dict[str, Any]:
        # Simple heuristics for quote recognition
        is_quote = False
        # If it starts or ends with quote marks, or has quote marks around most of it
        clean = sentence_text.strip()
        quote_chars = ('"', '“', '”', "'", '‘', '’')
        if any(clean.startswith(q) for q in quote_chars) or any(clean.endswith(q) for q in quote_chars) or '"' in clean or '“' in clean or '’' in clean:
            is_quote = True

        return {
            "sentence_text": sentence_text,
            "sentence_index": idx,
            "paragraph_index": paragraph_idx,
            "is_quote": is_quote
        }
