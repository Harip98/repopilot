import pytest

from repopilot.code_index import CodeIndex
from repopilot.config import REPO_ROOT
from repopilot.llm.embeddings import FakeEmbedder

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def test_hybrid_search_finds_pricing(settings, conn):
    index = CodeIndex(settings.dsn(), FakeEmbedder())
    count = await index.index("run-x", REPO_ROOT / "bench" / "repos" / "orders-api")
    assert count > 5
    hits = await index.search("run-x", "discount percentage applied before tax")
    # README.md legitimately ranks high ("Tax is applied after discounts"); with the hashing
    # fake embedder scores are close, so assert the pricing code is surfaced, not its rank.
    assert "app/pricing.py" in [h.path for h in hits]
    assert (
        await index.index("run-x", REPO_ROOT / "bench" / "repos" / "orders-api") == count
    )  # idempotent
    assert await index.exists("run-x") and not await index.exists("run-y")
    await index.drop("run-x")
    assert await index.search("run-x", "discount") == []
    assert not await index.exists("run-x")


async def test_run_index_build_search_drop(settings, conn):
    run = CodeIndex(settings.dsn(), FakeEmbedder()).for_run("run-z")
    await run.build(REPO_ROOT / "bench" / "repos" / "orders-api")
    assert "app/pricing.py" in [h.path for h in await run.search("discount percentage before tax")]
    await run.drop()
    assert await run.search("discount") == []
