from src.deduplication.exact import DeduplicationEngine

def test_exact_hash_match():
    text1 = "  Magandang   Umaga  "
    text2 = "magandang umaga"
    assert DeduplicationEngine.compute_sha256(text1) == DeduplicationEngine.compute_sha256(text2)

def test_exact_hash_mismatch():
    text1 = "magandang umaga"
    text2 = "magandang hapon"
    assert DeduplicationEngine.compute_sha256(text1) != DeduplicationEngine.compute_sha256(text2)

def test_simhash_distance_similar():
    t1 = "Si Pangulong Marcos ay naglakbay patungong Amerika kahapon upang makipagpulong sa iba't ibang pinuno."
    t2 = "Si Pangulong Marcos ay naglakbay patungong Amerika kahapon upang makipagpulong sa iba't ibang pinuno!"
    h1 = DeduplicationEngine.compute_simhash(t1)
    h2 = DeduplicationEngine.compute_simhash(t2)
    assert DeduplicationEngine.get_distance(h1, h2) < 5

def test_simhash_distance_different():
    t1 = "Si Pangulong Marcos ay naglakbay patungong Amerika."
    t2 = "Kumain ako ng masarap na hapunan kagabi kasama ang aking pamilya sa labas."
    h1 = DeduplicationEngine.compute_simhash(t1)
    h2 = DeduplicationEngine.compute_simhash(t2)
    assert DeduplicationEngine.get_distance(h1, h2) > 10
