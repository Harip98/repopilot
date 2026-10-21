"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useRef, useState } from "react";
import { Decision } from "@/components/Decision";
import { Diff } from "@/components/Diff";
import { Status } from "@/components/Status";
import { Track, buildStops } from "@/components/Track";
import { api, ApiError, TERMINAL, type RunDetail, type RunEvent, type ToolCallRow } from "@/lib/api";

// e.g. http://127.0.0.1:16686/trace/{trace_id} for the compose Jaeger, or a Langfuse trace URL.
const TRACE_URL = process.env.NEXT_PUBLIC_TRACE_URL ?? "";

export default function RunPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [run, setRun] = useState<RunDetail | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [calls, setCalls] = useState<ToolCallRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [streamLost, setStreamLost] = useState(false);
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [detail, toolCalls] = await Promise.all([api.run(id), api.toolCalls(id)]);
      setRun(detail);
      setCalls(toolCalls);
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError && e.status === 404 ? "There is no run with this id." : (e as Error).message);
    }
  }, [id]);

  // Coalesce bursts of events into one refresh of the run and its tool calls.
  const scheduleRefresh = useCallback(() => {
    if (refreshTimer.current) clearTimeout(refreshTimer.current);
    refreshTimer.current = setTimeout(refresh, 300);
  }, [refresh]);

  useEffect(() => {
    refresh();
    const source = new EventSource(`/api/rp/runs/${id}/events`);
    const onEvent = (msg: MessageEvent) => {
      const ev = JSON.parse(msg.data) as RunEvent;
      setEvents((prev) => (prev.some((p) => p.seq === ev.seq) ? prev : [...prev, ev]));
      scheduleRefresh();
    };
    // Named SSE events ("llm.reply", "validate.report", …) need a listener each; the
    // generic handler covers unnamed ones.
    const names = ["intake", "llm", "tools", "validate", "approval", "record", "grade"].flatMap((n) =>
      ["reply", "error", "submit_plan", "finish", "report", "approve", "reject", "heldout",
        "completed", "pr_opened", "rejected", "failed"].map((k) => `${n}.${k}`));
    names.forEach((name) => source.addEventListener(name, onEvent as EventListener));
    source.onmessage = onEvent;
    source.onopen = () => setStreamLost(false);
    // EventSource retries on its own after a dropped connection, but gives up for good on an
    // HTTP error (a 502 from the proxy, say): say so instead of freezing the timeline.
    source.onerror = () => {
      if (source.readyState === EventSource.CLOSED) setStreamLost(true);
    };
    source.addEventListener("end", () => {
      source.close();
      refresh();
    });
    return () => {
      source.close();
      if (refreshTimer.current) clearTimeout(refreshTimer.current);
    };
  }, [id, refresh, scheduleRefresh]);

  if (error) return <p className="error-note" role="alert">{error} <Link href="/">Back to runs</Link></p>;
  if (!run) return <p className="muted">Loading run…</p>;

  const live = !TERMINAL.includes(run.status) && run.status !== "awaiting_approval";
  const review = run.approval_request && run.status === "awaiting_approval" ? run.approval_request : run.result;
  const stops = buildStops(events, calls, run.status);

  return (
    <article>
      <header className="run-head">
        <h1>{run.task_id ?? run.issue_ref ?? run.repo}</h1>
        <div className="meta">
          <Status status={run.status} reason={run.failure_reason} />
          <span>{run.model}</span>
          <span>{run.llm_calls} model calls, {run.tool_calls} tool calls{run.tool_failures ? ` (${run.tool_failures} failed)` : ""}</span>
          <span>{(run.prompt_tokens + run.completion_tokens).toLocaleString()} tokens</span>
          {run.unauthorized_attempts > 0 ? (
            <span className="error-note">{run.unauthorized_attempts} blocked edit{run.unauthorized_attempts > 1 ? "s" : ""} of protected files</span>
          ) : null}
          {run.pr_url ? <a href={run.pr_url}>View draft pull request</a> : null}
          {run.trace_id && TRACE_URL ? (
            <a href={TRACE_URL.replace("{trace_id}", run.trace_id)}>Open trace</a>
          ) : null}
        </div>
        {streamLost ? (
          <p className="error-note" role="alert">
            Live updates stopped (the API is not reachable). <button type="button" onClick={() => location.reload()}>Reload</button>
          </p>
        ) : null}
      </header>

      <div className="run-grid">
        <Track stops={stops} live={live} />

        <div className="evidence">
          <section aria-labelledby="issue">
            <h2 id="issue">Issue</h2>
            <p className="issue-text">{run.issue_text || "Fetching the issue from GitHub…"}</p>
          </section>

          {review?.plan ? (
            <section className="plan" aria-labelledby="plan">
              <h2 id="plan">Diagnosis</h2>
              <dl>
                <dt>Cause</dt><dd>{review.plan.root_cause}</dd>
                <dt>Files</dt><dd>{review.plan.files.map((f) => <code key={f}>{f} </code>)}</dd>
                {review.plan.acceptance_criteria.length > 0 ? (
                  <>
                    <dt>Done when</dt>
                    <dd><ul>{review.plan.acceptance_criteria.map((c) => <li key={c}>{c}</li>)}</ul></dd>
                  </>
                ) : null}
              </dl>
              {review.summary ? <p>{review.summary}</p> : null}
            </section>
          ) : null}

          {review ? (
            <section aria-labelledby="change">
              <h2 id="change">Change</h2>
              <Diff diff={review.diff} />
            </section>
          ) : null}

          {review?.tests && review.tests.exit_code != null ? (
            <section aria-labelledby="tests">
              <h2 id="tests">Test run</h2>
              <Status status={review.tests.exit_code === 0 ? "completed" : "failed"} />
              {review.tests.passed_before_change ? (
                <p className="caveat">
                  The suite also passed before this change, so green tests alone do not show the issue
                  is fixed. Check the diff against the issue.
                </p>
              ) : null}
              <pre className="output">{review.tests.output_tail || "(no output)"}</pre>
            </section>
          ) : null}

          {run.error ? (
            <section aria-labelledby="error">
              <h2 id="error">Error</h2>
              <pre className="output">{run.error}</pre>
            </section>
          ) : null}

          {run.status === "awaiting_approval" ? (
            <Decision runId={run.id} opensPr={run.task_mode === "github"} onDecided={refresh} />
          ) : null}
        </div>
      </div>
    </article>
  );
}
