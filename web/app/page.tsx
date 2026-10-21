"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { NewRun } from "@/components/NewRun";
import { Status } from "@/components/Status";
import { ago, api, TERMINAL, type Run } from "@/lib/api";

export default function RunsPage() {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let stop = false;
    async function load() {
      try {
        const next = await api.runs();
        if (!stop) {
          setRuns(next);
          setError(null);
        }
      } catch (e) {
        if (!stop) setError((e as Error).message);
      }
    }
    load();
    const timer = setInterval(load, 4000);
    return () => {
      stop = true;
      clearInterval(timer);
    };
  }, []);

  const waiting = runs?.filter((r) => r.status === "awaiting_approval") ?? [];

  return (
    <>
      <h1>Runs</h1>
      <p className="lede">
        Each run takes one issue, works on it in a locked-down sandbox, and stops for your review
        before anything becomes a pull request.
      </p>
      <NewRun />

      {error ? <p className="error-note" role="alert">{error}</p> : null}
      {waiting.length > 0 ? (
        <p>
          {waiting.length === 1 ? "One run is" : `${waiting.length} runs are`} waiting for your review:{" "}
          {waiting.map((r, i) => (
            <span key={r.id}>{i > 0 ? ", " : ""}<Link href={`/runs/${r.id}`}>{r.task_id ?? r.issue_ref}</Link></span>
          ))}
        </p>
      ) : null}

      {runs === null && !error ? <p className="muted">Loading runs…</p> : null}
      {runs !== null && runs.length === 0 ? (
        <p className="muted">No runs yet. Start one above with a benchmark task or a GitHub issue.</p>
      ) : null}
      {runs !== null && runs.length > 0 ? (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Issue</th>
                <th scope="col">Status</th>
                <th scope="col">Model</th>
                <th scope="col" className="num">Model calls</th>
                <th scope="col" className="num">Tool calls</th>
                <th scope="col" className="num">Tokens</th>
                <th scope="col">Started</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.id}>
                  <td><Link href={`/runs/${r.id}`}>{r.task_id ?? r.issue_ref ?? r.repo}</Link></td>
                  <td><Status status={r.status} reason={TERMINAL.includes(r.status) ? r.failure_reason : null} /></td>
                  <td className="muted">{r.model}</td>
                  <td className="num">{r.llm_calls}</td>
                  <td className="num">{r.tool_calls}</td>
                  <td className="num">{(r.prompt_tokens + r.completion_tokens).toLocaleString()}</td>
                  <td className="muted">{ago(r.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </>
  );
}
