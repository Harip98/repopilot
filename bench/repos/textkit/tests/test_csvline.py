import pytest

from textkit import parse_csv_line


def test_plain_fields():
    assert parse_csv_line("a,b,c") == ["a", "b", "c"]


def test_quoted_delimiter():
    assert parse_csv_line('x,"a,b",y') == ["x", "a,b", "y"]


def test_other_delimiter():
    assert parse_csv_line("1;2;3", delim=";") == ["1", "2", "3"]


def test_unterminated_quote():
    with pytest.raises(ValueError):
        parse_csv_line('a,"b')
