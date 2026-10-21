"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { api, ApiError } from "@/lib/api";

export function NewRun() {
  const router = useRouter();
  const [mode, setMode] = useState<"bench" | "github">("bench");
  const [taskId, setTaskId] = useState("orders-api-005");
  const [issueRef, setIssueRef] = useState("");
  const [autoApprove, setAutoApprove] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function start(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      let runId: string;
      if (mode === "bench") {
        runId = (await api.startBench(taskId.trim(), autoApprove)).run_id;
      } else {
        const match = /^([\w.-]+\/[\w.-]+)#(\d+)$/.exec(issueRef.trim());
        if (!match) throw new ApiError(422, "invalid", "Write the issue as owner/repo#number, for example acme/shop#42.");
        runId = (await api.startGithub(match[1], Number(match[2]))).run_id;
      }
      router.push(`/runs/${runId}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "The run was not started. Check that the API is running.");
      setBusy(false);
    }
  }

  return (
    <form className="new-run" onSubmit={start} aria-label="Start a run">
      <label>
        Source
        <select className="field" value={mode} onChange={(e) => setMode(e.target.value as "bench" | "github")}>
          <option value="bench">Benchmark task</option>
          <option value="github">GitHub issue</option>
        </select>
      </label>
      {mode === "bench" ? (
        <label>
          Task id
          <input className="field" value={taskId} onChange={(e) => setTaskId(e.target.value)} required />
        </label>
      ) : (
        <label>
          Issue
          <input className="field" value={issueRef} onChange={(e) => setIssueRef(e.target.value)}
                 placeholder="owner/repo#42" required />
        </label>
      )}
      {mode === "bench" ? (
        <label className="check">
          <input type="checkbox" checked={autoApprove} onChange={(e) => setAutoApprove(e.target.checked)} />
          Skip review
        </label>
      ) : null}
      <button className="button" type="submit" disabled={busy}>{busy ? "Starting…" : "Start run"}</button>
      {error ? <span className="error-note" role="alert">{error}</span> : null}
    </form>
  );
}
