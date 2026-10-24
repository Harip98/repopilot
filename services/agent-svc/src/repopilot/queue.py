"""A durable job queue on Redis Streams: at-least-once delivery with a consumer group, and
entries idle past the visibility timeout reclaimed by another worker (XAUTOCLAIM)."""

import json
from dataclasses import dataclass

import redis.asyncio as redis
from redis.exceptions import ResponseError


@dataclass
class Delivery:
    id: str
    job: dict
    deliveries: int  # how many times this entry has been handed to a worker


class JobQueue:
    def __init__(self, redis_url: str, stream: str = "repopilot:jobs", group: str = "workers"):
        self.stream, self.group = stream, group
        # No socket read timeout: XREADGROUP blocks server-side for block_ms, and redis-py's
        # default (5 s) would cut every idle wait short. Connecting still times out.
        self._r = redis.from_url(
            redis_url, decode_responses=True, socket_timeout=None, socket_connect_timeout=5
        )

    async def ensure_group(self) -> None:
        try:
            await self._r.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def enqueue(self, job: dict) -> str:
        return await self._r.xadd(self.stream, {"job": json.dumps(job)})

    async def next(self, consumer: str, block_ms: int = 5000) -> Delivery | None:
        res = await self._r.xreadgroup(
            self.group, consumer, {self.stream: ">"}, count=1, block=block_ms
        )
        if not res:
            return None
        [(_, [(entry_id, fields)])] = res
        return Delivery(id=entry_id, job=json.loads(fields["job"]), deliveries=1)

    async def ack(self, entry_id: str) -> None:
        await self._r.xack(self.stream, self.group, entry_id)

    async def touch(self, consumer: str, entry_id: str) -> None:
        """Renew a lease: reset the entry's idle time without counting a new delivery."""
        await self._r.xclaim(self.stream, self.group, consumer, 0, [entry_id], justid=True)

    async def reclaim(self, consumer: str, min_idle_ms: int, count: int = 10) -> list[Delivery]:
        """Take over entries another consumer read but never acknowledged."""
        _, entries, _ = await self._r.xautoclaim(
            self.stream, self.group, consumer, min_idle_ms, start_id="0-0", count=count
        )
        out = []
        for entry_id, fields in entries:
            if fields is None:  # trimmed from the stream; nothing left to run
                await self.ack(entry_id)
                continue
            [pending] = await self._r.xpending_range(
                self.stream, self.group, min=entry_id, max=entry_id, count=1
            )
            out.append(
                Delivery(
                    id=entry_id,
                    job=json.loads(fields["job"]),
                    deliveries=pending["times_delivered"],
                )
            )
        return out

    async def drop(self) -> None:
        await self._r.delete(self.stream)

    async def close(self) -> None:
        await self._r.aclose()
