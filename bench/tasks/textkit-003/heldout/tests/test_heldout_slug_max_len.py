from textkit import slugify


def test_max_len_keeps_whole_words():
    assert slugify("the quick brown fox", max_len=12) == "the-quick"
    assert slugify("hello world again", max_len=8) == "hello"
    assert slugify("one two", max_len=7) == "one-two"


def test_single_long_word_is_cut():
    assert slugify("extraordinary", max_len=5) == "extra"
