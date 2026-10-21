// Types and calls for the RepoPilot API, reached through this app's /api/rp proxy.

export type RunStatus =
  | "queued" | "running" | "awaiting_approval" | "approved" | "rejected"
  | "pr_opened" | "completed" | "failed" | "cancelled";

export interface Plan {
  root_cause: string;
  files: string[];
  acceptance_criteria: string[];
  test_targets: string[];
}

export interface ReviewPayload {
  run_id: string;
  diff: string;
  summary: string | null;
  plan: Plan | null;
  tests: { exit_code: number | null; output_tail: string; passed_before_change: boolean | null };
}

export interface Run {
  id: string;
  task_id: string | null;
  repo: string;
  issue_ref: string | null;
  status: RunStatus;
  failure_reason: string | null;
  model: string | null;
  retries: number;
  llm_calls: number;
  tool_calls: number;
  tool_failures: number;
  unauthorized_attempts: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: number;
  pr_url: string | null;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
}

export interface RunDetail extends Run {
  issue_text: string;
  trace_id: string | null;
  task_mode: "bench" | "github";
  error: string | null;
  approval_request: ReviewPayload | null;
  result: ReviewPayload | null;
}

export interface RunEvent {
  seq: number;
  node: string;
  kind: string;
  payload: Record<string, unknown>;
  at: string;
}

export interface ToolCallRow {
  step: number;
  tool: string;
  args: Record<string, unknown>;
  ok: boolean;
  denied: boolean;
  error: string | null;
  result_excerpt: string | null;
  latency_ms: number;
}

export interface EvalListing { name: string; model: string; tasks: number; resolved: number; updated_at: string }

export interface EvalSummary {
  n: number; resolved: number; errors: number;
  resolution_rate: number | null; resolution_ci: [number | null, number | null];
  patch_applied_rate: number | null; visible_pass_rate: number | null; baseline_pass_rate: number | null;
  tool_calls: number; tool_failure_rate: number | null; avg_retries: number | null;
  p50_latency_s: number | null; p95_latency_s: number | null;
  prompt_tokens: number; completion_tokens: number; tokens_per_resolved: number | null;
  unauthorized_attempts: number; runs_with_test_edits: number;
  failure_reasons: Record<string, number>;
  by_difficulty: Record<string, EvalSummary>;
}

export interface EvalRow {
  task_id: string; difficulty: string; split: string; status: string; failure_reason: string | null;
  resolved: boolean; retries: number; llm_calls: number; tool_calls: number; latency_ms: number;
  prompt_tokens: number; completion_tokens: number; error: string | null;
}

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`/api/rp/${path}`, { cache: "no-store", ...init });
  const body = await resp.json().catch(() => null);
  if (!resp.ok) {
    const err = body?.error ?? {};
    throw new ApiError(resp.status, err.code ?? "http_error", err.message ?? resp.statusText);
  }
  return body as T;
}

const post = (body: unknown): RequestInit => ({
  method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify(body),
});

export const api = {
  runs: () => call<{ runs: Run[] }>("runs?limit=100").then((r) => r.runs),
  run: (id: string) => call<RunDetail>(`runs/${id}`),
  toolCalls: (id: string) => call<{ tool_calls: ToolCallRow[] }>(`runs/${id}/tool-calls`).then((r) => r.tool_calls),
  startBench: (taskId: string, autoApprove: boolean) =>
    call<{ run_id: string }>("runs", post({ task_id: taskId, auto_approve: autoApprove })),
  startGithub: (repo: string, issue: number) =>
    call<{ run_id: string }>("runs", post({ repo, issue_number: issue })),
  decide: (id: string, decision: "approve" | "reject", comment?: string) =>
    call<{ status: string }>(`runs/${id}/approval`, post({ decision, comment: comment || null })),
  evals: () => call<{ evals: EvalListing[] }>("evals").then((r) => r.evals),
  evalDetail: (name: string) =>
    call<{ name: string; summary: EvalSummary; results: EvalRow[] }>(`evals/${encodeURIComponent(name)}`),
};

export const TERMINAL: RunStatus[] = ["completed", "pr_opened", "rejected", "failed", "cancelled"];

export function pct(v: number | null | undefined): string {
  return v == null ? "n/a" : `${(v * 100).toFixed(1)}%`;
}

export function ago(iso: string): string {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.round(s)} s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString();
}
