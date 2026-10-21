import pytest

from repopilot.api.sse import run_events

pytestmark = pytest.mark.anyio


class LateGradeRepo:
    """The run is already terminal; the grade event lands two polls later."""

    def __init__(self):
        self.polls = 0
        self.rows = [{"seq": 1, "node": "record", "kind": "completed", "payload": {},
                      "created_at": "t"}]

    async def events_after(self, run_id, seq):
        self.polls += 1
        if self.polls == 4:
            self.rows.append({"seq": 2, "node": "grade", "kind": "heldout", "payload": {},
                              "created_at": "t"})
        return [r for r in self.rows if r["seq"] > seq]

    async def status(self, run_id):
        return "completed"


async def test_stream_waits_for_events_written_after_the_terminal_status():
    out = [chunk async for chunk in run_events(LateGradeRepo(), "r", 0, poll_s=0.01, settle_s=0.2)]
    kinds = [c.split("\n")[1] for c in out if c.startswith("id: ")]
    assert kinds == ["event: record.completed", "event: grade.heldout"]
    assert out[-1].startswith("event: end")
