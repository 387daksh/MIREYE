"use client";

import Link from "next/link";
import { ReactNode } from "react";
import { ArrowUpRight } from "lucide-react";
import { ThemeToggle } from "@/components/theme";
import { CountUp, Metric, Reveal, useSequence } from "@/components/motion/primitives";
import {
  Brand,
  Eyebrow,
  Fact,
  OriginTag,
  Quiet,
  RailHeading,
  RailSection,
  VerdictChip,
  type Verdict,
} from "@/components/product/ui";

export type Value = Record<string, unknown>;

export const record = (value: unknown): Value =>
  value && typeof value === "object" && !Array.isArray(value) ? (value as Value) : {};
export const list = (value: unknown): Value[] =>
  Array.isArray(value)
    ? value.filter((item): item is Value => Boolean(item) && typeof item === "object")
    : [];
const words = (value: unknown) => String(value ?? "").replaceAll("_", " ");

/**
 * Acronyms the backend stores lowercase in snake_case keys. Without this,
 * `bess_export_interconnection` title-cases with the expected acronyms. Display only — the
 * underlying values are untouched.
 */
const ACRONYMS = new Set([
  "ai",
  "mw",
  "mwh",
  "kv",
  "bess",
  "pcs",
  "iso",
  "rto",
  "hvac",
  "roi",
  "api",
  "rfi",
  "apn",
  "gis",
]);

const title = (value: unknown) =>
  words(value)
    .toLowerCase()
    .replace(/\b[\w']+/g, (word) =>
      ACRONYMS.has(word) ? word.toUpperCase() : word.charAt(0).toUpperCase() + word.slice(1),
    );
const optionalNumber = (value: unknown) =>
  value === null || value === undefined || value === "" ? undefined : Number(value);

/* ─── Header ──────────────────────────────────────────────────────────────── */

export function MireyeHeader({
  projectName,
  workspace,
}: {
  projectName: string;
  workspace?: string;
}) {
  return (
    <header className="flex h-14 shrink-0 items-center gap-4 border-b border-mi-line bg-mi-surface px-4 md:px-6">
      <Link href="/" className="shrink-0" aria-label="MIREYE home">
        <Brand />
      </Link>

      <div className="mx-auto hidden min-w-0 items-baseline gap-3 md:flex">
        <span className="shrink-0 font-mono text-[10px] uppercase tracking-cite text-mi-fg-muted">
          Current project
        </span>
        <strong className="truncate text-[13px] font-medium text-mi-fg-strong">
          {projectName}
        </strong>
      </div>

      <nav className="ml-auto flex items-center gap-3 md:ml-0" aria-label="Workspace controls">
        <Link
          href="/"
          className="group relative cursor-pointer py-1 font-mono text-[11px] uppercase tracking-cite text-mi-fg-muted transition-colors duration-micro ease-mi hover:text-mi-fg-strong"
        >
          New search
          <span
            aria-hidden
            className="absolute inset-x-0 bottom-0 h-px origin-left scale-x-0 bg-mi-orange transition-transform duration-micro ease-mi group-hover:scale-x-100"
          />
        </Link>
        <span className="hidden max-w-[160px] truncate border border-mi-line px-2.5 py-1.5 font-mono text-[10px] text-mi-fg-muted lg:inline">
          {workspace ?? "Workspace"}
        </span>
        <ThemeToggle />
        <span
          className="grid h-7 w-7 shrink-0 place-items-center rounded-full border border-mi-fg-strong font-mono text-[9px] font-semibold text-mi-fg-strong"
          aria-label="Current user"
        >
          ME
        </span>
      </nav>
    </header>
  );
}

/* ─── Project header ──────────────────────────────────────────────────────── */

/** Readiness words that mean the project cannot proceed. */
const CRITICAL_READINESS = new Set(["blocked", "critical"]);

export function ProjectHeader({ state }: { state: Value }) {
  const request = record(state.request);
  const runs = list(state.orchestration_runs);
  const spec = record(runs.at(-1)?.project_spec);
  const project = title(spec.project_type || request.project || "Project");
  const initial = optionalNumber(spec.initial_capacity_mw ?? request.capacity_mw);
  const expansion = optionalNumber(
    spec.expansion_capacity_mw ?? record(request.power_requirements).expansion_mw,
  );
  const site = String(
    record(state.project_intelligence).active_site
      ? record(record(state.project_intelligence).active_site).title
      : "Site pending",
  );
  const readiness = words(record(state.project_intelligence).project_readiness_state || "Assessing");
  const critical = CRITICAL_READINESS.has(readiness.toLowerCase());

  const hasInitial = initial !== undefined && Number.isFinite(initial);
  const hasExpansion = expansion !== undefined && Number.isFinite(expansion);

  return (
    <section className="flex shrink-0 flex-wrap items-center gap-x-8 gap-y-4 border-b border-mi-line bg-mi-surface px-4 py-4 md:px-6">
      <div className="min-w-[220px] flex-1">
        <Eyebrow>Project</Eyebrow>
        <h1 className="mt-1.5 text-[clamp(22px,2.2vw,30px)] font-medium leading-[1.05] tracking-tight text-mi-fg-strong">
          {hasInitial ? `${initial} MW ` : ""}
          {project}
        </h1>
      </div>

      <dl className="flex flex-wrap items-start gap-x-0 gap-y-3">
        {[
          // Capacities count up; the site name is text and simply renders.
          { label: "Phase 1", value: hasInitial ? initial : undefined, fallback: "—", unit: "MW" },
          {
            label: "Expansion",
            value: hasExpansion ? expansion : undefined,
            fallback: "Not set",
            unit: "MW",
          },
          { label: "Site", value: undefined, fallback: site },
        ].map((item) => (
          <div
            key={item.label}
            className="min-w-[110px] max-w-[220px] border-l border-mi-line px-4 first:border-l-0 first:pl-0"
          >
            <dt className="font-mono text-[9px] uppercase tracking-cite text-mi-fg-muted">
              {item.label}
            </dt>
            <dd className="mono-num mt-1.5 truncate font-mono text-[12px] font-medium text-mi-fg-strong">
              {item.value !== undefined ? (
                <>
                  <CountUp to={item.value} duration={700} /> {item.unit}
                </>
              ) : (
                item.fallback
              )}
            </dd>
          </div>
        ))}
      </dl>

      <span
        className={`hidden shrink-0 border px-2.5 py-1.5 font-mono text-[10px] uppercase tracking-cite sm:inline ${
          critical ? "border-mi-orange text-mi-orange-text" : "border-mi-line text-mi-fg-muted"
        }`}
      >
        {title(readiness)}
      </span>
    </section>
  );
}

/* ─── Map shell ───────────────────────────────────────────────────────────── */

export function MapWorkspace({ children }: { children: ReactNode }) {
  return (
    <section
      className="relative min-w-0 overflow-hidden border-mi-line bg-mi-surface-2 lg:border-r"
      aria-label="Physical-world workspace"
    >
      {children}
    </section>
  );
}

/* ─── Intelligence rail ───────────────────────────────────────────────────── */

export function SiteSummary({
  intelligence,
  candidate,
}: {
  intelligence: Value;
  candidate?: Value;
}) {
  const site = record(intelligence.active_site);
  const summary = record(candidate?.summary);
  const area = Number(summary.area_acres);
  return (
    <RailSection>
      <RailHeading
        kicker="Site intelligence"
        title={String(site.title ?? summary.title ?? candidate?.address ?? "Selected site")}
        aside={<OriginTag origin="observed" />}
      />
      <Metric value={area} unit="acres" decimals={1} />
      <p className="mt-3 flex items-center gap-2 font-mono text-[10px] uppercase tracking-cite text-mi-fg-muted">
        <span aria-hidden className="h-1.5 w-1.5 bg-mi-observed" />
        {title(candidate?.reconciliation_status ?? "Site selected")}
      </p>
    </RailSection>
  );
}

const readinessOrder = ["Land", "Flood", "Power", "Entitlement", "Water", "Access"];

const statusLabel = (value: unknown): Verdict => {
  const status = String(value ?? "NOT_ASSESSED").toUpperCase();
  if (status === "READY" || status === "PASS") return "PASS";
  if (status === "CRITICAL" || status === "BLOCKED" || status === "FAIL") return "BLOCKED";
  if (status === "PARTIAL") return "PARTIAL";
  if (status === "UNRESOLVED" || status === "UNAVAILABLE") return "UNRESOLVED";
  return "NOT ASSESSED";
};

/**
 * Readiness domains resolve one at a time rather than appearing all at once —
 * the checklist visibly runs, which is what the evaluator is actually doing.
 * Values are read straight from `intelligence.readiness`; only the reveal is new.
 */
export function ReadinessGrid({ intelligence }: { intelligence: Value }) {
  const readiness = record(intelligence.readiness);
  const { ref, lit } = useSequence<HTMLDListElement>(readinessOrder.length, 110);
  return (
    <RailSection>
      <RailHeading
        kicker="Project readiness"
        title={
          <span className="mono-num">
            <CountUp to={lit} duration={0} />
            <span className="text-mi-fg-muted"> / {readinessOrder.length} domains</span>
          </span>
        }
        aside={<OriginTag origin="derived" />}
      />
      <dl ref={ref} className="grid grid-cols-1 gap-px bg-mi-line sm:grid-cols-2">
        {readinessOrder.map((domain, index) => {
          const status = statusLabel(record(readiness[domain]).status);
          const on = index < lit;
          return (
            <div
              key={domain}
              className="flex items-center justify-between gap-3 bg-mi-surface px-3 py-3"
            >
              {/* The cell stays opaque; the pending chip carries the "still
                  running" state, so the hairline grid never shows through. */}
              <dt
                className={`text-[12px] transition-colors duration-reveal ease-mi ${
                  on ? "text-mi-fg" : "text-mi-fg-muted"
                }`}
              >
                {domain}
              </dt>
              <dd>
                {on ? (
                  <VerdictChip verdict={status} />
                ) : (
                  <span className="inline-flex items-center gap-1.5 border border-mi-line px-2 py-1 font-mono text-[9px] uppercase tracking-cite text-mi-fg-muted">
                    <span aria-hidden className="h-1.5 w-1.5 animate-caret bg-mi-orange" />
                    …
                  </span>
                )}
              </dd>
            </div>
          );
        })}
      </dl>
    </RailSection>
  );
}

export function BlockerList({ intelligence }: { intelligence: Value }) {
  const blockers = list(intelligence.unresolved_issues).filter((item) => item.blocking !== false);
  return (
    <RailSection>
      <RailHeading kicker="Critical blockers" title={`${blockers.length} open`} />
      {blockers.length ? (
        <ul className="space-y-px bg-mi-line">
          {blockers.map((item) => (
            <li
              key={String(item.gap_id)}
              className="flex items-start justify-between gap-3 bg-mi-surface py-3"
            >
              <span className="text-[12px] leading-snug text-mi-fg">{String(item.title)}</span>
              <span className="shrink-0 font-mono text-[9px] uppercase tracking-cite text-mi-orange-text">
                {String(item.domain ?? "Project")}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <Quiet>No critical blockers recorded.</Quiet>
      )}
    </RailSection>
  );
}

export function NextActionList({ intelligence }: { intelligence: Value }) {
  const actions = list(intelligence.recommended_actions).slice(0, 3);
  return (
    <RailSection>
      <RailHeading kicker="What should happen next" title="Recommended actions" />
      {actions.length ? (
        <ol className="space-y-3">
          {actions.map((item, index) => (
            <li
              key={String(item.action_id ?? item.title)}
              className="grid grid-cols-[22px_1fr] gap-2 text-[12px] leading-snug text-mi-fg"
            >
              <span className="mono-num font-mono text-[11px] text-mi-orange-text">
                {String(index + 1).padStart(2, "0")}
              </span>
              {String(item.title)}
            </li>
          ))}
        </ol>
      ) : (
        <Quiet>No next action is currently recommended.</Quiet>
      )}
    </RailSection>
  );
}

export function EvidenceDetail({ evidence }: { evidence: Value }) {
  const observed = Number(evidence.observed_at);
  return (
    <article className="border-t border-mi-line py-3 first:border-t-0">
      <div className="flex items-start justify-between gap-3">
        <strong className="min-w-0 break-words font-mono text-[10px] font-medium text-mi-fg-strong">
          {words(evidence.evidence_id)}
        </strong>
        <OriginTag origin="observed" />
      </div>
      <dl className="mt-2.5 grid grid-cols-2 gap-x-4 gap-y-2">
        {[
          { label: "Source", value: String(evidence.source ?? evidence.provider ?? "—") },
          { label: "Scope", value: words(evidence.scope || "—") },
          {
            label: "Freshness",
            value: evidence.status === "ok" ? "Current" : title(evidence.status ?? "Unknown"),
          },
          {
            label: "Timestamp",
            value: Number.isFinite(observed) ? new Date(observed * 1000).toLocaleString() : "—",
          },
        ].map((item) => (
          <div key={item.label} className="min-w-0">
            <dt className="font-mono text-[8px] uppercase tracking-cite text-mi-fg-muted">
              {item.label}
            </dt>
            <dd className="mono-num mt-0.5 truncate font-mono text-[10px] text-mi-fg">
              {item.value}
            </dd>
          </div>
        ))}
      </dl>
      {typeof evidence.source_url === "string" && (
        <a
          href={evidence.source_url}
          target="_blank"
          rel="noreferrer"
          className="mt-2.5 inline-flex cursor-pointer items-center gap-1 font-mono text-[10px] text-mi-fg-muted transition-colors duration-micro ease-mi hover:text-mi-fg-strong"
        >
          View source
          <ArrowUpRight className="h-3 w-3" strokeWidth={1.5} />
        </a>
      )}
    </article>
  );
}

export function SourceDrawer({ intelligence }: { intelligence: Value }) {
  const evidence = list(intelligence.evidence_items);
  return (
    <details className="group mt-4 border-t border-mi-line">
      <summary className="cursor-pointer list-none py-3 font-mono text-[10px] uppercase tracking-cite text-mi-fg-muted transition-colors duration-micro ease-mi hover:text-mi-fg-strong">
        <span className="mr-1.5 inline-block transition-transform duration-micro ease-mi group-open:rotate-90">
          ›
        </span>
        Sources / evidence
      </summary>
      <div className="pb-1">
        {evidence.slice(0, 8).map((item) => (
          <EvidenceDetail key={String(item.evidence_id)} evidence={item} />
        ))}
        {evidence.length > 8 && (
          <p className="pt-3 font-mono text-[10px] text-mi-fg-muted">
            Showing 8 of {evidence.length} source records.
          </p>
        )}
      </div>
    </details>
  );
}

export function EvidenceSummary({ intelligence }: { intelligence: Value }) {
  const evidence = list(intelligence.evidence_items);
  const usable = evidence.filter((item) => item.status === "ok").length;
  const unresolved = list(intelligence.unresolved_issues).filter(
    (item) => item.status === "OPEN",
  ).length;
  const freshness = String(record(intelligence.power_readiness).freshness ?? "UNKNOWN");
  return (
    <RailSection>
      <RailHeading kicker="Evidence" title={<Metric value={evidence.length} unit="records" />} />
      <dl>
        <Fact label="Usable" value={<CountUp to={usable} duration={600} />} strong />
        <Fact label="Unresolved" value={<CountUp to={unresolved} duration={600} />} strong />
        <Fact label="Freshness" value={title(freshness)} />
      </dl>
      {/* Evidence strength: usable records as a share of the whole set. */}
      <div className="mt-4">
        <div className="flex h-1 w-full bg-mi-line">
          <div
            className="h-full bg-mi-orange transition-[width] duration-700 ease-mi"
            style={{ width: evidence.length ? `${(usable / evidence.length) * 100}%` : "0%" }}
          />
        </div>
        <p className="mt-2 font-mono text-[9px] uppercase tracking-cite text-mi-fg-muted">
          Evidence strength
        </p>
      </div>
      <SourceDrawer intelligence={intelligence} />
    </RailSection>
  );
}

/**
 * The project rail. `children` carries the snapshot-scoped sections (feasibility,
 * intelligence, proposed design, physical context), which are rendered after the
 * project-level ones so the rail reads project → site → design.
 */
export function IntelligencePanel({
  state,
  children,
}: {
  state: Value;
  children?: ReactNode;
}) {
  const intelligence = record(state.project_intelligence);
  const candidates = list(state.candidates);
  const activeId = String(
    state.active_candidate_id ?? record(intelligence.active_site).candidate_id ?? "",
  );
  const candidate =
    candidates.find((item) => String(item.candidate_id) === activeId) ?? candidates[0];
  return (
    <aside
      className="mi-scroll min-h-0 overflow-y-auto border-t border-mi-line bg-mi-surface lg:border-t-0"
      aria-label="Project intelligence"
    >
      {/* Sections arrive in reading order, 60ms apart. */}
      {[
        <SiteSummary key="site" intelligence={intelligence} candidate={candidate} />,
        <ReadinessGrid key="readiness" intelligence={intelligence} />,
        <BlockerList key="blockers" intelligence={intelligence} />,
        <NextActionList key="actions" intelligence={intelligence} />,
        <EvidenceSummary key="evidence" intelligence={intelligence} />,
      ].map((section, index) => (
        <Reveal key={section.key} delay={index * 0.06}>
          {section}
        </Reveal>
      ))}
      {children}
    </aside>
  );
}
