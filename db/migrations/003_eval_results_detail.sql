-- Everything one eval row needs to be read on its own: which split and difficulty, how the run
-- ended, and why a task was not resolved.
ALTER TABLE eval_results
    ADD COLUMN split                 text NOT NULL DEFAULT 'dev',
    ADD COLUMN difficulty            text NOT NULL DEFAULT 'simple',
    ADD COLUMN status                text NOT NULL DEFAULT 'completed',
    ADD COLUMN failure_reason        text,
    ADD COLUMN llm_calls             int NOT NULL DEFAULT 0,
    ADD COLUMN test_files_modified   bool NOT NULL DEFAULT false,
    ADD COLUMN baseline_tests_passed bool,
    ADD COLUMN heldout_exit_code     int,
    ADD COLUMN error                 text;
