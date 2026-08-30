"use client";

import dynamic from "next/dynamic";
import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { basemapStyle } from "@/lib/basemap";
import { useTheme } from "@/components/theme";
import { useSite } from "./site-context";
import { TerminalLog } from "@/components/motion/terminal-log";
import {
  Eyebrow,
  GhostButton,
  OriginLegend,
  PrimaryButton,
} from "@/components/product/ui";

const KineticGrid = dynamic(() => import("@/components/ui/kinetic-grid"), { ssr: false });

/** Printed while no snapshot is attached. Idle state, still legible as work. */
const IDLE_LOG = ["awaiting snapshot_id…", "no authoritative scene attached", "evaluator idle"];

type Value = Record<string, unknown>;
type MapLike = {
  addControl(control: unknown, position?: string): void;
  addLayer(layer: Value): void;
  addSource(id: string, source: Value): void;
  fitBounds(bounds: [[number, number], [number, number]], options: Value): void;
  getLayer(id: string): unknown;
  on(event: string, callback: () => void): void;
  remove(): void;
  resize(): void;
  setStyle(style: unknown): void;
  setLayoutProperty(id: string, property: string, value: unknown): void;
  setTerrain(terrain: Value): void;
};
type MapLibre = {
  Map: new (options: Value) => MapLike;
  NavigationControl: new (options?: Value) => unknown;
  ScaleControl: new (options?: Value) => unknown;
};

declare global {
  interface Window {
    maplibregl?: MapLibre;
  }
}

const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const absolute = (url: string) => (url.startsWith("http") ? url : `${apiUrl}${url}`);
const feature = (geometry: unknown) => ({ type: "Feature", properties: {}, geometry });
const earthRadiusM = 6371008.8;

/**
 * Map palette, keyed to provenance rather than decoration.
 *
 *   Observed  — the authoritative real world      → chalk / jet
 *   Derived   — computed from observed geometry   → Denim grey
 *   Proposed  — a design that does not exist yet  → Mandarin orange
 *
 * Only the proposed layer is coloured, which is the one thing on the map that
 * is not yet real. Two variants so the map stays legible in both themes.
 */
const MAP_PALETTE = {
  dark: {
    observed: "#F4F4F4",
    derived: "#A1A1AA",
    proposed: "#FF6600",
    roads: "#71717A",
    buildings: "#3F3F46",
    water: "#1B1B21",
    land: "#141418",
    pointStroke: "#000000",
    field: "#0D0D10",
  },
  light: {
    observed: "#000000",
    derived: "#57575F",
    proposed: "#C24A00",
    roads: "#71717A",
    buildings: "#B4B4BC",
    water: "#DCDCE2",
    land: "#E4E4E8",
    pointStroke: "#FFFFFF",
    field: "#EBEBEB",
  },
} as const;

function proposedFeatures(scene: Value) {
  const frame = scene.frame as Value;
  const origin = frame.origin as Value;
  const localToLngLat = (x: number, y: number) => [
    Number(origin.lng) +
      (x / (earthRadiusM * Math.cos((Number(origin.lat) * Math.PI) / 180))) * (180 / Math.PI),
    Number(origin.lat) + (y / earthRadiusM) * (180 / Math.PI),
  ];
  const rectangle = (object: Value, geometry: Value, renderClass: string) => {
    const halfWidth = Number(geometry.width_m) / 2;
    const halfLength = Number(geometry.length_m) / 2;
    const radians = (Number(geometry.rotation_deg) * Math.PI) / 180;
    const center = geometry.center_xy_m as number[];
    const coordinates = [
      [-halfWidth, -halfLength],
      [halfWidth, -halfLength],
      [halfWidth, halfLength],
      [-halfWidth, halfLength],
    ].map(([x, y]) =>
      localToLngLat(
        x * Math.cos(radians) - y * Math.sin(radians) + Number(center[0]),
        x * Math.sin(radians) + y * Math.cos(radians) + Number(center[1]),
      ),
    );
    coordinates.push(coordinates[0]);
    return {
      type: "Feature",
      geometry: { type: "Polygon", coordinates: [coordinates] },
      properties: {
        id: object.id,
        render_class: renderClass,
        height_m: Number(geometry.height_m ?? 0),
      },
    };
  };
  const features = ((scene.proposed as Value[] | undefined) ?? []).flatMap((object) => {
    const parent = object.geometry_local as Value;
    const parentFeature = rectangle(
      object,
      parent,
      Array.isArray(object.components) ? "campus_boundary" : String(object.render_class ?? "building"),
    );
    const radians = (Number(parent.rotation_deg) * Math.PI) / 180;
    const center = parent.center_xy_m as number[];
    const children = ((object.components as Value[] | undefined) ?? []).map((component) => {
      const relative = component.geometry_relative as Value;
      const uv = relative.center_uv as number[];
      const x = Number(uv[0]) * Number(parent.width_m);
      const y = Number(uv[1]) * Number(parent.length_m);
      const geometry = {
        center_xy_m: [
          Number(center[0]) + x * Math.cos(radians) - y * Math.sin(radians),
          Number(center[1]) + x * Math.sin(radians) + y * Math.cos(radians),
        ],
        width_m: Number(relative.width_ratio) * Number(parent.width_m),
        length_m: Number(relative.length_ratio) * Number(parent.length_m),
        height_m: Number(relative.height_m),
        rotation_deg: Number(parent.rotation_deg) + Number(relative.rotation_offset_deg ?? 0),
      };
      return rectangle(component, geometry, String(component.render_class ?? "building"));
    });
    return [parentFeature, ...children];
  });
  return { type: "FeatureCollection", features };
}

function loadMapLibre() {
  if (window.maplibregl) return Promise.resolve();
  if (!document.querySelector("link[data-maplibre]")) {
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = `${apiUrl}/static/vendor/maplibre-gl.css`;
    link.dataset.maplibre = "true";
    document.head.appendChild(link);
  }
  return new Promise<void>((resolve, reject) => {
    const existing = document.querySelector<HTMLScriptElement>("script[data-maplibre]");
    if (existing) {
      existing.addEventListener("load", () => resolve(), { once: true });
      return;
    }
    const script = document.createElement("script");
    script.src = `${apiUrl}/static/vendor/maplibre-gl.js`;
    script.dataset.maplibre = "true";
    script.onload = () => resolve();
    script.onerror = () => reject(new Error("MapLibre could not load."));
    document.head.appendChild(script);
  });
}

type Palette = (typeof MAP_PALETTE)[keyof typeof MAP_PALETTE];

function addWorldLayers(map: MapLike, world: Value, palette: Palette) {
  for (const item of (world.layers as Value[] | undefined) ?? []) {
    if (item.availability !== "AVAILABLE") continue;
    const render = item.render as Value | undefined;
    const name = String(item.layer);
    if (!render) continue;
    if (name === "terrain") {
      map.addSource("world-terrain", {
        ...render,
        tiles: (render.tiles as string[]).map(absolute),
        bounds: (world.query_aoi as Value).bbox,
      });
      map.setTerrain({ source: "world-terrain", exaggeration: 1.25 });
      map.addLayer({ id: "world-terrain-hillshade", type: "hillshade", source: "world-terrain" });
      continue;
    }
    map.addSource(`world-${name}`, { type: "geojson", data: absolute(String(render.url)) });
    if (name === "roads")
      map.addLayer({
        id: "world-roads",
        type: "line",
        source: "world-roads",
        paint: { "line-color": palette.roads, "line-width": 2.4 },
      });
    else if (name === "buildings")
      map.addLayer({
        id: "world-buildings",
        type: "fill-extrusion",
        source: "world-buildings",
        paint: {
          "fill-extrusion-color": palette.buildings,
          "fill-extrusion-height": ["to-number", ["get", "height_m"], 0],
          "fill-extrusion-opacity": 0.72,
        },
      });
    else
      map.addLayer({
        id: `world-${name}`,
        type: "fill",
        source: `world-${name}`,
        paint: {
          "fill-color": name === "water" ? palette.water : palette.land,
          "fill-opacity": 0.46,
        },
      });
  }
}

function addSceneLayers(map: MapLike, scene: Value, palette: Palette, world?: Value) {
  if (world) addWorldLayers(map, world, palette);
  const objects = [
    ...((scene.observed as Value[] | undefined) ?? []),
    ...((scene.derived as Value[] | undefined) ?? []),
  ];
  const parcel = objects.find((item) => item.id === "parcel_boundary");
  const point = objects.find((item) => item.id === "resolution_point");
  if (parcel) {
    map.addSource("sandbox-parcel", { type: "geojson", data: feature(parcel.geometry) });
    map.addLayer({
      id: "sandbox-parcel-fill",
      type: "fill",
      source: "sandbox-parcel",
      paint: { "fill-color": palette.observed, "fill-opacity": 0.08 },
    });
    map.addLayer({
      id: "sandbox-parcel-line",
      type: "line",
      source: "sandbox-parcel",
      paint: { "line-color": palette.observed, "line-width": 2 },
    });
  }
  if (point) {
    map.addSource("sandbox-point", { type: "geojson", data: feature(point.geometry) });
    map.addLayer({
      id: "sandbox-point-circle",
      type: "circle",
      source: "sandbox-point",
      paint: {
        "circle-radius": 6,
        "circle-color": palette.derived,
        "circle-stroke-width": 2,
        "circle-stroke-color": palette.pointStroke,
      },
    });
  }
  const proposed = proposedFeatures(scene);
  if (proposed.features.length) {
    map.addSource("sandbox-proposed", { type: "geojson", data: proposed });
    map.addLayer({
      id: "sandbox-proposed-fill",
      type: "fill",
      source: "sandbox-proposed",
      paint: { "fill-color": palette.proposed, "fill-opacity": 0.3 },
    });
    map.addLayer({
      id: "sandbox-proposed-outline",
      type: "line",
      source: "sandbox-proposed",
      paint: { "line-color": palette.proposed, "line-width": 1.5, "line-dasharray": [2, 2] },
    });
  }
  const bbox = (world?.query_aoi as Value | undefined)?.bbox as number[] | undefined;
  if (bbox)
    map.fitBounds([[bbox[0], bbox[1]], [bbox[2], bbox[3]]], {
      padding: 48,
      maxZoom: 16.5,
      duration: 0,
    });
}

const worldLayers = ["Terrain", "Roads", "Buildings", "Water", "Land cover"];
const layerIds: Record<string, string[]> = {
  Terrain: ["world-terrain-hillshade"],
  Roads: ["world-roads"],
  Buildings: ["world-buildings"],
  Water: ["world-water"],
  "Land cover": ["world-land_cover"],
};

export function WorldLayerControls({
  visible,
  onToggle,
}: {
  visible: Set<string>;
  onToggle: (name: string) => void;
}) {
  return (
    <div
      className="absolute left-4 top-[104px] z-10 w-[200px] overflow-hidden rounded-[2px] bg-mi-surface shadow-plateau sm:left-auto sm:right-16 sm:top-4"
      aria-label="World layers"
    >
      <div className="border-b border-mi-line px-3 py-2.5">
        <OriginLegend className="flex-wrap gap-x-2.5 gap-y-1" />
      </div>
      <div className="grid grid-cols-1 gap-px bg-mi-line">
        {worldLayers.map((name) => {
          const active = visible.has(name);
          return (
            <button
              key={name}
              aria-pressed={active}
              onClick={() => onToggle(name)}
              className={`flex cursor-pointer items-center gap-1.5 bg-mi-surface px-2.5 py-2 text-left font-mono text-[9px] uppercase tracking-cite transition-colors duration-micro ease-mi ${
                active ? "text-mi-fg-strong" : "text-mi-fg-muted hover:text-mi-fg"
              }`}
            >
              <span
                aria-hidden
                className={`h-2 w-2 shrink-0 border ${
                  active ? "border-mi-fg-strong bg-mi-fg-strong" : "border-mi-line-strong"
                }`}
              />
              <span className="truncate">{name}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

export function ScenarioSwitcher({
  onEvaluate,
  onSave,
  busy,
}: {
  onEvaluate: () => void;
  onSave: () => void;
  busy: boolean;
}) {
  return (
    // Wraps to two rows on narrow screens so the scene prompt keeps a usable
    // width instead of being crushed to nothing by the two buttons.
    <div className="relative z-10 flex shrink-0 flex-wrap items-center gap-3 bg-mi-surface px-4 py-3 shadow-plateau sm:h-[68px] sm:flex-nowrap sm:py-0 md:px-5">
      <div className="mr-auto min-w-0">
        <Eyebrow>Scenario</Eyebrow>
        <strong className="mt-0.5 block truncate text-[14px] font-medium text-mi-fg-strong">
          Current scene
        </strong>
      </div>

      <GhostButton onClick={onEvaluate} disabled={busy}>
        Evaluate
      </GhostButton>
      <PrimaryButton onClick={onSave} disabled={busy}>
        Save scenario
      </PrimaryButton>
    </div>
  );
}

export function SandboxPanel({ workspaceId }: { workspaceId?: string }) {
  const container = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLike | undefined>(undefined);
  const [result, setResult] = useState<Value>();
  const [busy, setBusy] = useState(false);
  const [visibleLayers, setVisibleLayers] = useState(() => new Set(worldLayers));
  const { theme } = useTheme();
  // Scene, world and the snapshot id now come from SiteProvider so the rail and
  // the map can never render two different sites. The calls themselves moved
  // there unchanged.
  const { snapshotId, worldId, scene, world, error, setError } = useSite();

  useEffect(() => {
    if (!container.current || !scene) return;
    const palette = MAP_PALETTE[theme];
    let map: MapLike | undefined;
    void loadMapLibre()
      .then(() => {
        if (!container.current || !window.maplibregl) return;
        const bounds = (world?.query_aoi as Value | undefined)?.bbox as number[] | undefined;
        const camera = scene.camera as Value;
        const center = camera.center as Value;
        const primary = process.env.NEXT_PUBLIC_BASEMAP_STYLE_URL;
        const fallback = process.env.NEXT_PUBLIC_BASEMAP_FALLBACK_STYLE_URL;
        let switched = false;
        let loaded = false;
        map = new window.maplibregl.Map({
          container: container.current,
          style: basemapStyle(primary, fallback, palette.field),
          center: bounds
            ? [(bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2]
            : [center.lng, center.lat],
          zoom: bounds ? 12.5 : camera.zoom,
          pitch: 55,
          bearing: -20,
          maxPitch: 75,
        });
        mapRef.current = map;
        map.addControl(new window.maplibregl.NavigationControl(), "top-right");
        map.addControl(new window.maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");
        map.on("load", () => {
          loaded = true;
          if (map) {
            map.resize();
            addSceneLayers(map, scene, palette, world);
          }
        });
        map.on("error", () => {
          if (!loaded && primary && fallback && !switched && map) {
            switched = true;
            map.setStyle(fallback);
          }
        });
      })
      .catch((reason: Error) => setError(reason.message));
    return () => {
      mapRef.current = undefined;
      map?.remove();
    };
    // theme is a dependency: switching themes rebuilds the map with the other
    // palette, which is the only way to recolour terrain + extrusion layers.
  }, [scene, world, theme, setError]);

  function toggleLayer(name: string) {
    const next = new Set(visibleLayers);
    if (next.has(name)) next.delete(name);
    else next.add(name);
    setVisibleLayers(next);
    for (const id of layerIds[name] ?? [])
      if (mapRef.current?.getLayer(id))
        mapRef.current.setLayoutProperty(id, "visibility", next.has(name) ? "visible" : "none");
  }

  async function evaluate() {
    if (!snapshotId || !scene) return;
    setBusy(true);
    const response = await api.POST("/v1/sandbox/site/{snapshot_id}/evaluate", {
      params: { path: { snapshot_id: snapshotId } },
      body: { scene_state: scene, requested_constraints: [] } as never,
    });
    setBusy(false);
    if (response.error) setError("The deterministic evaluator rejected the scene.");
    else setResult(response.data as Value);
  }

  async function saveScenario() {
    if (!snapshotId || !scene) return;
    setBusy(true);
    const response = await api.POST("/v1/sandbox/{snapshot_id}/scenarios", {
      params: { path: { snapshot_id: snapshotId } },
      body: {
        workspace_id: workspaceId ?? "local",
        user_intent: "Save the current React sandbox scene",
        scene_state: scene,
        requested_constraints: [],
        world_snapshot_id: worldId,
        model_id: null,
      } as never,
    });
    setBusy(false);
    if (response.error) setError("The scenario could not be saved.");
    else setResult(response.data as Value);
  }


  return (
    <section className="flex h-full flex-col" aria-label="Site world and scenarios">
      {!snapshotId && (
        <KineticGrid className="h-full min-h-[320px] flex-1">
          <div className="flex h-full min-h-[320px] items-center justify-center p-8">
            <div className="max-w-[38ch] rounded-[2px] bg-mi-surface p-5 shadow-plateau">
              <Eyebrow className="mb-3">Awaiting snapshot</Eyebrow>
              <p className="text-[13px] leading-relaxed text-mi-fg">
                Enrich a candidate to load its authoritative SiteSnapshot.
              </p>
              <div className="mt-4 border-t border-mi-line pt-3">
                <TerminalLog lines={IDLE_LOG} lineDelay={420} loop />
              </div>
            </div>
          </div>
        </KineticGrid>
      )}

      {snapshotId && (
        <>
          <div className="relative min-h-0 flex-1 overflow-hidden bg-mi-surface-2">
            <div
              ref={container}
              data-testid="react-sandbox-map"
              className="absolute inset-0 h-full w-full"
            />

            <div className="absolute left-4 top-4 z-10 rounded-[2px] bg-mi-surface px-3 py-2.5 shadow-plateau">
              <Eyebrow>Real world</Eyebrow>
              <strong className="mt-0.5 block text-[14px] font-medium text-mi-fg-strong">
                SiteSnapshot
              </strong>
            </div>

            <WorldLayerControls visible={visibleLayers} onToggle={toggleLayer} />

            {!process.env.NEXT_PUBLIC_BASEMAP_STYLE_URL &&
              !process.env.NEXT_PUBLIC_BASEMAP_FALLBACK_STYLE_URL && (
                <p className="absolute left-4 top-[76px] z-10 hidden rounded-[2px] bg-mi-surface-3 px-2.5 py-1.5 font-mono text-[10px] text-mi-fg-muted shadow-well sm:block">
                  World layers active · Basemap unavailable
                </p>
              )}
          </div>

          <ScenarioSwitcher
            onEvaluate={() => void evaluate()}
            onSave={() => void saveScenario()}
            busy={busy}
          />
        </>
      )}

      {error && (
        <p
          role="alert"
          className="absolute left-4 top-[88px] z-40 max-w-[420px] rounded-[2px] bg-mi-surface px-3 py-2.5 text-[12px] text-mi-orange-text shadow-plateau"
        >
          {error}
        </p>
      )}
      {result && (
        <p className="absolute left-4 top-[88px] z-40 max-w-[420px] rounded-[2px] bg-mi-surface px-3 py-2.5 text-[12px] text-mi-fg shadow-plateau">
          <strong className="font-medium text-mi-fg-strong">Scenario updated.</strong> The
          deterministic result is saved in project state.
        </p>
      )}
    </section>
  );
}
