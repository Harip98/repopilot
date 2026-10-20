from textkit import truncate

CASES = [("supercalifragilistic", 10), ("abcd efgh", 4), ("hello big world", 10),
         ("a" * 50, 1), ("one two three four", 12)]


def test_never_longer_than_limit():
    for text, limit in CASES:
        assert len(truncate(text, limit)) <= limit, (text, limit)


def test_exact_results():
    assert truncate("supercalifragilistic", 10) == "supercali…"
    assert truncate("abcd efgh", 4) == "abc…"
