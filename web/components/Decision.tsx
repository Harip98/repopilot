"use client";

import { useState } from "react";
import { api, ApiError } from "@/lib/api";

export function Decision({ runId, opensPr, onDecided }: { runId: string; opensPr: boolean; onDecided: () => void }) {
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function decide(decision: "approve" | "reject") {
    setBusy(true);
    setError(null);
    try {
      await api.decide(runId, decision, comment.trim() || undefined);
      onDecided();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "The decision was not sent. Check that the API is running.");
      setBusy(false);
    }
  }

  return (
    <form className="decision" onSubmit={(e) => e.preventDefault()} aria-label="Review decision">
      <label htmlFor="comment">
        Comment <span className="muted">(a rejection with a comment gets one revision)</span>
      </label>
      <textarea
        id="comment" className="field" value={comment} onChange={(e) => setComment(e.target.value)}
        placeholder="What should change?"
      />
      <div className="row">
        <button type="button" className="button" disabled={busy} onClick={() => decide("approve")}>
          {opensPr ? "Approve and open draft PR" : "Approve"}
        </button>
        <button type="button" className="button quiet" disabled={busy || !comment.trim()} onClick={() => decide("reject")}>
          Request changes
        </button>
        <button type="button" className="button quiet" disabled={busy || Boolean(comment.trim())} onClick={() => decide("reject")}>
          Reject
        </button>
        {error ? <span className="error-note" role="alert">{error}</span> : null}
      </div>
    </form>
  );
}
