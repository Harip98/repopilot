-- Agent runs and everything recorded about them: graph events, tool calls,
-- human approvals, and per-task evaluation results.

CREATE TABLE runs (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id           text,                    -- benchmark task id; NULL for live GitHub runs
    repo              text NOT NULL,           -- owner/name
    issue_ref         text,                    -- e.g. "#42"
    issue_text        text NOT NULL,
    status            text NOT NULL DEFAULT 'queued' CHECK (status IN (
                          'queued', 'running', 'awaiting_approval', 'approved',
                          'rejected', 'pr_opened', 'failed', 'cancelled')),
    model             text,
    retries           int NOT NULL DEFAULT 0,
    prompt_tokens     int NOT NULL DEFAULT 0,
    completion_tokens int NOT NULL DEFAULT 0,
    cost_usd          numeric(12, 6) NOT NULL DEFAULT 0,
    pr_url            text,
    trace_id          text,
    error             text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz
);

CREATE INDEX runs_created_at_idx ON runs (created_at DESC);

CREATE TABLE run_events (
    id         bigserial PRIMARY KEY,
    run_id     uuid NOT NULL REFERENCES runs ON DELETE CASCADE,
    seq        int NOT NULL,
    node       text NOT NULL,
    kind       text NOT NULL,
    payload    jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, seq)
);

CREATE TABLE tool_calls (
    id             bigserial PRIMARY KEY,
    run_id         uuid NOT NULL REFERENCES runs ON DELETE CASCADE,
    step           int NOT NULL,
    tool           text NOT NULL,
    args           jsonb NOT NULL,
    ok             bool NOT NULL,
    denied         bool NOT NULL DEFAULT false,  -- blocked by the tool policy
    result_excerpt text,
    error          text,
    latency_ms     int NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX tool_calls_run_step_idx ON tool_calls (run_id, step);

CREATE TABLE approvals (
    id         bigserial PRIMARY KEY,
    run_id     uuid NOT NULL REFERENCES runs ON DELETE CASCADE,
    decision   text NOT NULL CHECK (decision IN ('approve', 'reject')),
    comment    text,
    decided_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX approvals_run_idx ON approvals (run_id);

CREATE TABLE eval_results (
    id                    bigserial PRIMARY KEY,
    eval_run              text NOT NULL,
    task_id               text NOT NULL,
    run_id                uuid REFERENCES runs ON DELETE SET NULL,
    model                 text NOT NULL,
    resolved              bool NOT NULL,       -- held-out tests pass
    patch_applied         bool NOT NULL,
    visible_tests_passed  bool NOT NULL,
    retries               int NOT NULL,
    tool_calls            int NOT NULL,
    tool_failures         int NOT NULL,
    unauthorized_attempts int NOT NULL,
    latency_ms            int NOT NULL,
    prompt_tokens         int NOT NULL,
    completion_tokens     int NOT NULL,
    cost_usd              numeric(12, 6) NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (eval_run, task_id)
);
