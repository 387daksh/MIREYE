"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import {
  Eyebrow,
  FieldLabel,
  GhostButton,
  Quiet,
  RailHeading,
  RailSection,
  VerdictChip,
  fieldClass,
  type Verdict,
} from "@/components/product/ui";
import { arr, constraintName, num, rec, useSite, type Value } from "./site-context";

const asVerdict = (value: unknown): Verdict => {
  const v = String(value ?? "").toUpperCase();
  if (v === "PASS") return "PASS";
  if (v === "FAIL" || v === "BLOCKED") return "BLOCKED";
  if (v === "PARTIAL") return "PARTIAL";
  return "UNRESOLVED";
};

/** Planning envelope in m², from a scenario's first proposed object. */
function envelope(scenario: Value | undefined): string {
  const object = arr(rec(scenario?.scene_state).proposed)[0];
  const geometry = rec(object?.geometry_local);
  const area = (num(geometry.width_m) ?? 0) * (num(geometry.length_m) ?? 0);
  return `${Math.round(area).toLocaleString()} m²`;
}

function capacity(scenario: Value | undefined): string {
  const object = arr(rec(scenario?.scene_state).proposed)[0];
  const mw = num(rec(object?.attributes).capacity_mw);
  return mw === undefined ? "—" : `${mw} MW`;
}

/**
 * Design options: save the current scene as a named scenario, branch it, load a
 * saved one back onto the map, and compare two under the deterministic
 * evaluator. Every outcome shown here comes from the backend — the browser
 * never decides which option is stronger.
 */
export function ScenarioSection({ workspaceId }: { workspaceId?: string }) {
  const { snapshotId, worldId, scene, setScene } = useSite();
  const [saved, setSaved] = useState<Value[]>([]);
  const [activeId, setActiveId] = useState<string>();
  const [compareId, setCompareId] = useState<string>("");
  const [comparison, setComparison] = useState<Value>();
  const [intent, setIntent] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string>();

  const active = saved.find((s) => String(s.scenario_id) === activeId);
  const other = saved.find((s) => String(s.scenario_id) === compareId);
  const label = (scenario: Value | undefined, fallback: string) =>
    String(scenario?.user_intent || fallback);

  function register(scenario: Value) {
    const id = String(scenario.scenario_id);
    setSaved((items) => [...items.filter((s) => String(s.scenario_id) !== id), scenario]);
    setActiveId(id);
  }

  async function save() {
    if (!snapshotId || !scene) return;
    setBusy(true);
    setNote(undefined);
    const response = await api.POST("/v1/sandbox/{snapshot_id}/scenarios", {
      params: { path: { snapshot_id: snapshotId } },
      body: {
        workspace_id: workspaceId ?? "local",
        user_intent: intent.trim() || "Saved scene",
        scene_state: scene,
        requested_constraints: [],
        world_snapshot_id: worldId,
        model_id: null,
      } as never,
    });
    setBusy(false);
    if (response.error) return setNote("Scenario save failed.");
    register(rec(response.data));
  }

  async function branch() {
    if (!activeId) return;
    setBusy(true);
    setNote(undefined);
    const response = await api.POST("/v1/sandbox/scenarios/{scenario_id}/branch", {
      params: { path: { scenario_id: activeId } },
      body: { user_intent: intent.trim() || "Branched option" } as never,
    });
    setBusy(false);
    if (response.error) return setNote("Scenario branch failed.");
    const scenario = rec(response.data);
    // Branching returns a new scene; put it on the map, as the vanilla did.
    if (scenario.scene_state) setScene(rec(scenario.scene_state));
    register(scenario);
  }

  async function select(id: string) {
    if (!id) return;
    setBusy(true);
    setNote(undefined);
    const response = await api.GET("/v1/sandbox/scenarios/{scenario_id}", {
      params: { path: { scenario_id: id } },
    });
    setBusy(false);
    if (response.error) return setNote("Scenario load failed.");
    const scenario = rec(response.data);
    if (scenario.scene_state) setScene(rec(scenario.scene_state));
    register(scenario);
  }

  async function compare() {
    if (!activeId || !compareId || activeId === compareId) return;
    setBusy(true);
    setNote(undefined);
    const response = await api.POST("/v1/sandbox/compare", {
      body: { left_scenario_id: activeId, right_scenario_id: compareId } as never,
    });
    setBusy(false);
    if (response.error) return setNote("Scenario comparison failed.");
    setComparison(rec(response.data));
  }

  const leftName = label(active, "Option A");
  const rightName = label(other, "Option B");

  // Planning rows are only meaningful when both scenes are in hand.
  const planningRows: [string, string, string][] =
    active && other
      ? [
          ["Capacity", capacity(active), capacity(other)],
          ["Land envelope", envelope(active), envelope(other)],
          [
            "Evaluation",
            String(rec(active.evaluation).overall_status ?? "—"),
            String(rec(other.evaluation).overall_status ?? "—"),
          ],
        ]
      : [];

  const changed = Object.entries(rec(comparison?.constraint_changes)).map(([id, value]) => {
    const v = rec(value);
    return [
      constraintName(id),
      String(rec(v.before).outcome ?? "—"),
      String(rec(v.after).outcome ?? "—"),
    ] as [string, string, string];
  });

  const rows = [
    ...planningRows,
    ...(changed.length ? changed : ([["Constraint outcomes", "No change", "No change"]] as const)),
  ];

  const dominance = String(rec(comparison?.dominance).result ?? "");
  const summary = comparison
    ? dominance === "neither"
      ? `${((comparison.what_changed as string[] | undefined) ?? []).join(" ")} Neither option dominates under the current deterministic evaluation.`
      : `${dominance === "left" ? leftName : rightName} is stronger under the matching evaluated requirements.`
    : undefined;

  if (!snapshotId) return null;

  return (
    <RailSection>
      <RailHeading
        kicker="Design options"
        title={active ? leftName : "Current option"}
        aside={
          <span className="mono-num shrink-0 font-mono text-[9px] uppercase tracking-cite text-mi-fg-muted">
            {active ? `rev ${String(active.revision ?? 1)}` : "Unsaved"}
          </span>
        }
      />

      <FieldLabel htmlFor="scenario-name">Option name</FieldLabel>
      <input
        id="scenario-name"
        value={intent}
        onChange={(event) => setIntent(event.target.value)}
        placeholder="Scenario A"
        className={fieldClass}
      />

      <div className="mt-3 flex gap-2">
        <GhostButton onClick={() => void save()} disabled={busy || !scene} className="flex-1">
          Save option
        </GhostButton>
        <GhostButton onClick={() => void branch()} disabled={busy || !activeId} className="flex-1">
          Try another
        </GhostButton>
      </div>

      {saved.length > 0 && (
        <>
          <div className="mt-5">
            <FieldLabel htmlFor="scenario-current">Current option</FieldLabel>
            <select
              id="scenario-current"
              value={activeId ?? ""}
              onChange={(event) => void select(event.target.value)}
              className={`${fieldClass} cursor-pointer`}
            >
              <option value="">Current session</option>
              {saved.map((scenario, index) => (
                <option key={String(scenario.scenario_id)} value={String(scenario.scenario_id)}>
                  {label(scenario, `Option ${index + 1}`)}
                </option>
              ))}
            </select>
          </div>

          <div className="mt-3">
            <FieldLabel htmlFor="scenario-compare">Compare with</FieldLabel>
            <select
              id="scenario-compare"
              value={compareId}
              onChange={(event) => setCompareId(event.target.value)}
              className={`${fieldClass} cursor-pointer`}
            >
              <option value="">Choose an option</option>
              {saved
                .filter((scenario) => String(scenario.scenario_id) !== activeId)
                .map((scenario, index) => (
                  <option key={String(scenario.scenario_id)} value={String(scenario.scenario_id)}>
                    {label(scenario, `Option ${index + 1}`)}
                  </option>
                ))}
            </select>
          </div>

          <GhostButton
            onClick={() => void compare()}
            disabled={busy || !activeId || !compareId}
            className="mt-3 w-full"
          >
            Compare options
          </GhostButton>
        </>
      )}

      {note && <p className="mt-3 text-[11px] text-mi-orange-text">{note}</p>}

      {comparison && (
        <div className="mt-5">
          <Eyebrow className="mb-2">Comparison</Eyebrow>
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-left">
              <thead>
                <tr>
                  <th className="py-2 font-mono text-[9px] uppercase tracking-cite text-mi-fg-muted" />
                  <th className="py-2 font-mono text-[9px] uppercase tracking-cite text-mi-fg-muted">
                    {leftName}
                  </th>
                  <th className="py-2 font-mono text-[9px] uppercase tracking-cite text-mi-fg-muted">
                    {rightName}
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map(([name, left, right], index) => (
                  <tr key={`${name}-${index}`} className="border-t border-mi-line">
                    <td className="py-2 pr-3 text-[11px] text-mi-fg-muted">{name}</td>
                    <td className="py-2 pr-3">
                      {["PASS", "FAIL", "UNRESOLVED", "PARTIAL"].includes(left.toUpperCase()) ? (
                        <VerdictChip verdict={asVerdict(left)} />
                      ) : (
                        <span className="mono-num font-mono text-[11px] text-mi-fg">{left}</span>
                      )}
                    </td>
                    <td className="py-2">
                      {["PASS", "FAIL", "UNRESOLVED", "PARTIAL"].includes(right.toUpperCase()) ? (
                        <VerdictChip verdict={asVerdict(right)} />
                      ) : (
                        <span className="mono-num font-mono text-[11px] text-mi-fg">{right}</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {summary && (
            <p className="mt-3 border-t border-mi-line pt-3 text-[11px] leading-relaxed text-mi-fg">
              {summary}
            </p>
          )}
        </div>
      )}

      {!saved.length && (
        <Quiet>
          <span className="mt-3 block">
            Save the current scene to compare layouts under the deterministic evaluator.
          </span>
        </Quiet>
      )}
    </RailSection>
  );
}
