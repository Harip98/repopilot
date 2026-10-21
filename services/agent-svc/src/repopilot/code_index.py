"""Hybrid code search: Postgres full-text and pgvector similarity, fused with reciprocal rank
fusion (the same retrieval approach as ind-agent, applied to source code).

ripgrep remains the agent's primary search: it is exact and fast. This index helps with
questions phrased in the issue's words ("where are discounts applied?") rather than the
code's identifiers.
"""

from dataclasses import dataclass
from pathlib import Path

import psycopg

from repopilot.llm.embeddings import Embedder

WINDOW, OVERLAP = 40, 10
MAX_FILE_BYTES = 200_000
SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".pytest_cache", ".ruff_cache"}
TEXT_SUFFIXES = {".py", ".md", ".txt", ".toml", ".yml", ".yaml", ".json", ".cfg", ".ini", ".js",
                 ".ts", ".tsx", ".go", ".rs", ".java", ".sql", ".sh", ".html", ".css"}
RRF_K = 60


@dataclass(frozen=True)
class Chunk:
    path: str
    start_line: int
    end_line: int
    content: str


@dataclass(frozen=True)
class Hit:
    path: str
    start_line: int
    end_line: int
    content: str
    score: float


def chunk_repo(root: Path) -> list[Chunk]:
    """Overlapping line windows over the repository's text files."""
    chunks: list[Chunk] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if (not path.is_file() or SKIP_DIRS.intersection(rel.parts)
                or path.suffix not in TEXT_SUFFIXES or path.stat().st_size > MAX_FILE_BYTES):
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        step = WINDOW - OVERLAP
        for start in range(0, max(len(lines), 1), step):
            window = lines[start:start + WINDOW]
            if not any(line.strip() for line in window):
                continue
            chunks.append(Chunk(rel.as_posix(), start + 1, start + len(window), "\n".join(window)))
            if start + WINDOW >= len(lines):
                break
    return chunks


def _vector(values: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in values) + "]"


def rrf(*rankings: list[int], k: int = RRF_K) -> dict[int, float]:
    """Reciprocal rank fusion over lists of ids, best first."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return scores


class CodeIndex:
    def __init__(self, dsn: str, embedder: Embedder):
        self._dsn, self._embedder = dsn, embedder

    async def index(self, key: str, root: Path) -> int:
        chunks = chunk_repo(root)
        if not chunks:
            return 0
        vectors = await self._embedder.embed([f"{c.path}\n{c.content}" for c in chunks])
        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM code_chunks WHERE index_key = %s", (key,))
                await cur.executemany(
                    "INSERT INTO code_chunks (index_key, path, start_line, end_line, content, embedding)"
                    " VALUES (%s, %s, %s, %s, %s, %s::vector)",
                    [(key, c.path, c.start_line, c.end_line, c.content, _vector(v))
                     for c, v in zip(chunks, vectors, strict=True)],
                )
            await conn.commit()
        return len(chunks)

    async def search(self, key: str, query: str, k: int = 6) -> list[Hit]:
        [qvec] = await self._embedder.embed([query])
        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            lexical = await (await conn.execute(
                "SELECT id FROM code_chunks WHERE index_key = %s"
                " AND tsv @@ websearch_to_tsquery('simple', %s)"
                " ORDER BY ts_rank_cd(tsv, websearch_to_tsquery('simple', %s)) DESC LIMIT 30",
                (key, query, query))).fetchall()
            semantic = await (await conn.execute(
                "SELECT id FROM code_chunks WHERE index_key = %s"
                " ORDER BY embedding <=> %s::vector LIMIT 30", (key, _vector(qvec)))).fetchall()
            fused = rrf([r[0] for r in lexical], [r[0] for r in semantic])
            top = sorted(fused, key=fused.get, reverse=True)[:k]
            if not top:
                return []
            rows = await (await conn.execute(
                "SELECT id, path, start_line, end_line, content FROM code_chunks WHERE id = ANY(%s)",
                (top,))).fetchall()
        by_id = {r[0]: r for r in rows}
        return [Hit(by_id[i][1], by_id[i][2], by_id[i][3], by_id[i][4], fused[i]) for i in top]

    async def drop(self, key: str) -> None:
        async with await psycopg.AsyncConnection.connect(self._dsn, autocommit=True) as conn:
            await conn.execute("DELETE FROM code_chunks WHERE index_key = %s", (key,))
