"""Generate bench/tasks/textkit-NNN. Run: python bench/generate_textkit_tasks.py

Each task is an exact text replacement on the clean repo; see taskgen.py.
"""
from taskgen import write_tasks

T = {}

T["001"] = dict(
    title="slugify leaves doubled separators",
    split="dev", heldout="slug_separators",
    expected_files=["textkit/slug.py"],
    edits=[("textkit/slug.py", '_NON_ALNUM = re.compile(r"[^a-z0-9]+")\n',
            '_NON_ALNUM = re.compile(r"[^a-z0-9]")\n')],
    issue="""\
        Our article URLs got ugly after the last textkit upgrade:

            slugify("Hello, World!")  ->  "hello--world"
            slugify("Q&A: tips & tricks")  ->  "q-a--tips---tricks"

        A slug should never contain two separators in a row; any run of punctuation and
        spaces becomes a single `-`.
        """,
    test="""\
        from textkit import slugify


        def test_runs_collapse_to_one_separator():
            assert slugify("Hello, World!") == "hello-world"
            assert slugify("Q&A: tips & tricks") == "q-a-tips-tricks"
            assert slugify("a -- b") == "a-b"


        def test_custom_separator_runs():
            assert slugify("a,  b", sep="_") == "a_b"
        """,
)

T["002"] = dict(
    title="Accented letters disappear from slugs",
    split="dev", heldout="slug_accents",
    expected_files=["textkit/slug.py"],
    edits=[("textkit/slug.py",
            '    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")\n',
            '    ascii_text = text.encode("ascii", "ignore").decode("ascii")\n')],
    issue="""\
        French and German titles lose letters in their slugs:

            slugify("Crème brûlée")  ->  "crme-brle"
            slugify("Ångström")      ->  "ngstrm"

        Accented letters should become their plain ASCII letter ("creme-brulee",
        "angstrom"), as the README says.
        """,
    test="""\
        from textkit import slugify


        def test_accents_are_transliterated():
            assert slugify("Crème brûlée") == "creme-brulee"
            assert slugify("Ångström") == "angstrom"
            assert slugify("naïve café") == "naive-cafe"
        """,
)

T["003"] = dict(
    title="slugify(max_len=...) cuts words in half",
    split="dev", heldout="slug_max_len",
    expected_files=["textkit/slug.py"],
    edits=[("textkit/slug.py",
            '    kept = ""\n'
            "    for word in slug.split(sep):\n"
            '        candidate = word if not kept else f"{kept}{sep}{word}"\n'
            "        if len(candidate) > max_len:\n"
            "            break\n"
            "        kept = candidate\n"
            "    return kept or slug[:max_len]\n",
            "    return slug[:max_len].rstrip(sep)\n")],
    issue="""\
        With a length limit, slugs end in half a word:

            slugify("the quick brown fox", max_len=12)  ->  "the-quick-br"
            slugify("hello world again", max_len=8)     ->  "hello-wo"

        `max_len` is supposed to keep whole words only ("the-quick", "hello"). A single word
        that is longer than the limit on its own may still be cut.
        """,
    test="""\
        from textkit import slugify


        def test_max_len_keeps_whole_words():
            assert slugify("the quick brown fox", max_len=12) == "the-quick"
            assert slugify("hello world again", max_len=8) == "hello"
            assert slugify("one two", max_len=7) == "one-two"


        def test_single_long_word_is_cut():
            assert slugify("extraordinary", max_len=5) == "extra"
        """,
)

T["004"] = dict(
    title="truncate returns strings longer than the limit",
    split="dev", heldout="truncate_limit",
    expected_files=["textkit/wrap.py"],
    edits=[("textkit/wrap.py", "    room = limit - len(ellipsis)\n", "    room = limit\n")],
    issue="""\
        The card titles overflow: `truncate("supercalifragilistic", 10)` returns
        "supercalif…", which is 11 characters. The database column is exactly as wide as the
        limit, so inserts fail. The limit must include the ellipsis.
        """,
    test="""\
        from textkit import truncate

        CASES = [("supercalifragilistic", 10), ("abcd efgh", 4), ("hello big world", 10),
                 ("a" * 50, 1), ("one two three four", 12)]


        def test_never_longer_than_limit():
            for text, limit in CASES:
                assert len(truncate(text, limit)) <= limit, (text, limit)


        def test_exact_results():
            assert truncate("supercalifragilistic", 10) == "supercali…"
            assert truncate("abcd efgh", 4) == "abc…"
        """,
)

T["005"] = dict(
    title="wrap produces lines wider than the width",
    split="dev", heldout="wrap_long_words",
    expected_files=["textkit/wrap.py"],
    edits=[("textkit/wrap.py",
            "        while len(word) > width:\n"
            "            if line:\n"
            "                lines.append(line)\n"
            '                line = ""\n'
            "            lines.append(word[:width])\n"
            "            word = word[width:]\n", "")],
    issue="""\
        The SMS renderer wraps at 10 columns, but a long URL comes out on one 21-character line:

            wrap("see https://example.com/a ok", 10)
            -> ["see", "https://example.com/a", "ok"]

        No line may ever be longer than `width`; words that cannot fit have to be broken across
        lines (the README promises this).
        """,
    test="""\
        from textkit import wrap


        def test_long_words_are_broken():
            assert wrap("see https://example.com/a ok", 10) == ["see", "https://ex", "ample.com/", "a ok"]


        def test_no_line_exceeds_width():
            text = "aaaaaaaaaaaaaaaaaaaaaaaaa bb ccccccccccccc d"
            for width in (1, 3, 7, 12):
                assert all(len(line) <= width for line in wrap(text, width)), width
        """,
)

T["006"] = dict(
    title="Escaped quotes in CSV fields are lost",
    split="test", heldout="csv_escaped_quotes",
    expected_files=["textkit/csvline.py"],
    edits=[("textkit/csvline.py",
            "            if ch == '\"':\n"
            "                if i + 1 < len(line) and line[i + 1] == '\"':\n"
            "                    buf.append('\"')\n"
            "                    i += 1\n"
            "                else:\n"
            "                    quoted = False\n",
            "            if ch == '\"':\n"
            "                quoted = False\n")],
    issue='''\
        Product descriptions exported from the CMS contain quotes, written the standard CSV way
        as doubled quotes inside a quoted field:

            parse_csv_line('1,"say ""hi""",x')

        We get the quotes dropped instead of `['1', 'say "hi"', 'x']`.
        ''',
    test='''\
        from textkit import parse_csv_line


        def test_doubled_quote_is_literal():
            assert parse_csv_line('1,"say ""hi""",x') == ["1", 'say "hi"', "x"]
            assert parse_csv_line('"""quoted"""') == ['"quoted"']
            assert parse_csv_line('"a""b",c') == ['a"b', "c"]
        ''',
)

T["007"] = dict(
    title="Trailing empty CSV fields are dropped",
    split="test", heldout="csv_empty_fields",
    expected_files=["textkit/csvline.py"],
    edits=[("textkit/csvline.py",
            '    fields.append("".join(buf))\n    return fields\n',
            '    if buf:\n        fields.append("".join(buf))\n    return fields\n')],
    issue="""\
        Rows with an empty last column come back one field short:

            parse_csv_line("a,b,")  ->  ["a", "b"]

        so the importer shifts every column after it. Empty fields, including a trailing one
        and an empty quoted one (`x,""`), must be kept: `["a", "b", ""]`.
        """,
    test="""\
        from textkit import parse_csv_line


        def test_trailing_empty_field_kept():
            assert parse_csv_line("a,b,") == ["a", "b", ""]
            assert parse_csv_line(",,") == ["", "", ""]


        def test_empty_quoted_field_kept():
            assert parse_csv_line('x,""') == ["x", ""]
        """,
)

T["008"] = dict(
    title="camel_to_snake mangles acronyms",
    split="test", heldout="case_acronyms",
    expected_files=["textkit/case.py"],
    edits=[("textkit/case.py",
            '_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")\n',
            '_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")\n')],
    issue="""\
        Generated column names are wrong for identifiers that contain an acronym:

            camel_to_snake("HTTPServerError")   ->  "httpserver_error"
            camel_to_snake("parseXMLDocument")  ->  "parse_xmldocument"

        Expected "http_server_error" and "parse_xml_document". Plain names like "userName"
        and trailing acronyms like "userID" are fine.
        """,
    test="""\
        from textkit import camel_to_snake


        def test_acronyms_stay_together():
            assert camel_to_snake("HTTPServerError") == "http_server_error"
            assert camel_to_snake("parseXMLDocument") == "parse_xml_document"
            assert camel_to_snake("getHTTPResponseCode") == "get_http_response_code"


        def test_simple_names_unchanged():
            assert camel_to_snake("userName") == "user_name"
            assert camel_to_snake("userID") == "user_id"
        """,
)

write_tasks("textkit", T)
