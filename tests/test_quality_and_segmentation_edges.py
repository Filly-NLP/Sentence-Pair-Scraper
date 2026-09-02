from src.sentence.quality_filter import SentenceQualityFilter, normalize_text
from src.sentence.segmenter import SentenceSegmenter


def test_unicode_invisible_spacing_is_normalized():
    assert normalize_text("Mabuti\u00ad  ang\u00a0balita.") == "Mabuti ang balita."
    result = SentenceQualityFilter(min_tokens=2).evaluate("Mabuti\u00ad ang\u00a0balita.")
    assert result["clean"] is True
    assert result["text"] == "Mabuti ang balita."


def test_segmenter_drops_terminal_title_fragment_and_preserves_quote():
    assert SentenceSegmenter.split_sentences("Sinabi ni Palace Press Officer Atty.") == []
    result = SentenceSegmenter.split_sentences('"Mabuti ang lagay," sabi ng opisyal.')
    assert len(result) == 1
    assert result[0]["is_quote"] is True


def test_terminal_parentheses_brackets_and_braces_are_not_quotes():
    for suffix in (")", "]", "}"):
        result = SentenceSegmenter.split_sentences(f"Mahalaga ang balitang ito.{suffix}")
        assert len(result) == 1
        assert result[0]["is_quote"] is False


def test_unterminated_remainder_requires_explicit_opt_in():
    assert SentenceSegmenter.split_sentences("Ito ay isang listahan") == []
    assert len(SentenceSegmenter.split_sentences("Ito ay isang listahan", allow_unterminated=True)) == 1


def test_punctuation_without_whitespace_is_recovered_for_uppercase_boundary():
    result = SentenceSegmenter.split_sentences("Una.Ikalawa.")
    assert [item["sentence_text"] for item in result] == ["Una.", "Ikalawa."]
