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
  setTerrain(terrain: Value | null): void;
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
    const parentFeature = rectangle(object, parent, Array.isArray(object.components) ? "facility_boundary" : String(object.render_class ?? "building"));
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
      map.addLayer({ id: "world-terrain-hillshade", type: "hillshade", source: "world-terrain", paint: { "hillshade-shadow-color": "#4f5b55", "hillshade-highlight-color": "#f7f5ec", "hillshade-accent-color": "#87938c", "hillshade-exaggeration": 0.55 } });
      continue;
    }
    map.addSource(`world-${name}`, { type: "geojson", data: absolute(String(render.url)) });
    if (name === "roads") {
      const widths = ["interpolate", ["linear"], ["zoom"], 10, ["match", ["get", "class"], "motorway", 2.8, "trunk", 2.5, "primary", 2.2, "secondary", 1.8, "tertiary", 1.4, 0.8], 16, ["match", ["get", "class"], "motorway", 9, "trunk", 8, "primary", 6.8, "secondary", 5.2, "tertiary", 3.8, 1.6]];
      map.addLayer({ id: "world-roads-casing", type: "line", source: "world-roads", minzoom: 9, paint: { "line-color": "#f5f3ea", "line-opacity": 0.82, "line-width": ["+", widths, 1.4] } });
      map.addLayer({ id: "world-roads-line", type: "line", source: "world-roads", minzoom: 9, paint: { "line-color": ["match", ["get", "class"], "motorway", "#44555a", "trunk", "#536168", "primary", "#657178", "secondary", "#7c8584", "tertiary", "#949a93", "#adb0a7"], "line-width": widths, "line-opacity": 0.9 } });
    } else if (name === "buildings") {
      map.addLayer({ id: "world-buildings-footprints", type: "fill", source: "world-buildings", minzoom: 12, paint: { "fill-color": "#6e7773", "fill-opacity": 0.42, "fill-outline-color": "#56605b" } });
      map.addLayer({ id: "world-buildings-extrusion", type: "fill-extrusion", source: "world-buildings", minzoom: 13, filter: [">", ["to-number", ["get", "height_m"], 0], 0], paint: { "fill-extrusion-color": "#77817d", "fill-extrusion-height": ["to-number", ["get", "height_m"], 0], "fill-extrusion-opacity": 0.72 } });
    } else if (name === "water") {
      map.addLayer({ id: "world-water-fill", type: "fill", source: "world-water", minzoom: 9, paint: { "fill-color": "#6f9fac", "fill-opacity": 0.62 } });
      map.addLayer({ id: "world-water-line", type: "line", source: "world-water", minzoom: 9, paint: { "line-color": "#537f8c", "line-width": 1 } });
    } else {
      map.addLayer({ id: "world-land-cover-fill", type: "fill", source: "world-land_cover", minzoom: 10, paint: { "fill-color": ["match", ["get", "subtype"], "forest", "#53705b", "grass", "#879b73", "crop", "#a7a77c", "#81917d"], "fill-opacity": 0.26 } });
    }
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
  const centroid = ((scene.derived as Value[] | undefined) ?? []).find((item) => item.id === "parcel_centroid");
  if (centroid) {
    map.addSource("sandbox-centroid", { type: "geojson", data: feature(centroid.geometry) });
    map.addLayer({ id: "sandbox-centroid-circle", type: "circle", source: "sandbox-centroid", paint: { "circle-radius": 4, "circle-color": "#315b86", "circle-stroke-color": "#fff", "circle-stroke-width": 1 } });
  }
  const proposed = proposedFeatures(scene);
  if (proposed.features.length) {
    map.addSource("sandbox-proposed", { type: "geojson", data: proposed });
    map.addLayer({ id: "sandbox-proposed-surfaces", type: "fill", source: "sandbox-proposed", filter: ["in", ["get", "render_class"], ["literal", ["surface", "access", "reserve"]]], paint: { "fill-color": ["match", ["get", "render_class"], "access", "#667178", "reserve", "#8ca17f", "#e8a082"], "fill-opacity": ["match", ["get", "render_class"], "reserve", 0.28, 0.48] } });
    map.addLayer({ id: "sandbox-proposed-extrusion", type: "fill-extrusion", source: "sandbox-proposed", filter: ["in", ["get", "render_class"], ["literal", ["building", "utility"]]], paint: { "fill-extrusion-color": ["match", ["get", "render_class"], "utility", "#b8542b", "#e95920"], "fill-extrusion-height": ["get", "height_m"], "fill-extrusion-base": 0, "fill-extrusion-opacity": 0.9 } });
    map.addLayer({ id: "sandbox-proposed-outline", type: "line", source: "sandbox-proposed", filter: ["in", ["get", "render_class"], ["literal", ["facility_boundary", "reserve"]]], paint: { "line-color": ["match", ["get", "render_class"], "reserve", "#607558", "#b94a1a"], "line-width": ["match", ["get", "render_class"], "reserve", 1.5, 2.2], "line-dasharray": [2, 2] } });
  }
  const bbox = (world?.query_aoi as Value | undefined)?.bbox as number[] | undefined;
  if (bbox) map.fitBounds([[bbox[0], bbox[1]], [bbox[2], bbox[3]]], { padding: { top: 78, right: 70, bottom: 185, left: 70 }, maxZoom: 15.4, duration: 0 });
}

const worldLayers = ["Terrain", "Roads", "Buildings", "Water", "Land cover"];
const layerIds: Record<string, string[]> = {
  Terrain: ["world-terrain-hillshade"],
  Roads: ["world-roads-casing", "world-roads-line"],
  Buildings: ["world-buildings-footprints", "world-buildings-extrusion"],
  Water: ["world-water-fill", "world-water-line"],
  "Land cover": ["world-land-cover-fill"],
};
const semanticLayers: Record<string, string[]> = {
  Observed: [...Object.values(layerIds).flat(), "sandbox-parcel-fill", "sandbox-parcel-line", "sandbox-point-circle"],
  Derived: ["sandbox-centroid-circle"],
  Proposed: ["sandbox-proposed-surfaces", "sandbox-proposed-extrusion", "sandbox-proposed-outline"],
};

function boundsFor(geometry: unknown): [number, number, number, number] | undefined {
  const positions: [number, number][] = [];
  const visit = (value: unknown): void => {
    if (!Array.isArray(value)) return;
    if (typeof value[0] === "number" && typeof value[1] === "number") positions.push([value[0], value[1]]);
    else value.forEach(visit);
  };
  visit((geometry as Value | undefined)?.coordinates);
  if (!positions.length) return undefined;
  return [Math.min(...positions.map(([lng]) => lng)), Math.min(...positions.map(([, lat]) => lat)), Math.max(...positions.map(([lng]) => lng)), Math.max(...positions.map(([, lat]) => lat))];
}

export function WorldLayerControls({ visible, semantic, onToggle, onToggleSemantic }: { visible: Set<string>; semantic: Set<string>; onToggle: (name: string) => void; onToggleSemantic: (name: string) => void }) {
  return <div className="world-controls" aria-label="World layers">
    <div className="semantic-legend">{["Observed", "Derived", "Proposed"].map((name) => <button key={name} className={`${name.toLowerCase()} ${semantic.has(name) ? "active" : ""}`} aria-pressed={semantic.has(name)} onClick={() => onToggleSemantic(name)}>{name}</button>)}</div>
    <div className="layer-buttons">{worldLayers.map((name) => <button key={name} className={visible.has(name) ? "active" : ""} aria-pressed={visible.has(name)} onClick={() => onToggle(name)}>{name}</button>)}</div>
  </div>;
}

export function MapViewControls({ view, onChange }: { view: string; onChange: (view: string) => void }) {
  return <div className="map-views" aria-label="Map view"><span>View</span>{["Site", "Context", "Regional"].map((name) => <button key={name} className={view === name ? "active" : ""} onClick={() => onChange(name)}>{name}</button>)}</div>;
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
  const [visibleSemantic, setVisibleSemantic] = useState(() => new Set(Object.keys(semanticLayers)));
  const [mapView, setMapView] = useState("Context");
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
      map = new window.maplibregl.Map({ container: container.current, style: basemapStyle(primary, fallback), center: bounds ? [(bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2] : [center.lng, center.lat], zoom: bounds ? 12.5 : camera.zoom, pitch: 48, bearing: -16, maxPitch: 75, antialias: true });
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
    const visible = next.has(name) && visibleSemantic.has("Observed");
    if (name === "Terrain") mapRef.current?.setTerrain(visible ? { source: "world-terrain", exaggeration: 1.25 } : null);
    for (const id of layerIds[name] ?? []) if (mapRef.current?.getLayer(id)) mapRef.current.setLayoutProperty(id, "visibility", visible ? "visible" : "none");
  }

  function toggleSemantic(name: string) {
    const next = new Set(visibleSemantic);
    if (next.has(name)) next.delete(name); else next.add(name);
    setVisibleSemantic(next);
    const visible = next.has(name);
    if (name === "Observed") {
      if (mapRef.current) mapRef.current.setTerrain(visible && visibleLayers.has("Terrain") ? { source: "world-terrain", exaggeration: 1.25 } : null);
      for (const id of layerIds.Terrain) if (mapRef.current?.getLayer(id)) mapRef.current.setLayoutProperty(id, "visibility", visible && visibleLayers.has("Terrain") ? "visible" : "none");
      for (const layer of worldLayers.filter((layer) => layer !== "Terrain")) for (const id of layerIds[layer]) if (mapRef.current?.getLayer(id)) mapRef.current.setLayoutProperty(id, "visibility", visible && visibleLayers.has(layer) ? "visible" : "none");
      for (const id of ["sandbox-parcel-fill", "sandbox-parcel-line", "sandbox-point-circle"]) if (mapRef.current?.getLayer(id)) mapRef.current.setLayoutProperty(id, "visibility", visible ? "visible" : "none");
      return;
    }
    for (const id of semanticLayers[name] ?? []) if (mapRef.current?.getLayer(id)) mapRef.current.setLayoutProperty(id, "visibility", visible ? "visible" : "none");
  }

  function changeView(view: string) {
    setMapView(view);
    const context = (world?.query_aoi as Value | undefined)?.bbox as number[] | undefined;
    const parcel = ((scene?.observed as Value[] | undefined) ?? []).find((item) => item.id === "parcel_boundary");
    const site = boundsFor(parcel?.geometry);
    const base = view === "Site" ? site : context;
    if (!base || !mapRef.current) return;
    const scale = view === "Regional" ? 0.35 : 0;
    const lngPad = (base[2] - base[0]) * scale;
    const latPad = (base[3] - base[1]) * scale;
    mapRef.current.fitBounds([[base[0] - lngPad, base[1] - latPad], [base[2] + lngPad, base[3] + latPad]], { padding: { top: 78, right: 70, bottom: 185, left: 70 }, maxZoom: view === "Site" ? 16.5 : 15.4, duration: 350 });
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

  const siteName = String((selected?.summary as Value | undefined)?.title ?? selected?.address ?? "Selected site");
  return <section className="sandbox-panel" aria-label="Site world and scenarios">
    {!snapshotId && <p className="map-empty">Enrich a candidate to load its authoritative SiteSnapshot.</p>}
    {snapshotId && <>
      <div className="map-frame">
        <div ref={container} data-testid="react-sandbox-map" className="world-map"/>
        <div className="map-title"><span className="eyebrow">Real world</span><strong>{siteName}</strong><small>Observed WorldSnapshot</small></div>
        <MapViewControls view={mapView} onChange={changeView}/>
        <WorldLayerControls visible={visibleLayers} semantic={visibleSemantic} onToggle={toggleLayer} onToggleSemantic={toggleSemantic}/>
        {!process.env.NEXT_PUBLIC_BASEMAP_STYLE_URL && !process.env.NEXT_PUBLIC_BASEMAP_FALLBACK_STYLE_URL && <p className="basemap-note">World layers active · Basemap unavailable</p>}
      </div>
      <ScenarioSwitcher onEvaluate={() => void evaluate()} onSave={() => void saveScenario()} busy={busy}/>
      <details className="scenario-agent"><summary>Plan a different scenario</summary><form onSubmit={ask}><input name="message" required placeholder="Try a layout that preserves more expansion land"/><button className="primary-action">Apply with MIREYE</button></form></details>
    </>}
    {error && <p role="alert" className="map-message error">{error}</p>}
    {result && <p className="map-message success"><strong>Scenario updated.</strong> The deterministic result is saved in project state.</p>}
  </section>;
}
