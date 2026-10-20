-- Outcomes and per-run metrics written by the agent's recorder.

-- 'completed': approved, with no pull request to open (benchmark and eval runs).
ALTER TABLE runs DROP CONSTRAINT runs_status_check;
ALTER TABLE runs ADD CONSTRAINT runs_status_check CHECK (status IN (
    'queued', 'running', 'awaiting_approval', 'approved', 'rejected', 'pr_opened', 'completed',
    'failed', 'cancelled'));

ALTER TABLE runs
    ADD COLUMN failure_reason        text,
    ADD COLUMN llm_calls             int NOT NULL DEFAULT 0,
    ADD COLUMN tool_calls            int NOT NULL DEFAULT 0,
    ADD COLUMN tool_failures         int NOT NULL DEFAULT 0,
    ADD COLUMN unauthorized_attempts int NOT NULL DEFAULT 0,
    ADD COLUMN sandbox_id            text,
    ADD COLUMN task_mode             text NOT NULL DEFAULT 'bench'
        CHECK (task_mode IN ('bench', 'github'));

CREATE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END;
$$;

CREATE TRIGGER runs_set_updated_at BEFORE UPDATE ON runs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX eval_results_run_id_idx ON eval_results (run_id);
