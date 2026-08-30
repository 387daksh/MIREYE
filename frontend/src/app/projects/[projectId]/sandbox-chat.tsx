"use client";

import { FormEvent, useState } from "react";
import { ArrowRight } from "lucide-react";
import { api } from "@/lib/api";
import { VerdictChip, type Verdict } from "@/components/product/ui";
import { arr, constraintName, rec, useSite, type Value } from "./site-context";

/**
 * The site agent — a direct port of the chat dock in app/static/sandbox.html.
 *
 * It talks to /v1/sandbox/{snapshot_id}/chat, which is a different agent from
 * the project orchestrator: this one edits the scene and re-runs the
 * deterministic evaluator, and its reply carries the new scene_state back.
 */

/** Tool name → what the user is told happened. Copied from renderChatResult(). */
const ACTIVITY: Record<string, string> = {
  get_site_context: "Reviewed the site",
  propose_data_center: "Created a conceptual campus",
  transform_object: "Updated the layout",
  evaluate_scenario: "Re-evaluated the site",
  get_evidence: "Reviewed MIREYE sources",
  remove_object: "Removed a proposed object",
  reset_proposals: "Reset proposed designs",
  check_evidence_freshness: "Checked MIREYE freshness",
  quote_mireye_refresh: "Prepared a refresh estimate",
  confirm_and_refresh_evidence: "Refreshed MIREYE intelligence",
};

/** The demo prompts the vanilla dock offered. */
const PROMPTS = [
  "Design a 100 MW phase-1 campus with room for 300 MW later.",
  "Create a second layout with more expansion room.",
  "Which layout uses the least land?",
  "Why is grid capacity unresolved?",
  "Refresh the MIREYE intelligence.",
];

const asVerdict = (outcome: unknown): Verdict => {
  const value = String(outcome ?? "").toUpperCase();
  if (value === "PASS") return "PASS";
  if (value === "FAIL" || value === "BLOCKED") return "BLOCKED";
  if (value === "PARTIAL") return "PARTIAL";
  return "UNRESOLVED";
};

const DEFAULT_REPLY =
  "Ask Mireye to place a facility, change a layout, evaluate it, or explain an outcome.";

export function SandboxChat({ projectId }: { projectId?: string }) {
  const { snapshotId, worldId, scene, setScene, snapshot } = useSite();
  const [message, setMessage] = useState("");
  const [reply, setReply] = useState(DEFAULT_REPLY);
  const [trace, setTrace] = useState<string>();
  const [evaluation, setEvaluation] = useState<Value>();
  const [status, setStatus] = useState("Ready");
  const [busy, setBusy] = useState(false);

  async function send(event: FormEvent) {
    event.preventDefault();
    const text = message.trim();
    if (!text || !snapshotId) return;
    setBusy(true);
    setStatus("Working with the site…");

    const response = await api.POST("/v1/sandbox/{snapshot_id}/chat", {
      params: { path: { snapshot_id: snapshotId } },
      body: {
        message: text,
        session_id: projectId ? `${projectId}:${snapshotId}` : snapshotId,
        workspace_id: snapshot?.workspace_id ?? null,
        world_snapshot_id: rec(scene).world_snapshot_id ?? worldId ?? null,
        scene_state: scene,
      } as never,
    });
    setBusy(false);

    if (response.error) {
      setReply("Sandbox chat failed.");
      setStatus("Mireye couldn't complete that change.");
      return;
    }

    const result = rec(response.data);
    // The agent returns the edited scene; putting it back on the map is the
    // whole point of this surface.
    if (result.scene_state) setScene(rec(result.scene_state));
    setReply(String(result.message ?? ""));

    const tools = arr(result.tool_trace)
      .map((item) => ACTIVITY[String(item.tool)] ?? "Completed a validated site action")
      .join(" · ");
    setTrace(tools || undefined);

    if (result.evaluation) {
      setEvaluation(rec(result.evaluation));
      setStatus(`Evaluation: ${String(rec(result.evaluation).overall_status ?? "")}`);
    } else {
      setEvaluation(undefined);
      setStatus("Ready");
    }
    setMessage("");
  }

  if (!snapshotId) return null;

  const results = arr(evaluation?.constraint_results);

  return (
    <section
      className="pointer-events-none absolute inset-x-0 bottom-0 z-20 p-3 md:p-4"
      aria-label="Ask about this site"
    >
      <div className="pointer-events-auto mx-auto w-[min(760px,100%)] rounded-[2px] bg-mi-surface shadow-panel">
        {/* Reply, activity trace and the evaluator's verdicts. */}
        <div className="mi-scroll max-h-[34vh] overflow-y-auto px-4 pt-4">
          <p aria-live="polite" className="text-[13px] leading-relaxed text-mi-fg">
            {reply}
          </p>

          {trace && (
            <p className="mt-2 font-mono text-[10px] uppercase tracking-cite text-mi-fg-muted">
              {trace}
            </p>
          )}

          {evaluation && (
            <div className="mt-3 rounded-[2px] bg-mi-surface-3 p-3 shadow-well">
              <div className="mb-2 flex items-center gap-2">
                <VerdictChip verdict={asVerdict(evaluation.overall_status)} />
                <span className="font-mono text-[10px] uppercase tracking-cite text-mi-fg-muted">
                  {results.length} requirement{results.length === 1 ? "" : "s"} checked
                </span>
              </div>
              <div className="space-y-2">
                {results.map((item, index) => (
                  <div
                    key={`${String(item.constraint_id)}-${index}`}
                    className="border-t border-mi-line pt-2 first:border-t-0 first:pt-0"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <strong className="text-[12px] font-medium text-mi-fg-strong">
                        {constraintName(item.constraint_id)}
                      </strong>
                      <VerdictChip verdict={asVerdict(item.outcome)} />
                    </div>
                    <p className="mt-1 text-[11px] leading-relaxed text-mi-fg-muted">
                      {String(item.explanation ?? "")}
                    </p>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Suggested instructions. */}
        <div className="mi-scroll mt-3 flex gap-1.5 overflow-x-auto px-4">
          {PROMPTS.map((prompt) => (
            <button
              key={prompt}
              type="button"
              onClick={() => setMessage(prompt)}
              className="shrink-0 cursor-pointer rounded-[2px] bg-mi-surface px-2.5 py-1.5 font-mono text-[10px] text-mi-fg-muted shadow-raised transition-all duration-micro ease-mi hover:text-mi-fg-strong hover:shadow-plateau active:translate-x-px active:translate-y-px active:shadow-press"
            >
              {prompt}
            </button>
          ))}
        </div>

        <form onSubmit={send} className="flex items-end gap-2 px-4 pb-2 pt-3">
          <label className="sr-only" htmlFor="sandbox-chat">
            Ask about this site
          </label>
          <textarea
            id="sandbox-chat"
            rows={1}
            value={message}
            onChange={(event) => setMessage(event.target.value)}
            onKeyDown={(event) => {
              // Enter sends, Shift+Enter breaks the line — as the vanilla did.
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                void send(event);
              }
            }}
            placeholder="Ask about this site…"
            className="min-h-[42px] w-full resize-none rounded-[2px] bg-mi-surface-3 px-3 py-2.5 text-[13px] text-mi-fg shadow-well outline-none transition-shadow duration-micro ease-mi placeholder:text-mi-fg-muted/70 focus:shadow-press"
          />
          <button
            type="submit"
            disabled={busy}
            aria-label="Send"
            className="grid h-[42px] w-[42px] shrink-0 cursor-pointer place-items-center rounded-[2px] bg-mi-fg-strong text-mi-bg shadow-raised transition-all duration-micro ease-mi hover:shadow-plateau active:translate-x-px active:translate-y-px active:shadow-press disabled:cursor-wait disabled:opacity-50"
          >
            {busy ? "…" : <ArrowRight className="h-4 w-4" strokeWidth={1.5} />}
          </button>
        </form>

        <p className="px-4 pb-3 font-mono text-[10px] uppercase tracking-cite text-mi-fg-muted">
          {status}
        </p>
      </div>
    </section>
  );
}
