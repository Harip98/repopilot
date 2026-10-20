from textkit import slugify


def test_basic_slug():
    assert slugify("Hello World") == "hello-world"


def test_strips_outer_separators():
    assert slugify("  spaced out  ") == "spaced-out"


def test_custom_separator():
    assert slugify("a b c", sep="_") == "a_b_c"


def test_short_text_unaffected_by_max_len():
    assert slugify("short", max_len=10) == "short"
