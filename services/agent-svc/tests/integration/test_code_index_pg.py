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
    assert await index.index("run-x", REPO_ROOT / "bench" / "repos" / "orders-api") == count  # idempotent
    await index.drop("run-x")
    assert await index.search("run-x", "discount") == []
