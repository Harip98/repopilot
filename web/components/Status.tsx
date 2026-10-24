import type { RunStatus } from "@/lib/api";

const LABELS: Record<RunStatus, string> = {
  queued: "Queued",
  running: "Working",
  awaiting_approval: "Waiting for review",
  approved: "Approved",
  rejected: "Rejected",
  pr_opened: "Draft PR opened",
  completed: "Completed",
  failed: "Failed",
};

const TONE: Partial<Record<RunStatus, string>> = {
  running: "live",
  queued: "live",
  awaiting_approval: "wait",
  approved: "pass",
  pr_opened: "pass",
  completed: "pass",
  rejected: "fail",
  failed: "fail",
};

export function Status({ status, reason }: { status: RunStatus; reason?: string | null }) {
  return (
    <span className={`status ${TONE[status] ?? ""}`}>
      {LABELS[status] ?? status}
      {reason ? <span className="muted">({reason.replaceAll("_", " ")})</span> : null}
    </span>
  );
}
