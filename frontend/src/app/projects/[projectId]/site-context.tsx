"use client";

import { createContext, useContext, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";

export type Value = Record<string, unknown>;

export const rec = (value: unknown): Value =>
  value && typeof value === "object" && !Array.isArray(value) ? (value as Value) : {};
export const arr = (value: unknown): Value[] =>
  Array.isArray(value) ? value.filter((i): i is Value => Boolean(i) && typeof i === "object") : [];
export const num = (value: unknown) => {
  const n = Number(value);
  return Number.isFinite(n) ? n : undefined;
};

/**
 * A stable clock, in seconds. Reading Date.now() during render makes freshness
 * unstable across re-renders, so the value is held in state and ticked once a
 * minute — fast enough for "5 min ago", slow enough to be free.
 */
export function useNowSeconds(): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now() / 1000), 60_000);
    return () => window.clearInterval(id);
  }, []);
  return now;
}

/** Mirrors relativeTime() in app/static/sandbox.js. */
export function relativeTime(timestamp: unknown, now: number): string {
  const seconds = Math.max(0, Math.round(now - Number(timestamp || 0)));
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} hr ago`;
  return `${Math.floor(seconds / 86400)} days ago`;
}

/** Constraint id → human label. Copied verbatim from app/static/sandbox.js. */
const CONSTRAINT_NAMES: Record<string, string> = {
  footprint_inside_parcel: "Fits inside parcel",
  minimum_setback: "Setback",
  footprint_area: "Footprint area",
  parcel_coverage: "Parcel coverage",
  object_collision: "Blocked areas",
  resolution_point_outside_fema_sfha: "Flood at resolution point",
  max_nwi_wetland_fraction_of_parcel: "Mapped wetlands",
  max_nwi_wetland_acres_on_parcel: "Mapped wetlands",
  max_resolution_point_slope_degrees: "Slope at resolution point",
  max_resolution_point_substation_distance_m: "Substation proximity",
  max_resolution_point_transmission_distance_m: "Transmission proximity",
  max_resolution_point_major_road_distance_m: "Road proximity",
  parcel_zoning_code_in: "Raw zoning code",
  parcel_outside_fema_sfha: "Whole-parcel flood",
  industrial_zoning: "Industrial zoning",
  sufficient_grid_capacity: "Grid capacity",
  legal_access: "Legal road access",
};

export const constraintName = (id: unknown) =>
  CONSTRAINT_NAMES[String(id)] ||
  String(id).replaceAll("_", " ").replace(/^./, (v) => v.toUpperCase());

/**
 * A field's value, but only if the record is actually usable — status ok, a
 * non-null value, and still inside its freshness window. Mirrors
 * usableEvidence() in app/static/sandbox.js; the freshness rule is the whole
 * point, so it is not relaxed here.
 */
export function usableEvidence(
  snapshot: Value | undefined,
  field: string,
  now: number,
): unknown {
  const record = rec(rec(snapshot?.evidence)[field]);
  const ok =
    record.status === "ok" &&
    record.value !== null &&
    record.value !== undefined &&
    Number(record.expires_at) > now;
  return ok ? record.value : null;
}

/** Layers a WorldSnapshot must cover before it can be reused. */
const REQUIRED_LAYERS = [
  "terrain",
  "roads",
  "buildings",
  "water",
  "land_cover",
  "transmission",
] as const;

interface SiteValue {
  snapshotId?: string;
  worldId?: string;
  scene?: Value;
  setScene: (scene: Value) => void;
  world?: Value;
  snapshot?: Value;
  plan?: Value;
  reloadPlan: () => void;
  error?: string;
  setError: (message?: string) => void;
}

const SiteContext = createContext<SiteValue>({
  setScene: () => {},
  reloadPlan: () => {},
  setError: () => {},
});

export const useSite = () => useContext(SiteContext);

/**
 * Owns everything keyed to one SiteSnapshot: the authoritative scene, the
 * pinned WorldSnapshot, the snapshot record and the evidence plan. The map and
 * the rail both read from here so they can never disagree about which site
 * they are showing.
 *
 * Every request below is the same call the vanilla sandbox made.
 */
export function SiteProvider({
  snapshotId,
  worldId,
  children,
}: {
  snapshotId?: string;
  worldId?: string;
  children: React.ReactNode;
}) {
  const [scene, setScene] = useState<Value>();
  const [world, setWorld] = useState<Value>();
  const [snapshot, setSnapshot] = useState<Value>();
  const [plan, setPlan] = useState<Value>();
  const [planNonce, setPlanNonce] = useState(0);
  const [error, setError] = useState<string>();

  // Scene, then the pinned world — creating one if the site has none.
  useEffect(() => {
    if (!snapshotId) return;
    let cancelled = false;

    /**
     * Mirrors ensureWorld() in app/static/sandbox.js: reuse the pinned
     * WorldSnapshot only when it actually covers every required layer,
     * otherwise build a fresh one. Without this the map silently renders with
     * no world layers whenever a site has never had one prepared.
     */
    async function ensureWorld(id: string): Promise<Value | undefined> {
      if (worldId) {
        const existing = await api.GET("/v1/sandbox/world-snapshots/{world_snapshot_id}", {
          params: { path: { world_snapshot_id: worldId } },
        });
        if (!existing.error) {
          const world = existing.data as Value;
          const present = new Set(arr(world.layers).map((layer) => String(layer.layer)));
          if (REQUIRED_LAYERS.every((layer) => present.has(layer))) return world;
        }
      }
      const created = await api.POST("/v1/sandbox/world-snapshots", {
        body: { site_snapshot_id: id, requested_layers: REQUIRED_LAYERS } as never,
      });
      if (created.error) return undefined;
      const world = created.data as Value;
      // Only adopt a world that actually came back as one. A malformed payload
      // here would otherwise blank the layer panel and drop the camera bounds.
      if (!world?.world_snapshot_id || !arr(world.layers).length) return undefined;
      return world;
    }

    void (async () => {
      const sceneResponse = await api.GET("/v1/sandbox/site/snapshots/{snapshot_id}/scene", {
        params: { path: { snapshot_id: snapshotId } },
      });
      if (cancelled) return;
      if (sceneResponse.error) {
        setError("The authoritative site scene could not be loaded.");
        return;
      }
      setScene(sceneResponse.data as Value);
      const world = await ensureWorld(snapshotId);
      if (!cancelled && world) setWorld(world);
    })();

    return () => {
      cancelled = true;
    };
  }, [snapshotId, worldId]);

  // Snapshot record — evidence, identity, expiry.
  useEffect(() => {
    if (!snapshotId) return;
    void api
      .GET("/v1/sandbox/site/snapshots/{snapshot_id}", {
        params: { path: { snapshot_id: snapshotId } },
      })
      .then((response) => {
        if (!response.error) setSnapshot(response.data as Value);
      });
  }, [snapshotId]);

  // Evidence plan + freshness. Re-read after a confirmed refresh.
  useEffect(() => {
    if (!snapshotId) return;
    void api
      .GET("/v1/sandbox/site/{snapshot_id}/intelligence-plan", {
        params: { path: { snapshot_id: snapshotId } },
      })
      .then((response) => {
        if (!response.error) setPlan(response.data as Value);
      });
  }, [snapshotId, planNonce]);

  const value = useMemo<SiteValue>(
    () => ({
      snapshotId,
      worldId,
      scene,
      setScene,
      world,
      snapshot,
      plan,
      reloadPlan: () => setPlanNonce((n) => n + 1),
      error,
      setError,
    }),
    [snapshotId, worldId, scene, world, snapshot, plan, error],
  );

  return <SiteContext.Provider value={value}>{children}</SiteContext.Provider>;
}
