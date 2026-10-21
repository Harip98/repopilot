-- The configuration an eval started with, so a resume cannot mix models, code or budgets.
CREATE TABLE eval_runs (
    name       text PRIMARY KEY,
    meta       jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
