"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";
import { ArrowRight } from "lucide-react";
import { api } from "@/lib/api";
import { orchestrationStage } from "@/lib/orchestration";
import { Eyebrow, PrimaryButton, TextButton, fieldClass } from "@/components/product/ui";
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

/** Stages that mean the run has stopped, so the status mark should not pulse. */
const SETTLED = new Set(["COMPLETED", "FAILED", "CANCELLED", "WAITING FOR YOU"]);
/** Event types that mark a problem, so the timeline dot goes orange. */
const ATTENTION_EVENTS = new Set(["TASK_FAILED", "FAILED", "CANCELLED", "NEEDS_USER_DECISION"]);

export function DecisionRequest({
  projectId,
  runId,
  decision,
  onDone,
}: {
  projectId: string;
  runId: string;
  decision: Value;
  onDone: () => void;
}) {
  const [choice, setChoice] = useState("");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const options = list(decision.options);
  const recommended = String(decision.recommended_option_id ?? "");

  async function answer(cancelled = false) {
    if (!cancelled && !choice && !text.trim())
      return setError("Choose an option or enter an answer.");
    setBusy(true);
    setError(undefined);
    const answered = await api.POST(
      "/v1/diligence/projects/{project_id}/decisions/{decision_id}/answer",
      {
        params: {
          path: { project_id: projectId, decision_id: String(decision.decision_id) },
        },
        body: {
          resume_token: String(decision.resume_token),
          option_id: choice || undefined,
          text: decision.allow_custom ? text || undefined : undefined,
          cancelled,
        } as never,
      },
    );
    if (answered.error) {
      setBusy(false);
      return setError("The decision could not be saved.");
    }
    const resumed = await api.POST(
      "/v1/ai/projects/{project_id}/orchestration/{run_id}/resume",
      { params: { path: { project_id: projectId, run_id: runId } } },
    );
    if (resumed.error) {
      setBusy(false);
      return setError("The decision was saved, but the investigation could not resume.");
    }
    setBusy(false);
    onDone();
  }

  return (
    <form
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        void answer();
      }}
      className="mx-auto mb-3 max-h-[62vh] max-w-[660px] overflow-y-auto rounded-[2px] border-t-2 border-t-mi-orange bg-mi-surface p-5 shadow-panel"
    >
      <div className="flex items-start gap-3">
        <span aria-hidden className="mt-1.5 h-2 w-2 shrink-0 bg-mi-orange" />
        <div className="min-w-0">
          <Eyebrow>MIREYE needs a decision</Eyebrow>
          <h2 className="mt-1.5 text-[19px] font-medium leading-snug tracking-tight text-mi-fg-strong">
            {String(decision.question ?? "A decision is required to continue.")}
          </h2>
        </div>
      </div>

      {Boolean(decision.context) && (
        <p className="mt-4 text-[12px] leading-relaxed text-mi-fg-muted">
          {String(decision.context)}
        </p>
      )}

      <div className="mt-4 space-y-2">
        {options.map((option) => {
          const id = String(option.id);
          const selected = choice === id;
          return (
            <label
              key={id}
              className={`flex cursor-pointer gap-3 border p-3 transition-colors duration-micro ease-mi ${
                selected
                  ? "border-mi-orange bg-mi-surface-2"
                  : "border-mi-line hover:border-mi-line-strong"
              }`}
            >
              <input
                type="radio"
                name="decision"
                value={id}
                checked={selected}
                onChange={(event) => setChoice(event.target.value)}
                className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-[var(--mi-orange)]"
              />
              <span className="min-w-0">
                <span className="flex flex-wrap items-center gap-2">
                  <strong className="text-[12px] font-medium text-mi-fg-strong">
                    {String(option.label)}
                  </strong>
                  {id === recommended && (
                    <em className="not-italic font-mono text-[8px] uppercase tracking-cite text-mi-orange-text">
                      Recommended
                    </em>
                  )}
                </span>
                <span className="mt-1 block text-[11px] leading-relaxed text-mi-fg-muted">
                  {String(option.description ?? option.consequence ?? "")}
                </span>
              </span>
            </label>
          );
        })}
      </div>

      {Boolean(decision.allow_custom) && (
        <label className="mt-4 block">
          <span className="mb-2 block font-mono text-[10px] uppercase tracking-label text-mi-fg-muted">
            Or add context
          </span>
          <textarea
            aria-label="Custom answer"
            value={text}
            onChange={(event) => setText(event.target.value)}
            placeholder="Add a response"
            rows={2}
            className={`${fieldClass} resize-y`}
          />
        </label>
      )}

      {error && (
        <p role="alert" className="mt-3 text-[11px] text-mi-orange-text">
          {error}
        </p>
      )}

      <div className="mt-5 flex items-center gap-4">
        <PrimaryButton disabled={busy}>{busy ? "Submitting…" : "Continue"}</PrimaryButton>
        <TextButton type="button" disabled={busy} onClick={() => void answer(true)}>
          Cancel request
        </TextButton>
      </div>
    </form>
  );
}

/**
 * Run activity, rendered as a terminal log rather than a spinner — the loading
 * state tells you what the system is actually doing.
 */
export function ActivityTimeline({ events }: { events: Value[] }) {
  const visible = events.filter((event) => activityCopy[String(event.type)]).slice(-8);
  if (!visible.length) return null;
  return (
    <details className="group ml-auto mb-2 w-[260px] border border-mi-line bg-mi-surface shadow-raised">
      <summary className="cursor-pointer list-none px-3 py-2 font-mono text-[10px] uppercase tracking-cite text-mi-fg-muted transition-colors duration-micro ease-mi hover:text-mi-fg-strong">
        <span className="mr-1.5 inline-block transition-transform duration-micro ease-mi group-open:rotate-90">
          ›
        </span>
        Activity · {visible.length}
      </summary>
      <ol className="px-3 pb-2.5">
        {visible.map((event, index) => {
          const type = String(event.type);
          return (
            <li
              key={String(event.sequence)}
              className="flex items-center gap-2 py-1 font-mono text-[10px] text-mi-fg-muted"
            >
              <span aria-hidden className="select-none text-mi-fg-muted/60">
                {String(index + 1).padStart(2, "0")}
              </span>
              <span
                aria-hidden
                className={`h-1.5 w-1.5 shrink-0 ${
                  ATTENTION_EVENTS.has(type) ? "bg-mi-orange" : "bg-mi-derived"
                }`}
              />
              <span className="truncate">{activityCopy[type]}</span>
            </li>
          );
        })}
      </ol>
    </details>
  );
}

export function AgentBar({
  onStart,
  busy,
  error,
  status,
}: {
  onStart: (message: string) => Promise<void>;
  busy: boolean;
  error?: string;
  status?: { label: string; detail: string };
}) {
  const [message, setMessage] = useState("");
  const suggestions = [
    "Why is power blocked?",
    "What changed?",
    "Can this site support 100 MW?",
    "Show me what is still unresolved.",
  ];
  const settled = status ? SETTLED.has(status.label.toUpperCase()) : false;

  return (
    <div className="relative">
      {status && (
        <div
          className="mx-auto mb-2 flex w-fit items-center gap-2.5 rounded-[2px] bg-mi-surface px-3 py-2 shadow-plateau"
          aria-live="polite"
        >
          <span
            aria-hidden
            className={`h-1.5 w-1.5 shrink-0 bg-mi-orange ${settled ? "" : "animate-caret"}`}
          />
          <div className="flex items-baseline gap-2">
            <strong className="font-mono text-[9px] uppercase tracking-cite text-mi-fg-strong">
              MIREYE · {status.label}
            </strong>
            <span className="hidden text-[11px] text-mi-fg-muted sm:inline">{status.detail}</span>
          </div>
        </div>
      )}

      <div className="mi-scroll mb-2 flex justify-start gap-1.5 overflow-x-auto sm:justify-center">
        {suggestions.map((prompt) => (
          <button
            type="button"
            key={prompt}
            onClick={() => setMessage(prompt)}
            className="shrink-0 cursor-pointer rounded-[2px] bg-mi-surface px-2.5 py-1.5 font-mono text-[10px] text-mi-fg-muted shadow-raised transition-all duration-micro ease-mi hover:text-mi-fg-strong hover:shadow-plateau active:translate-x-px active:translate-y-px active:shadow-press"
          >
            {prompt}
          </button>
        ))}
      </div>

      <form
        className="flex items-center gap-2 rounded-[2px] bg-mi-surface p-2 pl-4 shadow-panel"
        onSubmit={(event) => {
          event.preventDefault();
          void onStart(message);
        }}
      >
        <span aria-hidden className="h-1.5 w-1.5 shrink-0 bg-mi-orange" />
        <input
          aria-label="Ask MIREYE about this site"
          value={message}
          onChange={(event) => setMessage(event.target.value)}
          required
          placeholder="Ask MIREYE about this site…"
          className="min-w-0 flex-1 border-0 bg-transparent py-2 text-[13px] text-mi-fg outline-none placeholder:text-mi-fg-muted/70"
        />
        <PrimaryButton
          disabled={busy}
          aria-label="Ask MIREYE"
          className="h-9 w-9 shrink-0 px-0 py-0"
        >
          {busy ? "…" : <ArrowRight className="h-4 w-4" strokeWidth={1.5} />}
        </PrimaryButton>
      </form>

      {error && (
        <p
          role="alert"
          className="mt-1.5 border border-mi-orange/50 bg-mi-surface px-3 py-2 text-[11px] text-mi-orange-text"
        >
          {error}
        </p>
      )}
    </div>
  );
}

export function OrchestrationPanel({
  projectId,
  runs,
  activeDecision,
}: {
  projectId: string;
  runs?: Value[];
  activeDecision?: Value;
}) {
  const [runId, setRunId] = useState<string>();
  const [events, setEvents] = useState<Value[]>([]);
  const [decision, setDecision] = useState<Value>();
  const [answeredDecisionId, setAnsweredDecisionId] = useState<string>();
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string>();
  const restoredRunId = useMemo(() => {
    const latest =
      [...(runs ?? [])]
        .reverse()
        .find((item) => ["RUNNING", "WAITING_FOR_DECISION"].includes(String(item.status))) ??
      runs?.at(-1);
    return latest?.run_id ? String(latest.run_id) : undefined;
  }, [runs]);
  const activeRunId = runId ?? restoredRunId;
  const persistedRun = useMemo(
    () => runs?.find((item) => item.run_id === activeRunId),
    [activeRunId, runs],
  );
  const persistedDecision =
    persistedRun?.status === "WAITING_FOR_DECISION" &&
    activeDecision?.decision_id &&
    String(activeDecision.decision_id) !== answeredDecisionId
      ? activeDecision
      : undefined;
  const currentDecision = decision ?? persistedDecision;
  const lastEvent = events.at(-1);
  const stage = starting ? "UNDERSTANDING" : orchestrationStage(persistedRun?.status, lastEvent?.type);
  const status =
    activeRunId || starting
      ? stageCopy[stage] ?? { label: stage, detail: "Reviewing project state" }
      : undefined;

  async function start(message: string) {
    setStarting(true);
    setStartError(undefined);
    setEvents([]);
    setDecision(undefined);
    const response = await api.POST("/v1/ai/projects/{project_id}/orchestrate", {
      params: { path: { project_id: projectId } },
      body: { message },
    });
    setStarting(false);
    if (response.error) return setStartError("MIREYE could not start this investigation.");
    const run = (response.data as { run?: { run_id?: string } } | undefined)?.run;
    if (run?.run_id) setRunId(run.run_id);
  }

  useEffect(() => {
    if (!activeRunId) return;
    const source = new EventSource(
      `${process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"}/v1/ai/projects/${projectId}/orchestration/${activeRunId}/events`,
    );
    const receive = (event: MessageEvent) => {
      const value = JSON.parse(event.data) as Value;
      setEvents((items) =>
        items.some((item) => item.sequence === value.sequence) ? items : [...items, value],
      );
      if (value.decision_request) setDecision(value.decision_request as Value);
      if (["COMPLETED", "FAILED", "CANCELLED"].includes(String(value.type))) setDecision(undefined);
    };
    Object.keys(activityCopy).forEach((name) => source.addEventListener(name, receive));
    return () => source.close();
  }, [projectId, activeRunId]);

  return (
    <section
      className="pointer-events-none absolute bottom-[86px] left-1/2 z-20 w-[min(760px,calc(100%-32px))] -translate-x-1/2 [&>*]:pointer-events-auto"
      aria-label="Ask MIREYE"
    >
      {currentDecision && activeRunId && (
        <DecisionRequest
          projectId={projectId}
          runId={activeRunId}
          decision={currentDecision}
          onDone={() => {
            setDecision(undefined);
            setAnsweredDecisionId(String(currentDecision.decision_id));
          }}
        />
      )}
      <ActivityTimeline events={events} />
      <AgentBar onStart={start} busy={starting} error={startError} status={status} />
    </section>
  );
}
