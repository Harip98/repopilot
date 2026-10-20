from textkit import slugify


def test_runs_collapse_to_one_separator():
    assert slugify("Hello, World!") == "hello-world"
    assert slugify("Q&A: tips & tricks") == "q-a-tips-tricks"
    assert slugify("a -- b") == "a-b"


def test_custom_separator_runs():
    assert slugify("a,  b", sep="_") == "a_b"
