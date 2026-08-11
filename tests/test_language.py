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
