"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ago, api, type EvalListing } from "@/lib/api";

export default function EvalsPage() {
  const [evals, setEvals] = useState<EvalListing[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.evals().then(setEvals).catch((e: Error) => setError(e.message));
  }, []);

  return (
    <>
      <h1>Evaluations</h1>
      <p className="lede">
        Each evaluation runs the agent without review over a fixed set of benchmark tasks. A task
        counts as resolved only when hidden tests, never shown to the agent, pass.
      </p>
      {error ? <p className="error-note" role="alert">{error}</p> : null}
      {evals === null && !error ? <p className="muted">Loading…</p> : null}
      {evals !== null && evals.length === 0 ? (
        <p className="muted">No evaluations yet. Run one with <code>repopilot eval run --name NAME --split dev</code>.</p>
      ) : null}
      {evals !== null && evals.length > 0 ? (
        <div className="table-wrap" style={{ marginTop: "2rem" }}>
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Evaluation</th>
                <th scope="col">Model</th>
                <th scope="col" className="num">Resolved</th>
                <th scope="col" className="num">Tasks</th>
                <th scope="col">Updated</th>
              </tr>
            </thead>
            <tbody>
              {evals.map((e) => (
                <tr key={e.name}>
                  <td><Link href={`/evals/${encodeURIComponent(e.name)}`}>{e.name}</Link></td>
                  <td className="muted">{e.model}</td>
                  <td className="num">{e.resolved}</td>
                  <td className="num">{e.tasks}</td>
                  <td className="muted">{ago(e.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </>
  );
}
