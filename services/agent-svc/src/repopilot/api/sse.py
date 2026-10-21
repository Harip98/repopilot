"""Server-sent events for a run's timeline, backed by polling run_events.

Each event carries its sequence number as the SSE id, so a client that reconnects with
Last-Event-ID resumes exactly after the last event it saw.
"""

import json
import time
from collections.abc import AsyncIterator
from datetime import datetime

import anyio

from repopilot.api.repo import TERMINAL, RunsRepo


def _default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def format_event(row: dict) -> str:
    data = json.dumps({"seq": row["seq"], "node": row["node"], "kind": row["kind"],
                       "payload": row["payload"], "at": row["created_at"]}, default=_default)
    return f"id: {row['seq']}\nevent: {row['node']}.{row['kind']}\ndata: {data}\n\n"


async def run_events(repo: RunsRepo, run_id: str, after_seq: int, *, poll_s: float = 1.0,
                     keepalive_s: float = 15.0, settle_s: float = 10.0) -> AsyncIterator[str]:
    """Stream events until the run is terminal and no event has arrived for `settle_s`: some
    are written after the final status (the held-out grade, after the graph has returned)."""
    last, last_sent = after_seq, time.monotonic()
    quiet_since: float | None = None  # set once the run is terminal
    while True:
        rows = await repo.events_after(run_id, last)
        for row in rows:
            last = row["seq"]
            yield format_event(row)
        now = time.monotonic()
        if rows:
            last_sent = now
            if quiet_since is not None:
                quiet_since = now
            continue
        if quiet_since is None:
            status = await repo.status(run_id)
            if status is None or status in TERMINAL:
                quiet_since = now
        elif now - quiet_since >= settle_s:
            yield f"event: end\ndata: {json.dumps({'status': await repo.status(run_id)})}\n\n"
            return
        if now - last_sent >= keepalive_s:
            yield ": keep-alive\n\n"
            last_sent = now
        await anyio.sleep(poll_s)
