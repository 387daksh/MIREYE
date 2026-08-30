"use client";

import { useEffect, useState, type ReactNode } from "react";
import { ArrowUpRight } from "lucide-react";
import { api } from "@/lib/api";
import { CountUp, Reveal } from "@/components/motion/primitives";
import {
  OriginTag,
  Quiet,
  RailHeading,
  RailSection,
  TextButton,
  VerdictChip,
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
export function ProjectChanges({ projectId }: { projectId: string }) {
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
      </div>
      {note && <p className="mt-2 text-[11px] text-mi-fg-muted">{note}</p>}
    </RailSection>
  );
}

/* ─── Request drafts ──────────────────────────────────────────────────────── */

export function RfiDrafts({ project }: { project: Value }) {
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
                Draft
              </span>
            }
          >
            <p className="whitespace-pre-wrap text-[11px] leading-relaxed text-mi-fg">
              {String(rfi.generated_request ?? "")}
            </p>
            <p className="mt-2 font-mono text-[10px] uppercase tracking-cite text-mi-orange-text">
              Draft only · approval required before sending
            </p>
          </Disclosure>
        ))}
      </div>
    </RailSection>
  );
}

/* ─── Composed ────────────────────────────────────────────────────────────── */

/** Project-scoped sections that read straight from the project payload. */
export function ProjectSections({ state, projectId }: { state: Value; projectId: string }) {
  const intelligence = record(state.project_intelligence);
  const power = record(intelligence.power_readiness);
  const entitlement = record(intelligence.entitlement);

  return (
    <>
      <Reveal>
        <EvidenceCoverage intelligence={intelligence} />
      </Reveal>
      <Reveal delay={0.06}>
        <ReadinessModule kicker="Power readiness" title="Grid and capacity" state={power} />
      </Reveal>
      <Reveal delay={0.12}>
        <ReadinessModule kicker="Entitlement" title="Zoning and jurisdiction" state={entitlement} />
      </Reveal>
      <Reveal delay={0.18}>
        <ProjectChanges projectId={projectId} />
      </Reveal>
      <Reveal delay={0.24}>
        <RfiDrafts project={state} />
      </Reveal>
    </>
  );
}
