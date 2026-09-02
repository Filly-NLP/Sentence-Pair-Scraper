from src.language.detector import FilipinoLanguageDetector

def test_language_detection_filipino():
    detector = FilipinoLanguageDetector()
    lang, conf = detector.detect_sentence_language("Magandang umaga sa inyong lahat, sana ay masaya ang inyong araw.")
    assert lang == "FILIPINO"
    assert conf > 0.8

def test_language_detection_english():
    detector = FilipinoLanguageDetector()
    lang, conf = detector.detect_sentence_language("The quick brown fox jumps over the lazy dog.")
    assert lang == "ENGLISH"

def test_language_detection_empty():
    detector = FilipinoLanguageDetector()
    lang, conf = detector.detect_sentence_language("   ")
    assert lang == "UNKNOWN"

def test_language_acceptance_policy():
    detector = FilipinoLanguageDetector(min_confidence=0.7, accepted_languages=["FILIPINO"], allow_mixed=False)
    assert detector.is_accepted("FILIPINO", 0.9) is True
    assert detector.is_accepted("FILIPINO", 0.5) is False
    assert detector.is_accepted("ENGLISH", 0.95) is False
    assert detector.is_accepted("MIXED", 0.6) is False

    detector_mixed = FilipinoLanguageDetector(min_confidence=0.5, accepted_languages=["FILIPINO", "MIXED"], allow_mixed=True)
    assert detector_mixed.is_accepted("FILIPINO", 0.6) is True
    assert detector_mixed.is_accepted("MIXED", 0.4) is True
    assert detector_mixed.is_accepted("ENGLISH", 0.9) is False
