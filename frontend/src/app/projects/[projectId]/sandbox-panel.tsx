"use client";

import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../../lib/api";
import { basemapStyle } from "../../../lib/basemap";

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
  Map: new(options: Value) => MapLike;
  NavigationControl: new(options?: Value) => unknown;
  ScaleControl: new(options?: Value) => unknown;
};

declare global { interface Window { maplibregl?: MapLibre } }

const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const absolute = (url: string) => url.startsWith("http") ? url : `${apiUrl}${url}`;
const feature = (geometry: unknown) => ({ type: "Feature", properties: {}, geometry });
const earthRadiusM = 6371008.8;

function proposedFeatures(scene: Value) {
  const frame = scene.frame as Value;
  const origin = frame.origin as Value;
  const localToLngLat = (x: number, y: number) => [
    Number(origin.lng) + (x / (earthRadiusM * Math.cos(Number(origin.lat) * Math.PI / 180))) * (180 / Math.PI),
    Number(origin.lat) + (y / earthRadiusM) * (180 / Math.PI),
  ];
  const rectangle = (object: Value, geometry: Value, renderClass: string) => {
    const halfWidth = Number(geometry.width_m) / 2;
    const halfLength = Number(geometry.length_m) / 2;
    const radians = Number(geometry.rotation_deg) * Math.PI / 180;
    const center = geometry.center_xy_m as number[];
    const coordinates = [[-halfWidth, -halfLength], [halfWidth, -halfLength], [halfWidth, halfLength], [-halfWidth, halfLength]].map(([x, y]) => localToLngLat(
      x * Math.cos(radians) - y * Math.sin(radians) + Number(center[0]),
      x * Math.sin(radians) + y * Math.cos(radians) + Number(center[1]),
    ));
    coordinates.push(coordinates[0]);
    return { type: "Feature", geometry: { type: "Polygon", coordinates: [coordinates] }, properties: { id: object.id, render_class: renderClass, height_m: Number(geometry.height_m ?? 0) } };
  };
  const features = ((scene.proposed as Value[] | undefined) ?? []).flatMap((object) => {
    const parent = object.geometry_local as Value;
    const parentFeature = rectangle(object, parent, Array.isArray(object.components) ? "campus_boundary" : String(object.render_class ?? "building"));
    const radians = Number(parent.rotation_deg) * Math.PI / 180;
    const center = parent.center_xy_m as number[];
    const children = ((object.components as Value[] | undefined) ?? []).map((component) => {
      const relative = component.geometry_relative as Value;
      const uv = relative.center_uv as number[];
      const x = Number(uv[0]) * Number(parent.width_m);
      const y = Number(uv[1]) * Number(parent.length_m);
      const geometry = {
        center_xy_m: [Number(center[0]) + x * Math.cos(radians) - y * Math.sin(radians), Number(center[1]) + x * Math.sin(radians) + y * Math.cos(radians)],
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
    if (existing) { existing.addEventListener("load", () => resolve(), { once: true }); return; }
    const script = document.createElement("script");
    script.src = `${apiUrl}/static/vendor/maplibre-gl.js`;
    script.dataset.maplibre = "true";
    script.onload = () => resolve();
    script.onerror = () => reject(new Error("MapLibre could not load."));
    document.head.appendChild(script);
  });
}

function addWorldLayers(map: MapLike, world: Value) {
  for (const item of (world.layers as Value[] | undefined) ?? []) {
    if (item.availability !== "AVAILABLE") continue;
    const render = item.render as Value | undefined;
    const name = String(item.layer);
    if (!render) continue;
    if (name === "terrain") {
      map.addSource("world-terrain", { ...render, tiles: (render.tiles as string[]).map(absolute), bounds: (world.query_aoi as Value).bbox });
      map.setTerrain({ source: "world-terrain", exaggeration: 1.25 });
      map.addLayer({ id: "world-terrain-hillshade", type: "hillshade", source: "world-terrain" });
      continue;
    }
    map.addSource(`world-${name}`, { type: "geojson", data: absolute(String(render.url)) });
    if (name === "roads") map.addLayer({ id: "world-roads", type: "line", source: "world-roads", paint: { "line-color": "#586a66", "line-width": 2.4 } });
    else if (name === "buildings") map.addLayer({ id: "world-buildings", type: "fill-extrusion", source: "world-buildings", paint: { "fill-extrusion-color": "#778783", "fill-extrusion-height": ["to-number", ["get", "height_m"], 0], "fill-extrusion-opacity": 0.72 } });
    else map.addLayer({ id: `world-${name}`, type: "fill", source: `world-${name}`, paint: { "fill-color": name === "water" ? "#6d9daf" : "#7f9671", "fill-opacity": 0.46 } });
  }
}

function addSceneLayers(map: MapLike, scene: Value, world?: Value) {
  if (world) addWorldLayers(map, world);
  const objects = [...((scene.observed as Value[] | undefined) ?? []), ...((scene.derived as Value[] | undefined) ?? [])];
  const parcel = objects.find((item) => item.id === "parcel_boundary");
  const point = objects.find((item) => item.id === "resolution_point");
  if (parcel) {
    map.addSource("sandbox-parcel", { type: "geojson", data: feature(parcel.geometry) });
    map.addLayer({ id: "sandbox-parcel-fill", type: "fill", source: "sandbox-parcel", paint: { "fill-color": "#177064", "fill-opacity": 0.14 } });
    map.addLayer({ id: "sandbox-parcel-line", type: "line", source: "sandbox-parcel", paint: { "line-color": "#177064", "line-width": 3 } });
  }
  if (point) {
    map.addSource("sandbox-point", { type: "geojson", data: feature(point.geometry) });
    map.addLayer({ id: "sandbox-point-circle", type: "circle", source: "sandbox-point", paint: { "circle-radius": 7, "circle-color": "#315b86", "circle-stroke-width": 2, "circle-stroke-color": "#fff" } });
  }
  const proposed = proposedFeatures(scene);
  if (proposed.features.length) {
    map.addSource("sandbox-proposed", { type: "geojson", data: proposed });
    map.addLayer({ id: "sandbox-proposed-fill", type: "fill", source: "sandbox-proposed", paint: { "fill-color": "#e95920", "fill-opacity": 0.34 } });
    map.addLayer({ id: "sandbox-proposed-outline", type: "line", source: "sandbox-proposed", paint: { "line-color": "#e95920", "line-width": 2, "line-dasharray": [2, 2] } });
  }
  const bbox = (world?.query_aoi as Value | undefined)?.bbox as number[] | undefined;
  if (bbox) map.fitBounds([[bbox[0], bbox[1]], [bbox[2], bbox[3]]], { padding: 48, maxZoom: 16.5, duration: 0 });
}

const worldLayers = ["Terrain", "Roads", "Buildings", "Water", "Land cover"];
const layerIds: Record<string, string[]> = {
  Terrain: ["world-terrain-hillshade"], Roads: ["world-roads"], Buildings: ["world-buildings"], Water: ["world-water"], "Land cover": ["world-land_cover"],
};

export function WorldLayerControls({ visible, onToggle }: { visible: Set<string>; onToggle: (name: string) => void }) {
  return <div className="world-controls" aria-label="World layers">
    <div className="semantic-legend"><span className="observed">Observed</span><span className="derived">Derived</span><span className="proposed">Proposed</span></div>
    <div className="layer-buttons">{worldLayers.map((name) => <button key={name} className={visible.has(name) ? "active" : ""} aria-pressed={visible.has(name)} onClick={() => onToggle(name)}>{name}</button>)}</div>
  </div>;
}

export function ScenarioSwitcher({ onEvaluate, onSave, busy }: { onEvaluate: () => void; onSave: () => void; busy: boolean }) {
  return <div className="scenario-switcher"><div><span className="eyebrow">Scenario</span><strong>Current scene</strong><small>Authoritative site + proposed objects</small></div><button onClick={onEvaluate} disabled={busy}>Evaluate</button><button className="primary-action" onClick={onSave} disabled={busy}>Save scenario</button></div>;
}

export function SandboxPanel({ projectId, workspaceId, candidates }: { projectId: string; workspaceId?: string; candidates?: Value[] }) {
  const container = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLike | undefined>(undefined);
  const [scene, setScene] = useState<Value>();
  const [world, setWorld] = useState<Value>();
  const [result, setResult] = useState<Value>();
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [visibleLayers, setVisibleLayers] = useState(() => new Set(worldLayers));
  const selected = useMemo(() => candidates?.find((candidate) => candidate.snapshot_id), [candidates]);
  const snapshotId = selected?.snapshot_id ? String(selected.snapshot_id) : undefined;
  const worldId = useMemo(() => {
    const url = (selected?.summary as Value | undefined)?.sandbox_url;
    return typeof url === "string" ? new URL(url, "http://local").searchParams.get("world") ?? undefined : undefined;
  }, [selected]);

  useEffect(() => {
    if (!snapshotId) return;
    void Promise.all([
      api.GET("/v1/sandbox/site/snapshots/{snapshot_id}/scene", { params: { path: { snapshot_id: snapshotId } } }),
      worldId ? api.GET("/v1/sandbox/world-snapshots/{world_snapshot_id}", { params: { path: { world_snapshot_id: worldId } } }) : Promise.resolve(undefined),
    ]).then(([sceneResponse, worldResponse]) => {
      if (sceneResponse.error) throw new Error("The authoritative site scene could not be loaded.");
      setScene(sceneResponse.data as Value);
      if (worldResponse && !worldResponse.error) setWorld(worldResponse.data as Value);
    }).catch((reason: Error) => setError(reason.message));
  }, [snapshotId, worldId]);

  useEffect(() => {
    if (!container.current || !scene) return;
    let map: MapLike | undefined;
    void loadMapLibre().then(() => {
      if (!container.current || !window.maplibregl) return;
      const bounds = (world?.query_aoi as Value | undefined)?.bbox as number[] | undefined;
      const camera = scene.camera as Value;
      const center = camera.center as Value;
      const primary = process.env.NEXT_PUBLIC_BASEMAP_STYLE_URL;
      const fallback = process.env.NEXT_PUBLIC_BASEMAP_FALLBACK_STYLE_URL;
      let switched = false;
      let loaded = false;
      map = new window.maplibregl.Map({ container: container.current, style: basemapStyle(primary, fallback), center: bounds ? [(bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2] : [center.lng, center.lat], zoom: bounds ? 12.5 : camera.zoom, pitch: 55, bearing: -20, maxPitch: 75 });
      mapRef.current = map;
      map.addControl(new window.maplibregl.NavigationControl(), "top-right");
      map.addControl(new window.maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");
      map.on("load", () => { loaded = true; if (map) { map.resize(); addSceneLayers(map, scene, world); } });
      map.on("error", () => { if (!loaded && primary && fallback && !switched && map) { switched = true; map.setStyle(fallback); } });
    }).catch((reason: Error) => setError(reason.message));
    return () => { mapRef.current = undefined; map?.remove(); };
  }, [scene, world]);

  function toggleLayer(name: string) {
    const next = new Set(visibleLayers);
    if (next.has(name)) next.delete(name); else next.add(name);
    setVisibleLayers(next);
    for (const id of layerIds[name] ?? []) if (mapRef.current?.getLayer(id)) mapRef.current.setLayoutProperty(id, "visibility", next.has(name) ? "visible" : "none");
  }

  async function evaluate() {
    if (!snapshotId || !scene) return;
    setBusy(true);
    const response = await api.POST("/v1/sandbox/site/{snapshot_id}/evaluate", { params: { path: { snapshot_id: snapshotId } }, body: { scene_state: scene, requested_constraints: [] } as never });
    setBusy(false);
    if (response.error) setError("The deterministic evaluator rejected the scene."); else setResult(response.data as Value);
  }

  async function saveScenario() {
    if (!snapshotId || !scene) return;
    setBusy(true);
    const response = await api.POST("/v1/sandbox/{snapshot_id}/scenarios", { params: { path: { snapshot_id: snapshotId } }, body: { workspace_id: workspaceId ?? "local", user_intent: "Save the current React sandbox scene", scene_state: scene, requested_constraints: [], world_snapshot_id: worldId, model_id: null } as never });
    setBusy(false);
    if (response.error) setError("The scenario could not be saved."); else setResult(response.data as Value);
  }

  async function ask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!snapshotId || !scene) return;
    const message = String(new FormData(event.currentTarget).get("message"));
    const response = await api.POST("/v1/sandbox/{snapshot_id}/chat", { params: { path: { snapshot_id: snapshotId } }, body: { message, scene_state: scene, session_id: `${projectId}:${snapshotId}` } as never });
    if (response.error) setError("The sandbox agent could not complete the request."); else setResult(response.data as Value);
  }

  return <section className="sandbox-panel" aria-label="Site world and scenarios">
    {!snapshotId && <p className="map-empty">Enrich a candidate to load its authoritative SiteSnapshot.</p>}
    {snapshotId && <>
      <div className="map-frame">
        <div ref={container} data-testid="react-sandbox-map" className="world-map"/>
        <div className="map-title"><span className="eyebrow">Real world</span><strong>SiteSnapshot</strong></div>
        <WorldLayerControls visible={visibleLayers} onToggle={toggleLayer}/>
        {!process.env.NEXT_PUBLIC_BASEMAP_STYLE_URL && !process.env.NEXT_PUBLIC_BASEMAP_FALLBACK_STYLE_URL && <p className="basemap-note">World layers active · Basemap unavailable</p>}
      </div>
      <ScenarioSwitcher onEvaluate={() => void evaluate()} onSave={() => void saveScenario()} busy={busy}/>
      <details className="scenario-agent"><summary>Plan a different scenario</summary><form onSubmit={ask}><input name="message" required placeholder="Try a layout that preserves more expansion land"/><button className="primary-action">Apply with MIREYE</button></form></details>
    </>}
    {error && <p role="alert" className="map-message error">{error}</p>}
    {result && <p className="map-message success"><strong>Scenario updated.</strong> The deterministic result is saved in project state.</p>}
  </section>;
}
