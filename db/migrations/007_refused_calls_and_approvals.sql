-- Calls the harness refused, and one reviewer decision per review round.

-- A requested call that was not run: the tool was not offered for that reply, does not exist,
-- or the budget ran out. Its row pairs the model's reply with its calls; the runs counters
-- (tool_calls, tool_failures, unauthorized_attempts) count only calls that ran.
ALTER TABLE tool_calls ADD COLUMN refused bool NOT NULL DEFAULT false;

-- 0 for the first change, 1 for the revision after a rejection with a comment. The approval
-- node re-runs on resume, so the recorder inserts with ON CONFLICT (run_id, revision) DO NOTHING.
ALTER TABLE approvals ADD COLUMN revision int NOT NULL DEFAULT 0;
ALTER TABLE approvals ADD CONSTRAINT approvals_run_revision_key UNIQUE (run_id, revision);
DROP INDEX approvals_run_idx;  -- the unique index serves run_id lookups too
