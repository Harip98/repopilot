import itertools
import json
from pathlib import Path

import pytest

from repopilot.code_index import WINDOW, Hit, chunk_repo, rrf
from repopilot.tools.builtin import build_registry
from repopilot.tools.policy import ToolPolicy
from repopilot.tools.registry import ToolContext
from tests.fakes import ScriptedSandbox

pytestmark = pytest.mark.anyio


def test_chunk_repo_windows_and_skips(tmp_path: Path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "big.py").write_text("\n".join(f"line {i}" for i in range(100)))
    (tmp_path / "app" / "small.py").write_text("x = 1\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG")
    chunks = chunk_repo(tmp_path)
    assert {c.path for c in chunks} == {"app/big.py", "app/small.py"}
    big = [c for c in chunks if c.path == "app/big.py"]
    assert big[0].start_line == 1 and big[0].end_line == WINDOW
    assert big[-1].end_line == 100
    assert all(b.start_line < a.end_line for a, b in itertools.pairwise(big))  # overlap


def test_rrf_rewards_agreement():
    scores = rrf([1, 2, 3], [3, 1, 4])
    assert max(scores, key=scores.get) == 1 and scores[3] > scores[2] and 4 in scores


async def test_find_code_tool():
    async def search(query: str):
        return [Hit("app/pricing.py", 10, 20, "def apply_discount(...):", 0.03)]

    registry = build_registry(find_code_tool=True)
    assert "find_code" in [s.name for s in registry.subset({"read"}).specs()]
    assert "find_code" not in build_registry().names
    ctx = ToolContext(sandbox=ScriptedSandbox(), sandbox_id="s", policy=ToolPolicy(),
                      test_command=["pytest"], code_search=search)
    out = await registry.dispatch(ctx, "find_code", json.dumps({"query": "where are discounts"}))
    assert out.ok and out.content.startswith("app/pricing.py:10-20")
    ctx.code_search = None
    out = await registry.dispatch(ctx, "find_code", json.dumps({"query": "where are discounts"}))
    assert not out.ok and "not enabled" in out.error
