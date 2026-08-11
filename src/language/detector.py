from lingua import Language, LanguageDetectorBuilder
from typing import Tuple, Dict

class FilipinoLanguageDetector:
    def __init__(self, min_confidence: float = 0.7):
        self.min_confidence = min_confidence
        # Restrict classification target set to English and Tagalog to boost performance
        self.detector = (
            LanguageDetectorBuilder.from_languages(Language.TAGALOG, Language.ENGLISH)
            .with_low_accuracy_mode() # Fast and highly optimized for short strings
            .build()
        )

    def detect_sentence_language(self, sentence: str) -> Tuple[str, float]:
        """Detect language of a single sentence, returning class name and confidence score."""
        if not sentence or not sentence.strip():
            return "UNKNOWN", 0.0

        clean_text = sentence.strip()
        
        try:
            # Lingua returning detection targets
            result = self.detector.detect_language_of(clean_text)
            if not result:
                return "UNKNOWN", 0.0

            # Estimate confidence rating
            conf_values = self.detector.compute_language_confidence_values(clean_text)
            confidence = 0.0
            for lang_conf in conf_values:
                if lang_conf.language == result:
                    confidence = lang_conf.value
                    break

            lang_label = "OTHER"
            if result == Language.TAGALOG:
                lang_label = "FILIPINO"
            elif result == Language.ENGLISH:
                lang_label = "ENGLISH"

            # Detect Taglish (code-switching)
            if lang_label == "FILIPINO" and confidence < self.min_confidence:
                # If Tagalog is detected but with a weak score, we label as MIXED
                lang_label = "MIXED"

            return lang_label, confidence
        except Exception:
            return "UNKNOWN", 0.0
