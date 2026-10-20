from textkit import parse_csv_line


def test_doubled_quote_is_literal():
    assert parse_csv_line('1,"say ""hi""",x') == ["1", 'say "hi"', "x"]
    assert parse_csv_line('"""quoted"""') == ['"quoted"']
    assert parse_csv_line('"a""b",c') == ['a"b', "c"]
