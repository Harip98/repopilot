-- 'cancelled' was never reachable: there is no cancel route or command, and nothing sets it.
ALTER TABLE runs DROP CONSTRAINT runs_status_check;
ALTER TABLE runs ADD CONSTRAINT runs_status_check CHECK (status IN (
    'queued', 'running', 'awaiting_approval', 'approved', 'rejected', 'pr_opened', 'completed',
    'failed'));
