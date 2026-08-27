import Link from "next/link";
import { ReactNode } from "react";

export type Value = Record<string, unknown>;

export const record = (value: unknown): Value => value && typeof value === "object" && !Array.isArray(value) ? value as Value : {};
export const list = (value: unknown): Value[] => Array.isArray(value) ? value.filter((item): item is Value => Boolean(item) && typeof item === "object") : [];
const words = (value: unknown) => String(value ?? "").replaceAll("_", " ");
const title = (value: unknown) => words(value).toLowerCase().replace(/\b\w/g, (letter) => letter.toUpperCase());
const optionalNumber = (value: unknown) => value === null || value === undefined || value === "" ? undefined : Number(value);

export function MireyeHeader({ projectName, workspace }: { projectName: string; workspace?: string }) {
  return <header className="mireye-header">
    <Link href="/" className="brand" aria-label="MIREYE home"><span className="brand-mark" aria-hidden="true"/>MIREYE</Link>
    <div className="header-project"><span>Current project</span><strong>{projectName}</strong></div>
    <nav className="header-actions" aria-label="Workspace controls">
      <Link href="/" className="text-action">New search</Link>
      <span className="workspace-chip">{workspace ?? "Workspace"}</span>
      <span className="user-mark" aria-label="Current user">ME</span>
    </nav>
  </header>;
}

export function ProjectHeader({ state }: { state: Value }) {
  const request = record(state.request);
  const runs = list(state.orchestration_runs);
  const spec = record(runs.at(-1)?.project_spec);
  const project = title(spec.project_type || request.project || "Project");
  const initial = optionalNumber(spec.initial_capacity_mw ?? request.capacity_mw);
  const expansion = optionalNumber(spec.expansion_capacity_mw ?? record(request.power_requirements).expansion_mw);
  const site = String(record(state.project_intelligence).active_site ? record(record(state.project_intelligence).active_site).title : "Site pending");
  const readiness = words(record(state.project_intelligence).project_readiness_state || "Assessing");
  return <section className="project-header">
    <div><span className="eyebrow">Project</span><h1>{initial !== undefined && Number.isFinite(initial) ? `${initial} MW ` : ""}{project}</h1></div>
    <dl className="project-facts">
      <div><dt>Phase 1</dt><dd>{initial !== undefined && Number.isFinite(initial) ? `${initial} MW` : "—"}</dd></div>
      <div><dt>Expansion</dt><dd>{expansion !== undefined && Number.isFinite(expansion) ? `${expansion} MW` : "Not set"}</dd></div>
      <div><dt>Site</dt><dd>{site}</dd></div>
    </dl>
    <span className={`project-state state-${readiness.toLowerCase()}`}>{title(readiness)}</span>
  </section>;
}

export function MapWorkspace({ children }: { children: ReactNode }) {
  return <section className="map-workspace" aria-label="Physical-world workspace">{children}</section>;
}

export function SiteSummary({ intelligence, candidate }: { intelligence: Value; candidate?: Value }) {
  const site = record(intelligence.active_site);
  const summary = record(candidate?.summary);
  const area = Number(summary.area_acres);
  return <section className="intel-section site-summary">
    <span className="eyebrow">Site intelligence</span>
    <h2>{String(site.title ?? summary.title ?? candidate?.address ?? "Selected site")}</h2>
    <div className="site-metrics"><strong>{Number.isFinite(area) ? area.toLocaleString(undefined, { maximumFractionDigits: 1 }) : "—"}</strong><span>acres</span></div>
    <p className="metadata-line"><span className="observed-dot"/> {title(candidate?.reconciliation_status ?? "Site selected")}</p>
  </section>;
}

const readinessOrder = ["Land", "Flood", "Power", "Entitlement", "Water", "Access"];
const statusLabel = (value: unknown) => {
  const status = String(value ?? "NOT_ASSESSED").toUpperCase();
  if (status === "READY" || status === "PASS") return "PASS";
  if (status === "CRITICAL" || status === "BLOCKED" || status === "FAIL") return "BLOCKED";
  if (status === "PARTIAL") return "PARTIAL";
  if (status === "UNRESOLVED" || status === "UNAVAILABLE") return "UNRESOLVED";
  return "NOT ASSESSED";
};

export function ReadinessGrid({ intelligence }: { intelligence: Value }) {
  const readiness = record(intelligence.readiness);
  return <section className="intel-section">
    <h3>Project readiness</h3>
    <dl className="readiness-grid">{readinessOrder.map((domain) => {
      const status = statusLabel(record(readiness[domain]).status);
      return <div key={domain}><dt>{domain}</dt><dd className={`readiness-${status.toLowerCase().replace(" ", "-")}`}>{status}</dd></div>;
    })}</dl>
  </section>;
}

export function BlockerList({ intelligence }: { intelligence: Value }) {
  const blockers = list(intelligence.unresolved_issues).filter((item) => item.blocking !== false);
  return <section className="intel-section"><h3>Critical blockers</h3>
    {blockers.length ? <ul className="blocker-list">{blockers.map((item) => <li key={String(item.gap_id)}><span>{String(item.title)}</span><small>{String(item.domain ?? "Project")}</small></li>)}</ul> : <p className="quiet">No critical blockers recorded.</p>}
  </section>;
}

export function NextActionList({ intelligence }: { intelligence: Value }) {
  const actions = list(intelligence.recommended_actions).slice(0, 3);
  return <section className="intel-section"><h3>What should happen next</h3>
    {actions.length ? <ol className="next-actions">{actions.map((item, index) => <li key={String(item.action_id ?? item.title)}><span>{index + 1}</span>{String(item.title)}</li>)}</ol> : <p className="quiet">No next action is currently recommended.</p>}
  </section>;
}

export function EvidenceDetail({ evidence }: { evidence: Value }) {
  const observed = Number(evidence.observed_at);
  return <article className="evidence-detail">
    <div><strong>{words(evidence.evidence_id)}</strong><span className="semantic-tag observed">Observed</span></div>
    <dl><div><dt>Source</dt><dd>{String(evidence.source ?? evidence.provider ?? "—")}</dd></div><div><dt>Scope</dt><dd>{words(evidence.scope || "—")}</dd></div><div><dt>Freshness</dt><dd>{evidence.status === "ok" ? "Current" : title(evidence.status ?? "Unknown")}</dd></div><div><dt>Timestamp</dt><dd>{Number.isFinite(observed) ? new Date(observed * 1000).toLocaleString() : "—"}</dd></div></dl>
    {typeof evidence.source_url === "string" && <a href={evidence.source_url} target="_blank" rel="noreferrer">View source <span aria-hidden="true">↗</span></a>}
  </article>;
}

export function SourceDrawer({ intelligence }: { intelligence: Value }) {
  const evidence = list(intelligence.evidence_items);
  return <details className="source-drawer"><summary>Sources / evidence</summary><div className="source-drawer-content">
    {evidence.slice(0, 8).map((item) => <EvidenceDetail key={String(item.evidence_id)} evidence={item}/>)}
    {evidence.length > 8 && <p className="quiet">Showing 8 of {evidence.length} source records.</p>}
  </div></details>;
}

export function EvidenceSummary({ intelligence }: { intelligence: Value }) {
  const evidence = list(intelligence.evidence_items);
  const usable = evidence.filter((item) => item.status === "ok").length;
  const unresolved = list(intelligence.unresolved_issues).filter((item) => item.status === "OPEN").length;
  const freshness = String(record(intelligence.power_readiness).freshness ?? "UNKNOWN");
  return <section className="intel-section evidence-summary"><h3>Evidence</h3>
    <div className="evidence-count"><strong>{evidence.length}</strong><span>records</span></div>
    <div className="evidence-meta"><span>{usable} usable</span><span>{unresolved} unresolved</span><span>{title(freshness)}</span></div>
    <SourceDrawer intelligence={intelligence}/>
  </section>;
}

export function IntelligencePanel({ state }: { state: Value }) {
  const intelligence = record(state.project_intelligence);
  const candidates = list(state.candidates);
  const activeId = String(state.active_candidate_id ?? record(intelligence.active_site).candidate_id ?? "");
  const candidate = candidates.find((item) => String(item.candidate_id) === activeId) ?? candidates[0];
  return <aside className="intelligence-panel" aria-label="Project intelligence">
    <SiteSummary intelligence={intelligence} candidate={candidate}/>
    <ReadinessGrid intelligence={intelligence}/>
    <BlockerList intelligence={intelligence}/>
    <NextActionList intelligence={intelligence}/>
    <EvidenceSummary intelligence={intelligence}/>
  </aside>;
}
