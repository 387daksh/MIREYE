"use client";

import { useEffect, useRef, useState } from "react";
import { ArrowUpRight, X } from "lucide-react";
import { api } from "@/lib/api";
import { CountUp, Reveal, useSequence } from "@/components/motion/primitives";
import { TerminalLog } from "@/components/motion/terminal-log";
import {
  Eyebrow,
  Fact,
  GhostButton,
  OriginTag,
  PrimaryButton,
  Quiet,
  RailHeading,
  RailSection,
  TextButton,
  VerdictChip,
  type Verdict,
} from "@/components/product/ui";
import {
  arr,
  constraintName,
  num,
  rec,
  relativeTime,
  useNowSeconds,
  useSite,
  usableEvidence,
  type Value,
} from "./site-context";
import { ScenarioSection } from "./scenarios";

/** Deterministic outcomes map onto the shared verdict chips. */
const asVerdict = (outcome: unknown): Verdict => {
  const value = String(outcome ?? "").toUpperCase();
  if (value === "PASS") return "PASS";
  if (value === "FAIL" || value === "BLOCKED") return "BLOCKED";
  if (value === "PARTIAL") return "PARTIAL";
  if (value === "UNRESOLVED") return "UNRESOLVED";
  return "NOT ASSESSED";
};

/* ─── Proposed design ─────────────────────────────────────────────────────── */

/**
 * Reads the first proposed object out of the scene. Same fields the vanilla
 * syncControls() rendered: capacity, expansion target, planning envelope and
 * the campus element list with its phase.
 */
export function ProposedDesignSection() {
  const { scene } = useSite();
  const object = arr(scene?.proposed)[0];
  const geometry = rec(object?.geometry_local);
  const attributes = rec(object?.attributes);
  const components = arr(object?.components);
  const area = (num(geometry.width_m) ?? 0) * (num(geometry.length_m) ?? 0);
  const capacity = num(attributes.capacity_mw);
  const expansion = num(attributes.expansion_target_mw) ?? capacity;
  const { ref, lit } = useSequence<HTMLUListElement>(components.length, 80);

  return (
    <RailSection>
      <RailHeading
        kicker="Proposed design"
        title={object ? "Phase 1 campus" : "No proposed design"}
        aside={<OriginTag origin="proposed" />}
      />

      {object ? (
        <>
          <dl>
            <Fact
              label="Phase 1"
              value={capacity !== undefined ? <><CountUp to={capacity} /> MW</> : "—"}
              strong
            />
            <Fact
              label="Expansion target"
              value={expansion !== undefined ? <><CountUp to={expansion} /> MW</> : "—"}
              strong
            />
            <Fact
              label="Planning envelope"
              value={<><CountUp to={Math.round(area)} /> m²</>}
            />
            <Fact label="Campus elements" value={components.length} />
          </dl>

          {/* Elements arrive one at a time, like the layout being placed. The
              reveal is a colour change, not opacity — fading the row would let
              the hairline background show through as a grey slab. */}
          <ul ref={ref} className="mt-4">
            {components.map((component, index) => {
              const on = index < lit;
              return (
                <li
                  key={String(component.id ?? component.label ?? index)}
                  className="flex items-center justify-between gap-3 border-t border-mi-line py-2.5 first:border-t-0"
                >
                  <span className="flex min-w-0 items-center gap-2">
                    <span
                      aria-hidden
                      className={`h-1.5 w-1.5 shrink-0 border transition-colors duration-reveal ease-mi ${
                        on ? "border-mi-fg-strong bg-mi-fg-strong" : "border-mi-line-strong"
                      }`}
                    />
                    <span
                      className={`min-w-0 truncate text-[12px] transition-colors duration-reveal ease-mi ${
                        on ? "text-mi-fg" : "text-mi-fg-muted"
                      }`}
                    >
                      {String(component.label ?? component.id ?? "Element")}
                    </span>
                  </span>
                  <span className="shrink-0 font-mono text-[9px] uppercase tracking-cite text-mi-fg-muted">
                    {rec(component.attributes).phase === "FUTURE" ? "Future" : "Phase 1"}
                  </span>
                </li>
              );
            })}
          </ul>

          <p className="mt-4 border-t border-mi-line pt-3 text-[11px] leading-relaxed text-mi-fg-muted">
            Conceptual planning envelope and massing only. No engineering, grading,
            utility-capacity, or construction claim is implied.
          </p>
        </>
      ) : (
        <Quiet>Ask MIREYE to place a facility to generate a proposed design.</Quiet>
      )}
    </RailSection>
  );
}

/* ─── Site feasibility ────────────────────────────────────────────────────── */

/** The exact constraint set the vanilla loadFeasibility() requested. */
const REQUESTED_CONSTRAINTS = [
  { constraint_id: "footprint_inside_parcel" },
  { constraint_id: "parcel_outside_fema_sfha" },
  { constraint_id: "transmission_available_capacity_mw" },
  { constraint_id: "legal_access" },
  { constraint_id: "industrial_zoning" },
  { constraint_id: "sufficient_grid_capacity" },
  { constraint_id: "utilities_available" },
];

const EVALUATING_LOG = [
  "loading scene_state…",
  "requesting 7 constraints",
  "POST /v1/sandbox/site/evaluate",
  "awaiting deterministic outcome…",
];

/** `Nearest mapped line: 1.3 km. ` — mirrors distanceContext(). */
function distanceContext(
  snapshot: Value | undefined,
  field: string,
  label: string,
  now: number,
) {
  const raw = usableEvidence(snapshot, field, now);
  if (raw === null) return "";
  const value = Number(raw);
  return Number.isFinite(value) ? `${label}: ${(value / 1000).toFixed(1)} km. ` : "";
}

export function FeasibilitySection() {
  const { snapshotId, scene, snapshot } = useSite();
  const now = useNowSeconds();
  const [evaluation, setEvaluation] = useState<Value>();
  const [failed, setFailed] = useState(false);
  // The evaluator is deterministic, so re-running for an unchanged scene is
  // wasted spend. Key the request on the scene we last submitted.
  const submitted = useRef<Value>(undefined);

  useEffect(() => {
    if (!snapshotId || !scene || !arr(scene.proposed).length) return;
    if (submitted.current === scene) return;
    submitted.current = scene;
    setFailed(false);
    void api
      .POST("/v1/sandbox/site/{snapshot_id}/evaluate", {
        params: { path: { snapshot_id: snapshotId } },
        body: { scene_state: scene, requested_constraints: REQUESTED_CONSTRAINTS } as never,
      })
      .then((response) => {
        if (response.error) setFailed(true);
        else setEvaluation(response.data as Value);
      });
  }, [snapshotId, scene]);

  const results = Object.fromEntries(
    arr(evaluation?.constraint_results).map((item) => [String(item.constraint_id), item]),
  );
  const zoning = usableEvidence(snapshot, "parcel_zoning", now);
  const pointFlood = usableEvidence(snapshot, "within_floodplain_polygon", now);
  const water = usableEvidence(snapshot, "within_water_service_area", now);

  // Card order and copy are lifted from renderFeasibility() unchanged.
  const cards: [string, Value][] = evaluation
    ? [
        ["Land", rec(results.footprint_inside_parcel)],
        [
          "Flood",
          {
            ...rec(results.parcel_outside_fema_sfha),
            explanation: `${
              pointFlood === null
                ? "Resolution-point evidence is unavailable. "
                : `Resolution point is ${pointFlood ? "inside" : "outside"} the mapped FEMA floodplain. `
            }${rec(results.parcel_outside_fema_sfha).explanation ?? ""}`,
          },
        ],
        [
          "Transmission",
          {
            ...rec(results.transmission_available_capacity_mw),
            explanation: `${distanceContext(snapshot, "nearest_transmission_line_distance_m", "Nearest mapped line", now)}${
              rec(results.transmission_available_capacity_mw).explanation ?? ""
            }`,
          },
        ],
        [
          "Road access",
          {
            ...rec(results.legal_access),
            explanation: `${distanceContext(snapshot, "nearest_major_road_distance_m", "Nearest mapped major road", now)}${
              rec(results.legal_access).explanation ?? ""
            }`,
          },
        ],
        [
          "Zoning",
          {
            ...rec(results.industrial_zoning),
            explanation: `${zoning ? `Raw parcel code: ${zoning}. ` : "Raw parcel code is unavailable. "}${
              rec(results.industrial_zoning).explanation ?? ""
            }`,
          },
        ],
        ["Grid", rec(results.sufficient_grid_capacity)],
        [
          "Water",
          {
            ...rec(results.utilities_available),
            explanation: `${
              water === null
                ? "Mapped water service-area evidence is unavailable. "
                : `Mapped service-area flag: ${water ? "yes" : "no"}. `
            }${rec(results.utilities_available).explanation ?? ""}`,
          },
        ],
        [
          "Expansion",
          {
            outcome: "UNRESOLVED",
            explanation:
              "The 300 MW reserve is conceptual geometry; future power, water, grading, and construction feasibility are not proven.",
          },
        ],
      ]
    : [];

  return (
    <RailSection>
      <RailHeading
        kicker="Site feasibility"
        title="Current evidence"
        aside={<OriginTag origin="derived" />}
      />

      {failed && <Quiet>Site feasibility could not be evaluated for this scene.</Quiet>}

      {!failed && !evaluation && (
        <TerminalLog lines={EVALUATING_LOG} lineDelay={320} loop />
      )}

      {evaluation && (
        // Two columns only while the rail is full-width (stacked, below lg).
        // Inside the desktop rail it is always one column — 180px cards shred
        // the explanation text.
        <div className="grid grid-cols-1 gap-px bg-mi-line sm:grid-cols-2 lg:grid-cols-1">
          {cards.map(([label, item], index) => (
            <Reveal key={label} delay={index * 0.04}>
              <article className="h-full bg-mi-surface p-3">
                <div className="mb-2 flex items-start justify-between gap-2">
                  <strong className="text-[12px] font-medium text-mi-fg-strong">{label}</strong>
                  <VerdictChip verdict={asVerdict(item.outcome)} />
                </div>
                <p className="text-[11px] leading-relaxed text-mi-fg-muted">
                  {String(item.explanation ?? "")}
                </p>
              </article>
            </Reveal>
          ))}
        </div>
      )}
    </RailSection>
  );
}

/* ─── MIREYE site intelligence ────────────────────────────────────────────── */

export function IntelligenceSection({ onViewSources }: { onViewSources: () => void }) {
  const { snapshotId, snapshot, plan, reloadPlan } = useSite();
  const now = useNowSeconds();
  const [spendPlan, setSpendPlan] = useState<Value>();
  const [note, setNote] = useState<string>();
  const [busy, setBusy] = useState(false);

  const freshness = rec(plan?.freshness);
  const refreshRequired = Boolean(freshness.refresh_required);
  const evidence = rec(snapshot?.evidence);
  const planned = (plan?.fields as string[] | undefined) ?? Object.keys(evidence);

  // Verified means: present, ok, non-null, and not past its expiry.
  const verified = planned.filter((field) => {
    const record = rec(evidence[field]);
    return (
      record.status === "ok" &&
      record.value !== null &&
      record.value !== undefined &&
      Number(record.expires_at) > now
    );
  });
  const unresolved = planned.filter((field) => !verified.includes(field));
  const cached = Object.values(evidence).filter((r) => rec(r).carried_from_snapshot_id).length;

  const usable = Object.values(evidence)
    .map(rec)
    .filter((r) => r.status === "ok" && r.value !== null && r.value !== undefined);
  const strength: [string, number][] = [
    ["Verified", usable.filter((r) => r.semantic_strength === "DIRECTLY_VERIFIED").length],
    [
      "Needs interpretation",
      usable.filter((r) =>
        ["SOURCE_BACKED_SIGNAL", "DERIVED"].includes(String(r.semantic_strength)),
      ).length,
    ],
    ["Missing / unresolved", unresolved.length],
  ];
  const strengthTotal = strength.reduce((sum, [, count]) => sum + count, 0) || 1;

  const headline = refreshRequired
    ? "Update recommended"
    : `Refreshed ${relativeTime(snapshot?.observed_at, now)}`;

  const defaultNote = refreshRequired
    ? `${arr(freshness.refresh_fields).length || (freshness.refresh_fields as unknown[] | undefined)?.length || 0} of ${
        plan?.field_count ?? planned.length
      } project-relevant fields need enrichment or refresh.`
    : `${verified.length} verified · ${unresolved.length} unresolved · all results are within their freshness windows.`;

  async function quote() {
    if (!snapshotId) return;
    setBusy(true);
    setNote(undefined);
    const response = await api.POST("/v1/sandbox/site/{snapshot_id}/refresh/quote", {
      params: {
        path: { snapshot_id: snapshotId },
        query: { profile: "data_center_siting" },
      } as never,
    });
    setBusy(false);
    if (response.error) return setNote("MIREYE couldn't prepare a refresh estimate.");
    const result = rec(response.data);
    if (result.status === "NO_REFRESH_REQUIRED") {
      reloadPlan();
      return;
    }
    setSpendPlan(result);
  }

  async function confirm() {
    if (!spendPlan) return;
    setBusy(true);
    const response = await api.POST("/v1/sandbox/site/refresh/{spend_plan_id}/confirm", {
      params: { path: { spend_plan_id: String(spendPlan.spend_plan_id) } },
      body: { confirmed: true } as never,
    });
    setBusy(false);
    if (response.error) {
      setNote(
        "MIREYE couldn't refresh this site. Your previous site intelligence is still available.",
      );
      return;
    }
    setSpendPlan(undefined);
    reloadPlan();
  }

  const credits = spendPlan?.expected_credits;
  const expiry = num(spendPlan?.quote_expires_at);

  return (
    <RailSection>
      <RailHeading
        kicker="MIREYE site intelligence"
        title={headline}
        aside={
          <span className="mt-1.5 flex shrink-0 items-center gap-1.5 font-mono text-[9px] uppercase tracking-cite text-mi-fg-muted">
            <span
              aria-hidden
              className={`h-1.5 w-1.5 ${refreshRequired ? "bg-mi-orange" : "bg-mi-fg-strong"}`}
            />
            {refreshRequired ? "Stale" : "Current"}
          </span>
        }
      />

      <Quiet>{note ?? defaultNote}</Quiet>

      <dl className="mt-4">
        <Fact label="Verified" value={<CountUp to={verified.length} duration={600} />} strong />
        <Fact label="Unresolved" value={<CountUp to={unresolved.length} duration={600} />} strong />
        <Fact label="Cached" value={<CountUp to={cached} duration={600} />} />
      </dl>

      {/* Evidence strength: how much of what we hold is directly verified
          versus merely signal versus absent. */}
      <div className="mt-4 space-y-2">
        {strength.map(([label, count]) => (
          <div key={label} className="flex items-center gap-3">
            <span className="w-[124px] shrink-0 text-[11px] text-mi-fg-muted">{label}</span>
            <span className="h-1 flex-1 bg-mi-line">
              <span
                className={`block h-full transition-[width] duration-700 ease-mi ${
                  label === "Missing / unresolved" ? "bg-mi-orange" : "bg-mi-fg-strong"
                }`}
                style={{ width: `${(count / strengthTotal) * 100}%` }}
              />
            </span>
            <span className="mono-num w-6 shrink-0 text-right font-mono text-[11px] text-mi-fg">
              {count}
            </span>
          </div>
        ))}
      </div>

      <div className="mt-5 flex items-center gap-5">
        <TextButton onClick={onViewSources}>View sources</TextButton>
        <TextButton onClick={() => void quote()} disabled={busy || !refreshRequired}>
          Refresh
        </TextButton>
      </div>

      {/* Nothing is charged until this is confirmed — the spend gate is the
          product's promise, so the estimate is always shown first. */}
      {spendPlan && (
        <div className="mt-4 border border-mi-orange/50 bg-mi-surface-2 p-3">
          <p className="text-[11px] leading-relaxed text-mi-fg">
            {arr(spendPlan.requested_fields).length ||
              (spendPlan.requested_fields as unknown[] | undefined)?.length ||
              0}{" "}
            fields need an update. Estimated cost:{" "}
            <span className="mono-num font-mono text-mi-orange-text">
              {credits === null || credits === undefined
                ? "an unavailable credit estimate"
                : `${String(credits)} credits`}
            </span>
            . {expiry ? `Quote expires ${new Date(expiry * 1000).toLocaleString()}. ` : ""}
            Nothing has been charged.
          </p>
          <div className="mt-3 flex items-center gap-3">
            <PrimaryButton onClick={() => void confirm()} disabled={busy}>
              {busy ? "Refreshing…" : "Refresh with MIREYE"}
            </PrimaryButton>
            <TextButton onClick={() => setSpendPlan(undefined)} disabled={busy}>
              Not now
            </TextButton>
          </div>
        </div>
      )}
    </RailSection>
  );
}

/* ─── Physical context ────────────────────────────────────────────────────── */

export function PhysicalContextSection() {
  const { world } = useSite();
  if (!world) return null;

  const layers = arr(world.layers);
  const layer = (name: string) => layers.find((l) => l.layer === name);
  const count = (l: Value | undefined, key: string) =>
    l && l.availability === "AVAILABLE" ? String(rec(l[key]).feature_count ?? "—") : "Unavailable";

  const terrain = layer("terrain");
  const conflicts = (world.quality_conflicts as unknown[] | undefined) ?? [];
  const warnings = layers.flatMap((l) => (l.warnings as string[] | undefined) ?? []);

  return (
    <RailSection>
      <RailHeading
        kicker="Physical context"
        title="Observed layers"
        aside={<OriginTag origin="observed" />}
      />
      <dl>
        <Fact
          label="Terrain"
          value={
            terrain && terrain.availability === "AVAILABLE"
              ? `${rec(terrain.terrain).actual_resolution_m} m / ${rec(terrain.terrain).vertical_reference}`
              : "Unavailable"
          }
          strong
        />
        <Fact label="Road features" value={count(layer("roads"), "roads")} />
        <Fact label="Building footprints" value={count(layer("buildings"), "buildings")} />
        <Fact label="Water features" value={count(layer("water"), "water")} />
        <Fact label="Land-cover features" value={count(layer("land_cover"), "land_cover")} />
        <Fact label="Conflicts" value={conflicts.length} />
      </dl>
      {(conflicts.length > 0 || warnings.length > 0) && (
        <p className="mt-4 border-t border-mi-line pt-3 text-[11px] leading-relaxed text-mi-orange-text">
          {conflicts.length ? `CONFLICT: ${conflicts.join(" ")}` : warnings.join(" ")}
        </p>
      )}
    </RailSection>
  );
}

/* ─── Sources dialog ──────────────────────────────────────────────────────── */

export function SourcesDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { snapshot, world } = useSite();
  const now = useNowSeconds();
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  const rows = Object.values(rec(snapshot?.evidence))
    .map(rec)
    .sort((a, b) => String(a.field).localeCompare(String(b.field)));
  const manifest = arr(world?.source_manifest);

  return (
    <dialog
      ref={ref}
      onClose={onClose}
      onClick={(event) => {
        // Click on the backdrop (the dialog element itself) closes it.
        if (event.target === ref.current) onClose();
      }}
      className="m-auto w-[min(680px,calc(100vw-32px))] max-w-none border border-mi-line bg-mi-surface p-0 text-mi-fg shadow-panel backdrop:bg-black/70"
    >
      <div className="flex items-start justify-between gap-4 border-b border-mi-line px-5 py-4">
        <div>
          <Eyebrow>MIREYE site intelligence</Eyebrow>
          <h2 className="mt-1.5 text-[19px] font-medium tracking-tight text-mi-fg-strong">
            Sources and freshness
          </h2>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close sources"
          className="cursor-pointer p-1 text-mi-fg-muted transition-colors duration-micro ease-mi hover:text-mi-fg-strong"
        >
          <X className="h-4 w-4" strokeWidth={1.5} />
        </button>
      </div>

      <p className="border-b border-mi-line px-5 py-3 text-[12px] text-mi-fg-muted">
        Each fact retains its source, capture time, confidence, and field-level freshness.
      </p>

      <div className="mi-scroll max-h-[60vh] overflow-y-auto px-5">
        {manifest.map((entry, index) => (
          <div key={`world-${index}`} className="border-b border-mi-line py-3 last:border-b-0">
            <div className="flex items-start justify-between gap-3">
              <strong className="text-[12px] font-medium text-mi-fg-strong">
                Observed {String(entry.layer).replaceAll("_", " ")}
              </strong>
              <span className="shrink-0 font-mono text-[9px] uppercase tracking-cite text-mi-fg-muted">
                Pinned
              </span>
            </div>
            <p className="mt-1 font-mono text-[10px] text-mi-fg-muted">
              {String(rec(entry.source).provider)} ·{" "}
              {String(
                rec(entry.source).release ??
                  rec(entry.source).dataset ??
                  rec(entry.source).source_id ??
                  "",
              )}
            </p>
          </div>
        ))}

        {rows.map((record, index) => {
          const fresh =
            Number(record.expires_at) > now &&
            record.value !== null &&
            record.value !== undefined &&
            ["ok", null, undefined].includes(record.status as string);
          const strengthLabel = String(record.semantic_strength ?? "SOURCE_BACKED_SIGNAL")
            .replaceAll("_", " ")
            .toLowerCase();
          return (
            <div
              key={`ev-${String(record.field ?? index)}`}
              className="border-b border-mi-line py-3 last:border-b-0"
            >
              <div className="flex items-start justify-between gap-3">
                <strong
                  className="text-[12px] font-medium text-mi-fg-strong"
                  title={record.description ? String(record.description) : undefined}
                >
                  {constraintName(record.field)}
                </strong>
                <VerdictChip verdict={fresh ? "PASS" : "UNRESOLVED"} />
              </div>
              <p className="mt-1 font-mono text-[10px] text-mi-fg-muted">
                {strengthLabel} · {String(record.source ?? "MIREYE source")} ·{" "}
                {String(record.confidence ?? "confidence not stated")}
                {typeof record.source_url === "string" && (
                  <a
                    href={record.source_url}
                    target="_blank"
                    rel="noreferrer"
                    className="ml-2 inline-flex cursor-pointer items-center gap-0.5 text-mi-fg transition-colors duration-micro ease-mi hover:text-mi-fg-strong"
                  >
                    Source
                    <ArrowUpRight className="h-3 w-3" strokeWidth={1.5} />
                  </a>
                )}
              </p>
              <p className="mt-1 font-mono text-[10px] text-mi-fg-muted">
                {fresh ? `Current · captured ${relativeTime(record.observed_at, now)}` : "Stale or unresolved"}
              </p>
            </div>
          );
        })}

        {!manifest.length && !rows.length && (
          <p className="py-6 text-[12px] text-mi-fg-muted">No source records are attached yet.</p>
        )}
      </div>

      <div className="border-t border-mi-line px-5 py-4">
        <GhostButton onClick={onClose}>Close</GhostButton>
      </div>
    </dialog>
  );
}

/* ─── Composed rail ───────────────────────────────────────────────────────── */

/** Everything that is keyed to the SiteSnapshot rather than the project. */
export function SiteSections({ workspaceId }: { workspaceId?: string }) {
  const [sourcesOpen, setSourcesOpen] = useState(false);
  const { snapshotId } = useSite();
  if (!snapshotId) return null;
  return (
    <>
      <Reveal>
        <FeasibilitySection />
      </Reveal>
      <Reveal delay={0.06}>
        <IntelligenceSection onViewSources={() => setSourcesOpen(true)} />
      </Reveal>
      <Reveal delay={0.12}>
        <ProposedDesignSection />
      </Reveal>
      <Reveal delay={0.18}>
        <ScenarioSection workspaceId={workspaceId} />
      </Reveal>
      <Reveal delay={0.24}>
        <PhysicalContextSection />
      </Reveal>
      <SourcesDialog open={sourcesOpen} onClose={() => setSourcesOpen(false)} />
    </>
  );
}
