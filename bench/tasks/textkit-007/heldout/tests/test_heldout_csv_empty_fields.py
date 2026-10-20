from textkit import parse_csv_line


def test_trailing_empty_field_kept():
    assert parse_csv_line("a,b,") == ["a", "b", ""]
    assert parse_csv_line(",,") == ["", "", ""]


def test_empty_quoted_field_kept():
    assert parse_csv_line('x,""') == ["x", ""]
