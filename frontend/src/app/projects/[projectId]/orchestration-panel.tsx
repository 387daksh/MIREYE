"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";
import { api } from "../../../lib/api";
import { orchestrationStage } from "../../../lib/orchestration";
import { Value, list } from "./product-components";

const stageCopy: Record<string, { label: string; detail: string }> = {
  UNDERSTANDING: { label: "Understanding", detail: "Understanding your requirements" },
  INVESTIGATING: { label: "Investigating", detail: "Investigating site intelligence" },
  VERIFYING: { label: "Verifying", detail: "Verifying evidence and constraints" },
  "WAITING FOR YOU": { label: "Waiting for you", detail: "A project decision needs your input" },
  RESUMING: { label: "Resuming", detail: "Continuing the site investigation" },
  COMPLETED: { label: "Completed", detail: "Site intelligence is current" },
  FAILED: { label: "Needs attention", detail: "The investigation could not complete" },
  CANCELLED: { label: "Cancelled", detail: "The investigation was cancelled" },
};

const activityCopy: Record<string, string> = {
  RUN_STARTED: "Investigation started",
  PLANNING: "Understanding project requirements",
  TASK_STARTED: "Checking project evidence",
  TASK_COMPLETED: "Evidence check complete",
  TASK_FAILED: "An evidence check needs attention",
  EVIDENCE_FOUND: "Relevant evidence collected",
  VERIFICATION: "Verifying findings",
  REPLAN: "Refining the next action",
  NEEDS_USER_DECISION: "Your decision is needed",
  RESUMED: "Investigation resumed",
  COMPLETED: "Site intelligence current",
  FAILED: "Investigation needs attention",
  CANCELLED: "Investigation cancelled",
};

export function DecisionRequest({ projectId, runId, decision, onDone }: { projectId: string; runId: string; decision: Value; onDone: () => void }) {
  const [choice, setChoice] = useState("");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const options = list(decision.options);
  const recommended = String(decision.recommended_option_id ?? "");

  async function answer(cancelled = false) {
    if (!cancelled && !choice && !text.trim()) return setError("Choose an option or enter an answer.");
    setBusy(true);
    setError(undefined);
    const answered = await api.POST("/v1/diligence/projects/{project_id}/decisions/{decision_id}/answer", {
      params: { path: { project_id: projectId, decision_id: String(decision.decision_id) } },
      body: { resume_token: String(decision.resume_token), option_id: choice || undefined, text: decision.allow_custom ? text || undefined : undefined, cancelled } as never,
    });
    if (answered.error) { setBusy(false); return setError("The decision could not be saved."); }
    const resumed = await api.POST("/v1/ai/projects/{project_id}/orchestration/{run_id}/resume", { params: { path: { project_id: projectId, run_id: runId } } });
    if (resumed.error) { setBusy(false); return setError("The decision was saved, but the investigation could not resume."); }
    setBusy(false);
    onDone();
  }

  return <form onSubmit={(event: FormEvent) => { event.preventDefault(); void answer(); }} className="decision-surface">
    <div className="decision-heading"><span className="agent-orb" aria-hidden="true"/><div><span className="eyebrow">MIREYE needs a decision</span><h2>{String(decision.question ?? "A decision is required to continue.")}</h2></div></div>
    {Boolean(decision.context) && <p className="decision-context">{String(decision.context)}</p>}
    <div className="decision-options">{options.map((option) => {
      const id = String(option.id);
      return <label key={id} className={choice === id ? "decision-option selected" : "decision-option"}>
        <input type="radio" name="decision" value={id} checked={choice === id} onChange={(event) => setChoice(event.target.value)}/>
        <span><strong>{String(option.label)}</strong>{id === recommended && <em>Recommended</em>}<small>{String(option.description ?? option.consequence ?? "")}</small></span>
      </label>;
    })}</div>
    {Boolean(decision.allow_custom) && <label className="custom-answer"><span>Or add context</span><textarea aria-label="Custom answer" value={text} onChange={(event) => setText(event.target.value)} placeholder="Add a response" rows={2}/></label>}
    {error && <p role="alert" className="form-error">{error}</p>}
    <div className="decision-actions"><button className="primary-action" disabled={busy}>{busy ? "Submitting…" : "Continue"}</button><button className="text-action" type="button" disabled={busy} onClick={() => void answer(true)}>Cancel request</button></div>
  </form>;
}

export function ActivityTimeline({ events }: { events: Value[] }) {
  const visible = events.filter((event) => activityCopy[String(event.type)]).slice(-8);
  if (!visible.length) return null;
  return <details className="activity-timeline"><summary>Activity</summary><ol>{visible.map((event) => <li key={String(event.sequence)} className={`activity-${String(event.type).toLowerCase()}`}><span/>{activityCopy[String(event.type)]}</li>)}</ol></details>;
}

export function AgentBar({ onStart, busy, error, status }: { onStart: (message: string) => Promise<void>; busy: boolean; error?: string; status?: { label: string; detail: string } }) {
  const [message, setMessage] = useState("");
  const suggestions = ["Why is power blocked?", "What changed?", "Can this site support 100 MW?", "Show me what is still unresolved."];
  return <div className="agent-area">
    {status && <div className="agent-status" aria-live="polite"><span className="agent-orb"/><div><strong>MIREYE · {status.label}</strong><span>{status.detail}</span></div></div>}
    <div className="suggested-prompts">{suggestions.map((prompt) => <button type="button" key={prompt} onClick={() => setMessage(prompt)}>{prompt}</button>)}</div>
    <form className="agent-bar" onSubmit={(event) => { event.preventDefault(); void onStart(message); }}>
      <span className="agent-orb" aria-hidden="true"/><input aria-label="Ask MIREYE about this site" value={message} onChange={(event) => setMessage(event.target.value)} required placeholder="Ask MIREYE about this site…"/><button className="agent-submit" disabled={busy} aria-label="Ask MIREYE">{busy ? "…" : "→"}</button>
    </form>
    {error && <p role="alert" className="form-error agent-error">{error}</p>}
  </div>;
}

export function OrchestrationPanel({ projectId, runs, activeDecision }: { projectId: string; runs?: Value[]; activeDecision?: Value }) {
  const [runId, setRunId] = useState<string>();
  const [events, setEvents] = useState<Value[]>([]);
  const [decision, setDecision] = useState<Value>();
  const [answeredDecisionId, setAnsweredDecisionId] = useState<string>();
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string>();
  const restoredRunId = useMemo(() => {
    const latest = [...(runs ?? [])].reverse().find((item) => ["RUNNING", "WAITING_FOR_DECISION"].includes(String(item.status))) ?? runs?.at(-1);
    return latest?.run_id ? String(latest.run_id) : undefined;
  }, [runs]);
  const activeRunId = runId ?? restoredRunId;
  const persistedRun = useMemo(() => runs?.find((item) => item.run_id === activeRunId), [activeRunId, runs]);
  const persistedDecision = persistedRun?.status === "WAITING_FOR_DECISION" && activeDecision?.decision_id && String(activeDecision.decision_id) !== answeredDecisionId ? activeDecision : undefined;
  const currentDecision = decision ?? persistedDecision;
  const lastEvent = events.at(-1);
  const stage = starting ? "UNDERSTANDING" : orchestrationStage(persistedRun?.status, lastEvent?.type);
  const status = activeRunId || starting ? stageCopy[stage] ?? { label: stage, detail: "Reviewing project state" } : undefined;

  async function start(message: string) {
    setStarting(true);
    setStartError(undefined);
    setEvents([]);
    setDecision(undefined);
    const response = await api.POST("/v1/ai/projects/{project_id}/orchestrate", { params: { path: { project_id: projectId } }, body: { message } });
    setStarting(false);
    if (response.error) return setStartError("MIREYE could not start this investigation.");
    const run = (response.data as { run?: { run_id?: string } } | undefined)?.run;
    if (run?.run_id) setRunId(run.run_id);
  }

  useEffect(() => {
    if (!activeRunId) return;
    const source = new EventSource(`${process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"}/v1/ai/projects/${projectId}/orchestration/${activeRunId}/events`);
    const receive = (event: MessageEvent) => {
      const value = JSON.parse(event.data) as Value;
      setEvents((items) => items.some((item) => item.sequence === value.sequence) ? items : [...items, value]);
      if (value.decision_request) setDecision(value.decision_request as Value);
      if (["COMPLETED", "FAILED", "CANCELLED"].includes(String(value.type))) setDecision(undefined);
    };
    Object.keys(activityCopy).forEach((name) => source.addEventListener(name, receive));
    return () => source.close();
  }, [projectId, activeRunId]);

  return <section className="orchestration-surface" aria-label="Ask MIREYE">
    {currentDecision && activeRunId && <DecisionRequest projectId={projectId} runId={activeRunId} decision={currentDecision} onDone={() => { setDecision(undefined); setAnsweredDecisionId(String(currentDecision.decision_id)); }}/>} 
    <ActivityTimeline events={events}/>
    <AgentBar onStart={start} busy={starting} error={startError} status={status}/>
  </section>;
}
