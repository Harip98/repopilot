-- What the API shows for a run without reading LangGraph checkpoints:
-- the pending review (diff, tests, plan, summary) and the final result.
ALTER TABLE runs
    ADD COLUMN approval_request jsonb,
    ADD COLUMN result           jsonb;
