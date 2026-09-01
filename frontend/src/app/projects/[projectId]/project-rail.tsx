"use client";

import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { ArrowUpRight } from "lucide-react";
import { api } from "@/lib/api";
import { CountUp, Reveal } from "@/components/motion/primitives";
import { AssistantAnswer } from "@/components/product/assistant-answer";
import {
  OriginTag,
  PrimaryButton,
  Quiet,
  RailHeading,
  RailSection,
  TextButton,
  VerdictChip,
  fieldClass,
  type Verdict,
} from "@/components/product/ui";
import { Value, list, record } from "./product-components";

const words = (value: unknown) => String(value ?? "").replaceAll("_", " ");

const asVerdict = (value: unknown): Verdict => {
  const v = String(value ?? "").toUpperCase();
  if (v === "PASS" || v === "READY" || v === "VERIFIED") return "PASS";
  if (v === "FAIL" || v === "BLOCKED" || v === "CRITICAL") return "BLOCKED";
  if (v === "PARTIAL") return "PARTIAL";
  if (v === "UNRESOLVED" || v === "UNAVAILABLE") return "UNRESOLVED";
  return "NOT ASSESSED";
};

/** A collapsible row: label + verdict in the summary, detail underneath. */
function Disclosure({
  label,
  verdict,
  aside,
  children,
}: {
  label: string;
  verdict?: Verdict;
  aside?: ReactNode;
  children: ReactNode;
}) {
  return (
    <details className="group border-t border-mi-line first:border-t-0">
      <summary className="flex cursor-pointer list-none items-center justify-between gap-3 py-2.5">
        <span className="flex min-w-0 items-center gap-1.5">
          <span
            aria-hidden
            className="shrink-0 text-mi-fg-muted transition-transform duration-micro ease-mi group-open:rotate-90"
          >
            ›
          </span>
          <span className="min-w-0 truncate text-[12px] text-mi-fg">{label}</span>
        </span>
        {verdict ? <VerdictChip verdict={verdict} /> : aside}
      </summary>
      <div className="pb-3 pl-4">{children}</div>
    </details>
  );
}

/* ─── Power readiness / Entitlement ───────────────────────────────────────── */

/** Mirrors readinessValue() in app/static/app.js. */
function readinessValue(item: Value): string {
  const value = item.value;
  if (value === null || value === undefined) return "Not established";
  if (typeof value === "object")
    return Object.entries(value as Value)
      .slice(0, 3)
      .map(([key, v]) => `${words(key)}: ${String(v)}`)
      .join(" · ");
  return `${String(value)}${item.unit ? ` ${String(item.unit)}` : ""}`;
}

/**
 * Power readiness and Entitlement share a shape: a headline readiness_state,
 * a list of items with their own state, and evidence_details keyed by id.
 */
export function ReadinessModule({
  kicker,
  title,
  state,
}: {
  kicker: string;
  title: string;
  state: Value;
}) {
  const items = list(state.items);
  const evidence = new Map(
    list(state.evidence_details).map((item) => [String(item.evidence_id), item]),
  );
  const headline = String(state.readiness_state ?? "UNAVAILABLE");

  return (
    <RailSection>
      <RailHeading
        kicker={kicker}
        title={title}
        aside={<VerdictChip verdict={asVerdict(headline)} />}
      />
      {items.length ? (
        <div>
          {items.map((item) => {
            const ids = (item.evidence_ids as string[] | undefined) ?? [];
            const refs = ids.map((id) => evidence.get(String(id))).filter(Boolean) as Value[];
            return (
              <Disclosure
                key={String(item.key ?? item.label)}
                label={String(item.label ?? item.key ?? "")}
                verdict={asVerdict(item.state)}
              >
                <p className="mono-num font-mono text-[11px] text-mi-fg-strong">
                  {readinessValue(item)}
                </p>
                <p className="mt-1.5 text-[11px] leading-relaxed text-mi-fg-muted">
                  {String(item.explanation ?? "")}
                </p>
                {refs.map((source, i) => (
                  <p
                    key={`${String(source.evidence_id)}-${i}`}
                    className="mt-2 font-mono text-[10px] text-mi-fg-muted"
                  >
                    {typeof source.source_url === "string" ? (
                      <a
                        href={source.source_url}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-flex cursor-pointer items-center gap-1 text-mi-fg transition-colors duration-micro ease-mi hover:text-mi-fg-strong"
                      >
                        {String(source.provider ?? "Source")} · View evidence
                        <ArrowUpRight className="h-3 w-3" strokeWidth={1.5} />
                      </a>
                    ) : (
                      <span>
                        {String(source.provider ?? "Source")} ·{" "}
                        {String(source.dataset ?? "Source")}
                      </span>
                    )}
                    <span className="block">
                      {String(source.section_reference ?? source.dataset ?? "")} ·{" "}
                      {String(source.scope ?? "")} ·{" "}
                      {String(source.freshness ?? "freshness unavailable")}
                      {source.human_review_required ? " · Human review required" : ""}
                    </span>
                  </p>
                ))}
              </Disclosure>
            );
          })}
        </div>
      ) : (
        <Quiet>No source-backed items are available for this domain yet.</Quiet>
      )}
    </RailSection>
  );
}

/* ─── What we know ────────────────────────────────────────────────────────── */

/**
 * Evidence coverage, filtered exactly as the vanilla did: only entries that are
 * decision-provable or have evidence available count as "known".
 */
export function EvidenceCoverage({ intelligence }: { intelligence: Value }) {
  const evidenceById = new Map(
    list(intelligence.evidence_items).map((item) => [String(item.evidence_id), item]),
  );
  const known = list(intelligence.evidence_coverage).filter(
    (item) => item.decision_provable || item.evidence_available,
  );

  return (
    <RailSection>
      <RailHeading
        kicker="What we know"
        title={
          <span className="mono-num">
            <CountUp to={known.length} duration={600} />
            <span className="text-mi-fg-muted"> established</span>
          </span>
        }
        aside={<OriginTag origin="observed" />}
      />
      {known.length ? (
        <div>
          {known.map((item) => {
            const ids = (item.evidence_ids as string[] | undefined) ?? [];
            const sources = ids.map((id) => evidenceById.get(String(id))).filter(Boolean) as Value[];
            return (
              <Disclosure
                key={String(item.title)}
                label={String(item.title ?? "")}
                verdict={asVerdict(item.status)}
              >
                <p className="text-[11px] leading-relaxed text-mi-fg-muted">
                  {String(item.outcome_explanation ?? "")}
                </p>
                {sources.map((source, i) => (
                  <p
                    key={`${String(source.evidence_id)}-${i}`}
                    className="mt-2 font-mono text-[10px] text-mi-fg-muted"
                  >
                    {words(source.semantic_strength ?? "SOURCE_BACKED_SIGNAL").toLowerCase()} ·{" "}
                    {String(source.source ?? "MIREYE source")} ·{" "}
                    {String(source.scope ?? "scope unavailable")} ·{" "}
                    {source.expires_at
                      ? `fresh until ${new Date(Number(source.expires_at) * 1000).toLocaleString()}`
                      : "freshness unavailable"}
                  </p>
                ))}
              </Disclosure>
            );
          })}
        </div>
      ) : (
        <Quiet>Nothing is established by source-backed evidence yet.</Quiet>
      )}
    </RailSection>
  );
}

/* ─── What changed ────────────────────────────────────────────────────────── */

const SIGNIFICANCE_VERDICT = (value: unknown): Verdict =>
  String(value ?? "").toUpperCase() === "MATERIAL" ? "BLOCKED" : "UNRESOLVED";

/**
 * Source drift since the last refresh, plus the two explicit controls the
 * vanilla exposed: watch this shortlist, and check freshness now.
 */
export function ProjectChanges({ projectId, siteId, onRefresh }: { projectId: string; siteId?: string; onRefresh?: () => Promise<unknown> }) {
  const [changes, setChanges] = useState<Value>();
  const [failed, setFailed] = useState(false);
  const [note, setNote] = useState<string>();
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void api
      .GET("/v1/diligence/projects/{project_id}/changes", {
        params: { path: { project_id: projectId }, query: { limit: 5 } } as never,
      })
      .then((response) => {
        if (response.error) setFailed(true);
        else setChanges(response.data as Value);
      });
  }, [projectId]);

  async function watch() {
    setBusy(true);
    const response = await api.POST("/v1/diligence/projects/{project_id}/watch", {
      params: { path: { project_id: projectId } },
      body: { enabled: true } as never,
    });
    setBusy(false);
    if (response.error) return setNote("Watch check failed.");
    const enabled = record(response.data).enabled;
    setNote(
      enabled
        ? "This shortlist is saved for explicit freshness checks."
        : "Watch mode is off.",
    );
  }

  async function checkNow() {
    setBusy(true);
    const response = await api.POST("/v1/diligence/projects/{project_id}/check-now", {
      params: { path: { project_id: projectId } },
      body: {} as never,
    });
    setBusy(false);
    if (response.error) return setNote("Freshness check failed.");
    setNote("Freshness check requested.");
  }

  async function refreshSources() {
    if (!siteId) return setNote("No enriched site is available for source refresh.");
    setBusy(true);
    const response = await api.POST(
      "/v1/diligence/projects/{project_id}/sites/{site_id}/sources/refresh",
      { params: { path: { project_id: projectId, site_id: siteId } } },
    );
    setBusy(false);
    if (response.error) return setNote("Free-source refresh failed; MIREYE evidence is unchanged.");
    const records = list(record(record(response.data).source_result).records).length;
    setNote(`Free-source refresh completed with ${records} source records.`);
    await onRefresh?.();
  }

  // The vanilla hid this section entirely when /changes was unavailable.
  if (failed) return null;

  const items = list(changes?.items);
  const material = Number(changes?.material_change_count ?? 0);
  const total = Number(changes?.change_count ?? 0);

  return (
    <RailSection>
      <RailHeading
        kicker="What changed"
        title={
          <span className="mono-num">
            <CountUp to={Number.isFinite(material) ? material : 0} duration={600} />
            <span className="text-mi-fg-muted"> material</span>
          </span>
        }
        aside={<OriginTag origin="derived" />}
      />
      <Quiet>
        {total
          ? `${material} material change${material === 1 ? "" : "s"} since the last refresh.`
          : "No source changes have been recorded yet."}
      </Quiet>

      {items.length > 0 && (
        <div className="mt-4 space-y-px">
          {items.map((change, index) => (
            <div key={index} className="rounded-[2px] bg-mi-surface-3 p-3 shadow-well">
              <div className="flex items-start justify-between gap-3">
                <strong className="text-[12px] font-medium text-mi-fg-strong">
                  {String(change.what_changed ?? "")}
                </strong>
                <VerdictChip verdict={SIGNIFICANCE_VERDICT(change.significance)} />
              </div>
              <p className="mt-1.5 text-[11px] leading-relaxed text-mi-fg-muted">
                {String(change.why_it_matters ?? "")}
              </p>
              <p className="mt-1 font-mono text-[10px] text-mi-fg-muted">
                {String(change.what_happens_next ?? "")}
              </p>
            </div>
          ))}
        </div>
      )}

      <div className="mt-5 flex items-center gap-5">
        <TextButton onClick={() => void watch()} disabled={busy}>
          Watch shortlist
        </TextButton>
        <TextButton onClick={() => void checkNow()} disabled={busy}>
          Check freshness
        </TextButton>
        <TextButton onClick={() => void refreshSources()} disabled={busy || !siteId}>
          Refresh free sources
        </TextButton>
      </div>
      {note && <p className="mt-2 text-[11px] text-mi-fg-muted">{note}</p>}
    </RailSection>
  );
}

/* ─── Project assistant ──────────────────────────────────────────────────── */

export function ProjectAssistant({ projectId, onRefresh }: { projectId: string; onRefresh?: () => Promise<unknown> }) {
  const [message, setMessage] = useState("");
  const [reply, setReply] = useState("Ask about blockers, evidence, sources, or the next action.");
  const [activity, setActivity] = useState<string>();
  const [busy, setBusy] = useState(false);
  const prompts = ["Why is power blocked?", "What can I resolve next?", "Draft the highest-priority request."];

  async function send(event: FormEvent) {
    event.preventDefault();
    const query = message.trim();
    if (!query) return;
    setBusy(true);
    setActivity(undefined);
    const response = await api.POST("/v1/diligence/projects/{project_id}/chat", {
      params: { path: { project_id: projectId } },
      body: { message: query, session_id: `${projectId}:assistant` },
    });
    setBusy(false);
    if (response.error) {
      setReply(String(record(response.error).detail ?? "MIREYE could not answer that question."));
      return;
    }
    const result = record(response.data);
    setReply(String(result.message ?? "No answer was returned."));
    const tools = list(result.tool_trace).map((item) => words(item.tool)).join(" · ");
    setActivity(tools || undefined);
    setMessage("");
    await onRefresh?.();
  }

  return (
    <RailSection>
      <RailHeading kicker="Project assistant" title="Ask MIREYE" aside={<OriginTag origin="derived" />} />
      <AssistantAnswer text={reply} />
      {activity && <p className="mt-2 font-mono text-[9px] uppercase tracking-cite text-mi-fg-muted">Checked: {activity}</p>}
      <div className="mi-scroll mt-4 flex gap-2 overflow-x-auto">
        {prompts.map((prompt) => <TextButton key={prompt} type="button" onClick={() => setMessage(prompt)}>{prompt}</TextButton>)}
      </div>
      <form onSubmit={send} className="mt-4 space-y-3">
        <textarea
          aria-label="Ask the project assistant"
          value={message}
          onChange={(event) => setMessage(event.target.value)}
          placeholder="Ask about this project…"
          rows={3}
          className={`${fieldClass} resize-y`}
        />
        <PrimaryButton disabled={busy}>{busy ? "Investigating…" : "Ask MIREYE"}</PrimaryButton>
      </form>
    </RailSection>
  );
}

/* ─── Source coverage and user evidence ──────────────────────────────────── */

export function SourceCoverage({ sourceState }: { sourceState: Value }) {
  const sources = list(sourceState.sources);
  const available = sources.filter((source) => source.availability === "AVAILABLE").length;
  return (
    <RailSection>
      <RailHeading kicker="Source coverage" title={`${available} of ${sources.length} connected`} aside={<OriginTag origin="observed" />} />
      {sources.length ? sources.map((source, index) => (
        <Disclosure
          key={`${String(source.provider)}-${String(source.dataset)}-${index}`}
          label={`${String(source.provider)} · ${String(source.dataset)}`}
          verdict={asVerdict(source.availability)}
        >
          <p className="text-[11px] leading-relaxed text-mi-fg-muted">{String(source.reason ?? "Source responded successfully.")}</p>
          {typeof source.source_url === "string" && (
            <a href={source.source_url} target="_blank" rel="noreferrer" className="mt-2 inline-flex items-center gap-1 text-[11px] text-mi-fg">
              Open official source <ArrowUpRight className="h-3 w-3" />
            </a>
          )}
        </Disclosure>
      )) : <Quiet>Run “Refresh free sources” to check the configured official sources.</Quiet>}
    </RailSection>
  );
}

export function UserEvidence({
  projectId,
  siteId,
  requirements,
  submissions,
  onRefresh,
}: {
  projectId: string;
  siteId?: string;
  requirements: Value[];
  submissions: Value[];
  onRefresh?: () => Promise<unknown>;
}) {
  const [requirementId, setRequirementId] = useState("");
  const [title, setTitle] = useState("");
  const [provider, setProvider] = useState("");
  const [sourceUrl, setSourceUrl] = useState("");
  const [details, setDetails] = useState("");
  const [note, setNote] = useState<string>();
  const [busy, setBusy] = useState(false);
  const selectedRequirement = requirementId || String(requirements[0]?.constraint_id ?? "");

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!siteId || !selectedRequirement) return;
    setBusy(true);
    setNote(undefined);
    const response = await api.POST(
      "/v1/diligence/projects/{project_id}/sites/{site_id}/user-evidence",
      {
        params: { path: { project_id: projectId, site_id: siteId } },
        body: {
          requirement_id: selectedRequirement,
          title,
          details,
          provider,
          source_url: sourceUrl || null,
          source_type: "document",
        },
      },
    );
    setBusy(false);
    if (response.error) {
      setNote(String(record(response.error).detail ?? "Evidence could not be saved."));
      return;
    }
    setTitle("");
    setDetails("");
    setSourceUrl("");
    setNote("Saved to project memory and queued for source and scope review.");
    await onRefresh?.();
  }

  return (
    <RailSection>
      <RailHeading kicker="Your project evidence" title={`${submissions.length} submitted`} aside={<VerdictChip verdict="PARTIAL" />} />
      <Quiet>User-supplied evidence is remembered by the project but never silently promoted to verified.</Quiet>
      {submissions.map((item) => (
        <Disclosure key={String(item.evidence_id)} label={String(item.title)} verdict="PARTIAL">
          <p className="text-[11px] leading-relaxed text-mi-fg">{String(item.details)}</p>
          <p className="mt-2 font-mono text-[9px] uppercase tracking-cite text-mi-fg-muted">
            {words(item.requirement_id)} · {String(item.provider)} · pending review
          </p>
        </Disclosure>
      ))}
      <form onSubmit={submit} className="mt-4 space-y-3">
        <select aria-label="Requirement" value={selectedRequirement} onChange={(event) => setRequirementId(event.target.value)} className={fieldClass} required>
          {requirements.map((item) => <option key={String(item.constraint_id)} value={String(item.constraint_id)}>{words(item.constraint_id)}</option>)}
        </select>
        <input aria-label="Evidence title" value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Document or evidence title" className={fieldClass} required minLength={3} />
        <input aria-label="Source organization" value={provider} onChange={(event) => setProvider(event.target.value)} placeholder="Utility, authority, consultant…" className={fieldClass} required minLength={2} />
        <input aria-label="Source URL" type="url" value={sourceUrl} onChange={(event) => setSourceUrl(event.target.value)} placeholder="Source URL (optional)" className={fieldClass} />
        <textarea aria-label="Evidence details" value={details} onChange={(event) => setDetails(event.target.value)} placeholder="Paste the relevant finding, commitment, or project fact" rows={4} className={`${fieldClass} resize-y`} required />
        <PrimaryButton disabled={busy || !siteId}>{busy ? "Saving…" : "Add to project"}</PrimaryButton>
      </form>
      {note && <p role="status" className="mt-2 text-[11px] text-mi-fg-muted">{note}</p>}
    </RailSection>
  );
}

/* ─── Request drafts ──────────────────────────────────────────────────────── */

function RfiDraftEditor({ projectId, rfi, onRefresh }: { projectId: string; rfi: Value; onRefresh?: () => Promise<unknown> }) {
  const rfiId = String(rfi.rfi_id);
  const status = String(rfi.status ?? "DRAFT");
  const [recipientName, setRecipientName] = useState(String(rfi.recipient_name ?? ""));
  const [recipientContact, setRecipientContact] = useState(String(rfi.recipient_contact ?? ""));
  const [request, setRequest] = useState(String(rfi.generated_request ?? ""));
  const [notes, setNotes] = useState(String(rfi.internal_notes ?? ""));
  const [reviewer, setReviewer] = useState(String(rfi.approved_by ?? rfi.sent_by ?? ""));
  const [deliveryReference, setDeliveryReference] = useState(String(rfi.delivery_reference ?? ""));
  const [responseProvider, setResponseProvider] = useState("");
  const [responseUrl, setResponseUrl] = useState("");
  const [responseDetails, setResponseDetails] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string>();

  const errorMessage = (error: unknown) => String(record(error).detail ?? "The RFI could not be updated.");

  const updateDraft = () => api.PATCH("/v1/diligence/projects/{project_id}/rfis/{rfi_id}", {
      params: { path: { project_id: projectId, rfi_id: rfiId } },
      body: {
        generated_request: request,
        recipient_name: recipientName || null,
        recipient_contact: recipientContact || null,
        internal_notes: notes || null,
      },
    });

  async function saveDraft() {
    setBusy(true);
    const response = await updateDraft();
    setBusy(false);
    setNote(response.error ? errorMessage(response.error) : "Draft saved.");
    if (!response.error) await onRefresh?.();
  }

  async function approve() {
    setBusy(true);
    const saved = await updateDraft();
    if (saved.error) {
      setBusy(false);
      setNote(errorMessage(saved.error));
      return;
    }
    const response = await api.POST("/v1/diligence/projects/{project_id}/rfis/{rfi_id}/approve", {
      params: { path: { project_id: projectId, rfi_id: rfiId } },
      body: { approved_by: reviewer },
    });
    setBusy(false);
    setNote(response.error ? errorMessage(response.error) : "RFI approved for manual sending.");
    if (!response.error) await onRefresh?.();
  }

  async function markSent() {
    setBusy(true);
    const response = await api.POST("/v1/diligence/projects/{project_id}/rfis/{rfi_id}/sent", {
      params: { path: { project_id: projectId, rfi_id: rfiId } },
      body: { sent_by: reviewer, delivery_reference: deliveryReference || null },
    });
    setBusy(false);
    setNote(response.error ? errorMessage(response.error) : "Manual delivery recorded. Awaiting response.");
    if (!response.error) await onRefresh?.();
  }

  async function recordResponse(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    const response = await api.POST("/v1/diligence/projects/{project_id}/rfis/{rfi_id}/response", {
      params: { path: { project_id: projectId, rfi_id: rfiId } },
      body: {
        details: responseDetails,
        provider: responseProvider,
        source_url: responseUrl || null,
        source_type: "email",
      },
    });
    setBusy(false);
    setNote(response.error ? errorMessage(response.error) : "Response saved as evidence pending human review.");
    if (!response.error) await onRefresh?.();
  }

  async function copyRequest() {
    try {
      await navigator.clipboard.writeText(request);
      setNote("Request copied. Send it through your approved email or portal.");
    } catch {
      setNote("Clipboard access is unavailable. Select and copy the request text manually.");
    }
  }

  return (
    <div className="space-y-4">
      <div>
        <p className="font-mono text-[9px] uppercase tracking-cite text-mi-fg-muted">Send to</p>
        <p className="mt-1 text-[11px] text-mi-fg">{String(rfi.recipient_category ?? "Relevant authority")}</p>
      </div>
      <details>
        <summary className="cursor-pointer font-mono text-[9px] uppercase tracking-cite text-mi-fg-muted">Evidence requested</summary>
        <ul className="mt-2 list-disc space-y-1 pl-5 text-[10px] text-mi-fg-muted">
          {list(rfi.required_evidence).map((item) => <li key={String(item)}>{words(item)}</li>)}
        </ul>
      </details>

      {status === "DRAFT" && (
        <div className="space-y-3">
          <input aria-label="RFI recipient name" value={recipientName} onChange={(event) => setRecipientName(event.target.value)} placeholder="Recipient or team name" className={fieldClass} />
          <input aria-label="RFI recipient contact" value={recipientContact} onChange={(event) => setRecipientContact(event.target.value)} placeholder="Email, portal, or contact reference" className={fieldClass} />
          <textarea aria-label="RFI request" value={request} onChange={(event) => setRequest(event.target.value)} rows={10} className={`${fieldClass} resize-y`} />
          <textarea aria-label="RFI internal notes" value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="Internal context or submission instructions" rows={3} className={`${fieldClass} resize-y`} />
          <input aria-label="RFI reviewer" value={reviewer} onChange={(event) => setReviewer(event.target.value)} placeholder="Reviewed by" className={fieldClass} />
          <div className="flex flex-wrap gap-2">
            <PrimaryButton type="button" disabled={busy || request.trim().length < 40} onClick={saveDraft}>Save draft</PrimaryButton>
            <PrimaryButton type="button" disabled={busy || !recipientContact.trim() || reviewer.trim().length < 2} onClick={approve}>Approve</PrimaryButton>
            <TextButton type="button" onClick={copyRequest}>Copy request</TextButton>
          </div>
        </div>
      )}

      {status === "APPROVED" && (
        <div className="space-y-3">
          <p className="whitespace-pre-wrap text-[11px] leading-relaxed text-mi-fg">{request}</p>
          <p className="text-[10px] text-mi-fg-muted">MIREYE does not send externally. Copy this request, send it through the approved email or portal, then record that delivery below.</p>
          <input aria-label="RFI sent by" value={reviewer} onChange={(event) => setReviewer(event.target.value)} placeholder="Sent by" className={fieldClass} />
          <input aria-label="RFI delivery reference" value={deliveryReference} onChange={(event) => setDeliveryReference(event.target.value)} placeholder="Email message ID or portal ticket (optional)" className={fieldClass} />
          <div className="flex flex-wrap gap-2">
            <PrimaryButton type="button" disabled={busy || reviewer.trim().length < 2} onClick={markSent}>Mark sent</PrimaryButton>
            <TextButton type="button" onClick={copyRequest}>Copy request</TextButton>
          </div>
        </div>
      )}

      {status === "SENT" && (
        <form onSubmit={recordResponse} className="space-y-3">
          <p className="text-[10px] text-mi-fg-muted">Delivery is recorded. Add the authority response when it arrives.</p>
          <input aria-label="RFI response provider" value={responseProvider} onChange={(event) => setResponseProvider(event.target.value)} placeholder="Responding organization" className={fieldClass} required />
          <input aria-label="RFI response source URL" type="url" value={responseUrl} onChange={(event) => setResponseUrl(event.target.value)} placeholder="Email archive or portal URL (optional)" className={fieldClass} />
          <textarea aria-label="RFI response details" value={responseDetails} onChange={(event) => setResponseDetails(event.target.value)} placeholder="Paste the complete response or authoritative finding" rows={6} className={`${fieldClass} resize-y`} required />
          <PrimaryButton disabled={busy}>Record response</PrimaryButton>
        </form>
      )}

      {status === "RESPONSE_RECEIVED" && (
        <p className="text-[11px] leading-relaxed text-mi-fg">Response recorded as user-supplied evidence. It remains pending source and scope review before it can affect readiness.</p>
      )}
      {note && <p role="status" className="text-[11px] text-mi-orange-text">{note}</p>}
    </div>
  );
}

export function RfiDrafts({ project, projectId, onRefresh }: { project: Value; projectId: string; onRefresh?: () => Promise<unknown> }) {
  const rfis = list(project.rfis);
  if (!rfis.length) return null;
  return (
    <RailSection>
      <RailHeading kicker="Request drafts" title={`${rfis.length} prepared`} />
      <div>
        {rfis.map((rfi, index) => (
          <Disclosure
            key={String(rfi.rfi_id ?? index)}
            label={words(rfi.type)}
            aside={
              <span className="shrink-0 font-mono text-[9px] uppercase tracking-cite text-mi-fg-muted">
                {words(rfi.status ?? "DRAFT")}
              </span>
            }
          >
            <RfiDraftEditor projectId={projectId} rfi={rfi} onRefresh={onRefresh} />
          </Disclosure>
        ))}
      </div>
    </RailSection>
  );
}

/* ─── Composed ────────────────────────────────────────────────────────────── */

/** Project-scoped sections that read straight from the project payload. */
export function ProjectSections({ state, projectId, onRefresh }: { state: Value; projectId: string; onRefresh?: () => Promise<unknown> }) {
  const intelligence = record(state.project_intelligence);
  const power = record(intelligence.power_readiness);
  const entitlement = record(intelligence.entitlement);
  const siteId = String(record(intelligence.active_site).site_id ?? "") || undefined;
  const requirements = list(intelligence.project_requirements);
  const sourceState = siteId ? record(record(state.external_evidence_by_site)[siteId]) : {};
  const submissions = siteId ? list(record(state.user_evidence_by_site)[siteId]) : [];

  return (
    <>
      <Reveal>
        <ProjectAssistant projectId={projectId} onRefresh={onRefresh} />
      </Reveal>
      <Reveal delay={0.04}>
        <EvidenceCoverage intelligence={intelligence} />
      </Reveal>
      <Reveal delay={0.08}>
        <SourceCoverage sourceState={sourceState} />
      </Reveal>
      <Reveal delay={0.12}>
        <UserEvidence projectId={projectId} siteId={siteId} requirements={requirements} submissions={submissions} onRefresh={onRefresh} />
      </Reveal>
      <Reveal delay={0.16}>
        <ReadinessModule kicker="Power readiness" title="Grid and capacity" state={power} />
      </Reveal>
      <Reveal delay={0.2}>
        <ReadinessModule kicker="Entitlement" title="Zoning and jurisdiction" state={entitlement} />
      </Reveal>
      <Reveal delay={0.24}>
        <ProjectChanges projectId={projectId} siteId={siteId} onRefresh={onRefresh} />
      </Reveal>
      <Reveal delay={0.28}>
        <RfiDrafts project={state} projectId={projectId} onRefresh={onRefresh} />
      </Reveal>
    </>
  );
}
