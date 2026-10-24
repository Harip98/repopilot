import hashlib
import shutil
from pathlib import Path

import pytest

from repopilot.eval.runner import REPORTS_DIR, MemoryResultStore, import_reports
from repopilot.eval.score import summarize

pytestmark = pytest.mark.anyio
SHIPPED = {"qwen3b-test": 10, "qwen7b-dev": 15, "qwen7b-dev2": 15, "qwen7b-test": 10}


def _digests(directory: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(directory.iterdir())}


async def test_every_shipped_report_imports_and_the_files_are_left_alone():
    before = _digests(REPORTS_DIR)
    store = MemoryResultStore()
    outcomes = await import_reports(store, REPORTS_DIR)
    assert {name: status for name, status, _ in outcomes} == dict.fromkeys(SHIPPED, "imported")
    for name, n in SHIPPED.items():
        results = await store.load(name)
        assert len(results) == n and {r.eval_run for r in results} == {name}
        assert (await store.get_meta(name))["model"].startswith("qwen2.5-")
    # Older dev metas predate the split and task_hash fields; they import as they are.
    assert "task_hash" not in await store.get_meta("qwen7b-dev")
    assert summarize(await store.load("qwen7b-test")).resolved == 1
    assert _digests(REPORTS_DIR) == before


async def test_import_is_idempotent_and_keeps_evals_already_stored(tmp_path: Path):
    shutil.copy(REPORTS_DIR / "qwen7b-test.jsonl", tmp_path)
    shutil.copy(REPORTS_DIR / "qwen7b-test.meta.json", tmp_path)
    store = MemoryResultStore()
    [(_, status, _)] = await import_reports(store, tmp_path)
    assert status == "imported"
    rows = dict(store.rows)
    [(_, status, detail)] = await import_reports(store, tmp_path)
    assert status == "skipped" and "--replace" in detail
    assert store.rows == rows

    first = next(iter(rows))
    store.rows[first] = store.rows[first].model_copy(update={"resolved": True})  # a local rerun
    await import_reports(store, tmp_path)
    assert store.rows[first].resolved is True  # never overwritten without --replace
    [(_, status, _)] = await import_reports(store, tmp_path, replace=True)
    assert status == "imported" and store.rows == rows


async def test_a_report_without_meta_or_with_foreign_rows(tmp_path: Path):
    shutil.copy(REPORTS_DIR / "qwen7b-dev.jsonl", tmp_path / "copy.jsonl")  # rows say qwen7b-dev
    shutil.copy(REPORTS_DIR / "qwen3b-test.jsonl", tmp_path)  # no meta file
    store = MemoryResultStore()
    outcomes = {
        name: (status, detail) for name, status, detail in await import_reports(store, tmp_path)
    }
    assert outcomes["copy"][0] == "invalid" and "qwen7b-dev" in outcomes["copy"][1]
    assert outcomes["qwen3b-test"][0] == "imported" and await store.get_meta("qwen3b-test") == {}
    assert await store.load("copy") == [] and await store.load("qwen7b-dev") == []
