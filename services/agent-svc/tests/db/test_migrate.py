from pathlib import Path

import pytest

from repopilot.db.migrate import MigrationChecksumError, _sha, check_checksums, discover


def test_discover_sorted_and_validated(tmp_path: Path):
    (tmp_path / "002_b.sql").write_text("SELECT 2;")
    (tmp_path / "001_a.sql").write_text("SELECT 1;")
    (tmp_path / "README.md").write_text("ignored")
    assert [version for version, _ in discover(tmp_path)] == ["001_a", "002_b"]

    (tmp_path / "1_bad.sql").write_text("SELECT 3;")
    with pytest.raises(ValueError):
        discover(tmp_path)


def test_edited_migration_rejected(tmp_path: Path):
    (tmp_path / "001_a.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationChecksumError):
        check_checksums(tmp_path, {"001_a": "deadbeef"})


def test_crlf_checksum_matches_lf(tmp_path: Path):
    lf, crlf = tmp_path / "lf.sql", tmp_path / "crlf.sql"
    lf.write_bytes(b"CREATE TABLE t (id int);\nSELECT 1;\n")
    crlf.write_bytes(b"CREATE TABLE t (id int);\r\nSELECT 1;\r\n")
    assert _sha(crlf) == _sha(lf)
