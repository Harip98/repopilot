from textkit import wrap


def test_long_words_are_broken():
    assert wrap("see https://example.com/a ok", 10) == ["see", "https://ex", "ample.com/", "a ok"]


def test_no_line_exceeds_width():
    text = "aaaaaaaaaaaaaaaaaaaaaaaaa bb ccccccccccccc d"
    for width in (1, 3, 7, 12):
        assert all(len(line) <= width for line in wrap(text, width)), width
