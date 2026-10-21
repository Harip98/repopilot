import type { RunEvent, RunStatus, ToolCallRow } from "@/lib/api";

interface Stop {
  key: string;
  kind: string;
  what: string;
  detail?: string;
  calls?: ToolCallRow[];
}

const CONTROL = new Set(["submit_plan", "finish"]);

function firstSentence(text: string): string {
  const clean = text.replace(/\s+/g, " ").trim();
  const end = clean.search(/[.!?](\s|$)/);
  const sentence = end > 0 ? clean.slice(0, end + 1) : clean;
  return sentence.length > 140 ? `${sentence.slice(0, 137)}…` : sentence;
}

function reason(value: unknown): string {
  return String(value ?? "").replaceAll("_", " ");
}

/** Turn the run's event log and tool calls into stops along one track. */
export function buildStops(events: RunEvent[], toolCalls: ToolCallRow[], status: RunStatus): Stop[] {
  const stops: Stop[] = [];
  let cursor = 0;
  for (const ev of events) {
    const p = ev.payload as Record<string, unknown>;
    const key = String(ev.seq);
    if (ev.node === "llm" && ev.kind === "reply") {
      const names = (p.tool_calls as string[] | undefined) ?? [];
      const real = names.filter((n) => !CONTROL.has(n)).length;
      const calls = toolCalls.slice(cursor, cursor + real);
      cursor += real;
      const content = String(p.content ?? "");
      stops.push({
        key, kind: String(p.phase ?? "explore"), calls,
        what: content ? firstSentence(content) : names.length ? `Called ${names.join(", ")}` : "Empty reply",
      });
    } else if (ev.node === "llm" && ev.kind === "error") {
      stops.push({ key, kind: "gate-fail", what: "Model unavailable", detail: String(p.error ?? "") });
    } else if (ev.node === "tools" && ev.kind === "submit_plan") {
      stops.push({ key, kind: "edit", what: "Plan submitted; editing tools unlocked" });
    } else if (ev.node === "tools" && ev.kind === "finish") {
      stops.push({ key, kind: "edit", what: "Asked for validation" });
    } else if (ev.node === "validate") {
      const passed = Boolean(p.passed);
      stops.push({
        key, kind: passed ? "gate-pass" : "gate-fail",
        what: passed ? "Test suite passed" : `Validation failed: ${reason(p.reason)}`,
        detail: p.exit_code != null ? `exit code ${p.exit_code}` : undefined,
      });
    } else if (ev.node === "approval") {
      const by = String(p.by ?? "reviewer");
      stops.push({
        key, kind: "station",
        what: ev.kind === "approve" ? `Approved by ${by}` : `Rejected by ${by}`,
        detail: p.comment ? `“${String(p.comment)}”` : undefined,
      });
    } else if (ev.node === "grade") {
      stops.push({
        key, kind: p.resolved ? "gate-pass" : "gate-fail",
        what: p.resolved ? "Hidden tests passed" : "Hidden tests failed",
      });
    } else if (ev.node === "record") {
      const ok = ev.kind === "completed" || ev.kind === "pr_opened";
      stops.push({
        key, kind: ok ? "end-pass" : "end-fail",
        what: ok ? (ev.kind === "pr_opened" ? "Draft pull request opened" : "Finished") : `Stopped: ${reason(ev.kind)}`,
        detail: p.failure_reason ? reason(p.failure_reason) : undefined,
      });
    }
  }
  if (status === "awaiting_approval") {
    stops.push({ key: "waiting", kind: "station", what: "Waiting for your review" });
  }
  return stops;
}

export function Track({ stops, live }: { stops: Stop[]; live: boolean }) {
  return (
    <aside className="track" aria-label="What the agent did">
      <h2>Path</h2>
      {stops.length === 0 ? (
        <p className="muted">{live ? "Starting the sandbox and reading the repository…" : "Nothing recorded."}</p>
      ) : (
        <ol>
          {stops.map((stop) => (
            <li key={stop.key} className={`stop ${stop.kind}`}>
              <div className="what">{stop.what}</div>
              {stop.detail ? <div className="detail">{stop.detail}</div> : null}
              {stop.calls && stop.calls.length > 0 ? (
                <div className="calls">
                  {stop.calls.map((c, i) => (
                    <span
                      key={`${c.step}-${c.tool}-${i}`}
                      className={`call ${c.denied ? "denied" : c.ok ? "" : "failed"}`}
                      title={c.error ?? JSON.stringify(c.args)}
                    >
                      {c.tool}
                    </span>
                  ))}
                </div>
              ) : null}
            </li>
          ))}
        </ol>
      )}
      <div className="phase-legend" aria-hidden="true">
        <span>○ exploring</span><span>● editing</span><span>◆ checks</span>
      </div>
    </aside>
  );
}
