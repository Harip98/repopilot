import pytest

from textkit import truncate, wrap


def test_truncate_leaves_short_text():
    assert truncate("short", 10) == "short"


def test_truncate_cuts_at_word_boundary():
    assert truncate("hello big world", 10) == "hello big…"


def test_wrap_greedy():
    assert wrap("the quick brown fox", 10) == ["the quick", "brown fox"]


def test_wrap_rejects_zero_width():
    with pytest.raises(ValueError):
        wrap("x", 0)
