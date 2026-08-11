from src.sentence.segmenter import SentenceSegmenter

def test_split_simple_sentences():
    text = "Magandang umaga. Kumusta ka na ngayon? Mabuti naman!"
    sentences = SentenceSegmenter.split_sentences(text)
    assert len(sentences) == 3
    assert sentences[0]["sentence_text"] == "Magandang umaga."
    assert sentences[1]["sentence_text"] == "Kumusta ka na ngayon?"
    assert sentences[2]["sentence_text"] == "Mabuti naman!"

def test_split_with_abbreviations():
    text = "Si Dr. Santos ay dumating kahapon mula sa US. Kasama niya si Gng. Reyes."
    sentences = SentenceSegmenter.split_sentences(text)
    assert len(sentences) == 2
    assert sentences[0]["sentence_text"] == "Si Dr. Santos ay dumating kahapon mula sa US."
    assert sentences[1]["sentence_text"] == "Kasama niya si Gng. Reyes."

def test_split_with_decimal_numbers():
    text = "Ang halaga nito ay P150.50 lamang sa palengke."
    sentences = SentenceSegmenter.split_sentences(text)
    # Ensure it doesn't split at the decimal point ".50"
    assert len(sentences) == 1
    assert sentences[0]["sentence_text"] == "Ang halaga nito ay P150.50 lamang sa palengke."

def test_split_quotes():
    text = '"Huwag kayong maingay," sabi ng guro.'
    sentences = SentenceSegmenter.split_sentences(text)
    assert len(sentences) == 1
    assert sentences[0]["is_quote"] is True
