"use client";

import { use, useEffect, useState } from "react";
import { api, pct, type EvalRow, type EvalSummary } from "@/lib/api";

function fmt(v: number | null | undefined, digits = 0): string {
  return v == null ? "n/a" : v.toLocaleString(undefined, { maximumFractionDigits: digits });
}

function Interval({ rate, ci }: { rate: number | null; ci: [number | null, number | null] }) {
  if (rate == null || ci[0] == null || ci[1] == null) return null;
  return (
    <div className="ci-bar" role="img"
         aria-label={`Resolution rate ${pct(rate)}, 95% interval ${pct(ci[0])} to ${pct(ci[1])}`}>
      <span className="span" style={{ left: `${ci[0] * 100}%`, width: `${(ci[1] - ci[0]) * 100}%` }} />
      <span className="point" style={{ left: `calc(${rate * 100}% - 1px)` }} />
    </div>
  );
}

export default function EvalPage({ params }: { params: Promise<{ name: string }> }) {
  const { name } = use(params);
  const [data, setData] = useState<{ summary: EvalSummary; results: EvalRow[] } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.evalDetail(decodeURIComponent(name)).then(setData).catch((e: Error) => setError(e.message));
  }, [name]);

  if (error) return <p className="error-note" role="alert">{error}</p>;
  if (!data) return <p className="muted">Loading…</p>;
  const s = data.summary;

  return (
    <>
      <h1>{decodeURIComponent(name)}</h1>
      <div className="headline">
        <span className="rate">{pct(s.resolution_rate)}</span>
        <span>
          of {s.n} tasks resolved ({s.resolved}), 95% interval {pct(s.resolution_ci[0])}–{pct(s.resolution_ci[1])}
        </span>
        <Interval rate={s.resolution_rate} ci={s.resolution_ci} />
      </div>

      <dl className="facts">
        <div><dt>Produced a change</dt><dd>{pct(s.patch_applied_rate)}</dd></div>
        <div><dt>Passed its own validation</dt><dd>{pct(s.visible_pass_rate)}</dd></div>
        <div><dt>Tool calls that failed</dt><dd>{pct(s.tool_failure_rate)} of {fmt(s.tool_calls)}</dd></div>
        <div><dt>Retries per task</dt><dd>{fmt(s.avg_retries, 2)}</dd></div>
        <div><dt>Time per task, median / p95</dt><dd>{fmt(s.p50_latency_s)} s / {fmt(s.p95_latency_s)} s</dd></div>
        <div><dt>Tokens per resolved task</dt><dd>{fmt(s.tokens_per_resolved)}</dd></div>
        <div><dt>Blocked edits of protected files</dt><dd>{s.unauthorized_attempts}</dd></div>
        <div><dt>Runs that edited existing tests</dt><dd>{s.runs_with_test_edits}</dd></div>
      </dl>

      {Object.keys(s.failure_reasons).length > 0 ? (
        <section style={{ marginBottom: "2.5rem" }}>
          <h2>Why tasks were not resolved</h2>
          <ul>
            {Object.entries(s.failure_reasons).map(([reason, n]) => (
              <li key={reason}>{reason.replaceAll("_", " ")}: {n}</li>
            ))}
          </ul>
        </section>
      ) : null}

      <h2>Tasks</h2>
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Task</th>
              <th scope="col">Difficulty</th>
              <th scope="col">Resolved</th>
              <th scope="col">Outcome</th>
              <th scope="col" className="num">Model / tool calls</th>
              <th scope="col" className="num">Tokens</th>
              <th scope="col" className="num">Time</th>
            </tr>
          </thead>
          <tbody>
            {data.results.map((r) => (
              <tr key={r.task_id}>
                <td><code>{r.task_id}</code></td>
                <td>{r.difficulty}</td>
                <td>
                  <span className={`status ${r.resolved ? "pass" : "fail"}`}>{r.resolved ? "Yes" : "No"}</span>
                </td>
                <td className="muted">{(r.failure_reason ?? r.error ?? r.status).replaceAll("_", " ")}</td>
                <td className="num">{r.llm_calls} / {r.tool_calls}</td>
                <td className="num">{(r.prompt_tokens + r.completion_tokens).toLocaleString()}</td>
                <td className="num">{Math.round(r.latency_ms / 1000)} s</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
