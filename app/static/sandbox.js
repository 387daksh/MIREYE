(() => {
  const EARTH_RADIUS_M = 6371008.8;
  const sceneEndpoint = () => `/v1/sandbox/site/snapshots/${encodeURIComponent(snapshotId())}/scene`;
  const snapshotEndpoint = () => `/v1/sandbox/site/snapshots/${encodeURIComponent(snapshotId())}`;
  const chatEndpoint = () => `/v1/sandbox/${encodeURIComponent(snapshotId())}/chat`;
  const scenarioEndpoint = () => `/v1/sandbox/${encodeURIComponent(snapshotId())}/scenarios`;
  let sceneState;
  let snapshotData;
  let activeScenarioId = null;
  const scenarioRevisions = new Map();
  let map;
  const chatSessionId = window.crypto && window.crypto.randomUUID ? window.crypto.randomUUID() : `sandbox-${Date.now()}`;

  function snapshotId() {
    const parts = window.location.pathname.split("/").filter(Boolean);
    return decodeURIComponent(parts[parts.length - 1] || "");
  }

  function requestedScenarioId() {
    return new URLSearchParams(window.location.search).get("scenario");
  }

  function showError(message) {
    const error = document.getElementById("sandboxError");
    error.textContent = message;
    error.hidden = false;
    document.getElementById("snapshotState").textContent = "SNAPSHOT UNAVAILABLE";
  }

  function localToLngLat(x, y, frame) {
    const lat = frame.origin.lat + (y / EARTH_RADIUS_M) * (180 / Math.PI);
    const lng = frame.origin.lng + (x / (EARTH_RADIUS_M * Math.cos(frame.origin.lat * Math.PI / 180))) * (180 / Math.PI);
    return [lng, lat];
  }

  function proposedFeatures() {
    return {
      type: "FeatureCollection",
      features: sceneState.proposed.map((object) => {
        const geometry = object.geometry_local;
        const halfWidth = Number(geometry.width_m) / 2;
        const halfLength = Number(geometry.length_m) / 2;
        const radians = Number(geometry.rotation_deg) * Math.PI / 180;
        const corners = [[-halfWidth, -halfLength], [halfWidth, -halfLength], [halfWidth, halfLength], [-halfWidth, halfLength]];
        const coordinates = corners.map(([x, y]) => {
          const rotatedX = x * Math.cos(radians) - y * Math.sin(radians) + Number(geometry.center_xy_m[0]);
          const rotatedY = x * Math.sin(radians) + y * Math.cos(radians) + Number(geometry.center_xy_m[1]);
          return localToLngLat(rotatedX, rotatedY, sceneState.frame);
        });
        coordinates.push(coordinates[0]);
        return {
          type: "Feature",
          geometry: { type: "Polygon", coordinates: [coordinates] },
          properties: { id: object.id, height_m: Number(geometry.height_m), capacity_mw: Number(object.attributes.capacity_mw) },
        };
      }),
    };
  }

  function setSourceData(sourceId, data) {
    if (!map) return;
    const source = map.getSource(sourceId);
    if (source) source.setData(data);
  }

  function updateProposedObject() {
    const object = sceneState.proposed[0];
    if (!object) return;
    object.attributes.capacity_mw = Number(document.getElementById("capacityMw").value);
    object.geometry_local.height_m = Number(document.getElementById("heightM").value);
    object.geometry_local.width_m = Number(document.getElementById("widthM").value);
    object.geometry_local.length_m = Number(document.getElementById("lengthM").value);
    object.geometry_local.rotation_deg = Number(document.getElementById("rotationDeg").value);
    setSourceData("sandbox-proposed", proposedFeatures());
    renderScenarioFacts(activeScenarioId ? { scenario_id: activeScenarioId, revision: scenarioRevisions.get(activeScenarioId) } : null, null);
  }

  function bindControls() {
    ["capacityMw", "heightM", "widthM", "lengthM", "rotationDeg"].forEach((id) => {
      document.getElementById(id).addEventListener("input", updateProposedObject);
    });
  }

  function syncControls() {
    const object = sceneState.proposed[0];
    ["capacityMw", "heightM", "widthM", "lengthM", "rotationDeg"].forEach((id) => { document.getElementById(id).disabled = !object; });
    if (object) {
      document.getElementById("capacityMw").value = object.attributes.capacity_mw;
      document.getElementById("heightM").value = object.geometry_local.height_m;
      document.getElementById("widthM").value = object.geometry_local.width_m;
      document.getElementById("lengthM").value = object.geometry_local.length_m;
      document.getElementById("rotationDeg").value = object.geometry_local.rotation_deg;
    }
  }

  function stateBadge(outcome) {
    const badge = document.createElement("span");
    badge.className = `sandbox-state ${String(outcome).toLowerCase()}`;
    badge.textContent = outcome;
    return badge;
  }

  function renderEvaluation(container, evaluation) {
    container.replaceChildren();
    const summary = document.createElement("div");
    summary.className = "sandbox-evaluation-summary";
    summary.append(stateBadge(evaluation.overall_status));
    const summaryText = document.createElement("span");
    summaryText.textContent = `${evaluation.constraint_results.length} deterministic constraint result(s)`;
    summary.append(summaryText);
    container.append(summary);
    evaluation.constraint_results.forEach((item) => {
      const row = document.createElement("div");
      row.className = "sandbox-evaluation-item";
      row.append(stateBadge(item.outcome));
      const copy = document.createElement("div");
      copy.className = "sandbox-evaluation-copy";
      const title = document.createElement("strong");
      title.textContent = item.constraint_id;
      const reason = document.createElement("p");
      reason.textContent = item.explanation;
      const evidence = document.createElement("div");
      evidence.className = "sandbox-evidence-ids";
      evidence.textContent = `Evidence: ${item.evidence_ids.length ? item.evidence_ids.join(", ") : "derived calculation"}`;
      copy.append(title, reason, evidence);
      row.append(copy);
      container.append(row);
    });
    container.hidden = false;
  }

  function renderScenarioFacts(scenario, evaluation) {
    const object = sceneState && sceneState.proposed[0];
    const metrics = object && evaluation ? evaluation.derived_geometry_metrics[object.id] : null;
    const facts = [
      ["Scenario", scenario ? scenario.scenario_id : "Unsaved session"],
      ["Revision", scenario ? scenario.revision : "-"],
      ["Capacity", object ? `${Number(object.attributes.capacity_mw)} MW` : "No proposal"],
      ["Footprint", metrics ? `${Number(metrics.footprint_area_m2).toLocaleString()} m2` : object ? `${(Number(object.geometry_local.width_m) * Number(object.geometry_local.length_m)).toLocaleString()} m2` : "-"],
      ["Evaluation", evaluation ? evaluation.overall_status : "UNRESOLVED"],
    ];
    const element = document.getElementById("scenarioFacts");
    element.replaceChildren(...facts.map(([term, value]) => {
      const row = document.createElement("div");
      const label = document.createElement("dt");
      const detail = document.createElement("dd");
      label.textContent = term;
      detail.textContent = value;
      row.append(label, detail);
      return row;
    }));
  }

  function renderChatResult(result) {
    const chatState = document.getElementById("chatState");
    const response = document.getElementById("chatResponse");
    const trace = document.getElementById("chatTrace");
    const evaluation = document.getElementById("chatEvaluation");
    response.textContent = result.message;
    trace.textContent = result.tool_trace.map((item) => `${item.status.toUpperCase()} ${item.tool}`).join(" | ");
    trace.hidden = !trace.textContent;
    if (result.evaluation) {
      renderEvaluation(evaluation, result.evaluation);
      chatState.textContent = result.evaluation.overall_status;
      chatState.className = `sandbox-state ${result.evaluation.overall_status.toLowerCase()}`;
    } else {
      evaluation.hidden = true;
      chatState.textContent = "READY";
      chatState.className = "sandbox-state derived";
    }
    if (result.scenario) registerScenario(result.scenario, result.evaluation);
    else renderScenarioFacts(null, result.evaluation);
  }

  function registerScenario(scenario, evaluation = scenario.evaluation) {
    activeScenarioId = scenario.scenario_id;
    scenarioRevisions.set(scenario.scenario_id, scenario.revision);
    const select = document.getElementById("scenarioSelect");
    let option = Array.from(select.options).find((item) => item.value === scenario.scenario_id);
    if (!option) {
      option = document.createElement("option");
      option.value = scenario.scenario_id;
      select.append(option);
    }
    option.textContent = `${scenario.scenario_id.slice(0, 12)} / rev ${scenario.revision}`;
    select.value = scenario.scenario_id;
    const compareSelect = document.getElementById("scenarioCompareSelect");
    let compareOption = Array.from(compareSelect.options).find((item) => item.value === scenario.scenario_id);
    if (!compareOption) {
      compareOption = document.createElement("option");
      compareOption.value = scenario.scenario_id;
      compareSelect.append(compareOption);
    }
    compareOption.textContent = option.textContent;
    document.getElementById("scenarioTitle").textContent = scenario.scenario_id;
    document.getElementById("scenarioRevision").textContent = `REV ${scenario.revision}`;
    document.getElementById("scenarioBranch").disabled = false;
    document.getElementById("scenarioCompare").disabled = scenarioRevisions.size < 2;
    renderScenarioFacts(scenario, evaluation);
  }

  function applyScenarioScene(nextScene) {
    sceneState = nextScene;
    window.MireyeSandboxScene = sceneState;
    setSourceData("sandbox-proposed", proposedFeatures());
    syncControls();
    renderScenarioFacts(activeScenarioId ? { scenario_id: activeScenarioId, revision: scenarioRevisions.get(activeScenarioId) } : null, null);
  }

  async function saveScenario() {
    const response = await fetch(scenarioEndpoint(), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        workspace_id: snapshotData.workspace_id,
        user_intent: document.getElementById("scenarioIntent").value.trim(),
        scene_state: sceneState,
      }),
    });
    const scenario = await response.json();
    if (!response.ok) throw new Error(scenario.detail || "Scenario save failed.");
    registerScenario(scenario);
  }

  async function branchScenario() {
    if (!activeScenarioId) return;
    const response = await fetch(`/v1/sandbox/scenarios/${encodeURIComponent(activeScenarioId)}/branch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_intent: document.getElementById("scenarioIntent").value.trim() }),
    });
    const scenario = await response.json();
    if (!response.ok) throw new Error(scenario.detail || "Scenario branch failed.");
    applyScenarioScene(scenario.scene_state);
    registerScenario(scenario);
  }

  async function selectScenario() {
    const scenarioId = document.getElementById("scenarioSelect").value;
    if (!scenarioId) return;
    const response = await fetch(`/v1/sandbox/scenarios/${encodeURIComponent(scenarioId)}`);
    const scenario = await response.json();
    if (!response.ok) throw new Error(scenario.detail || "Scenario load failed.");
    applyScenarioScene(scenario.scene_state);
    registerScenario(scenario);
  }

  async function compareScenarios() {
    const other = document.getElementById("scenarioCompareSelect").value;
    if (!activeScenarioId || !other || activeScenarioId === other) return;
    const response = await fetch("/v1/sandbox/compare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ left_scenario_id: activeScenarioId, right_scenario_id: other }),
    });
    const comparison = await response.json();
    if (!response.ok) throw new Error(comparison.detail || "Scenario comparison failed.");
    const output = document.getElementById("scenarioComparison");
    output.textContent = `${comparison.dominance.result.toUpperCase()} | ${comparison.what_changed.join(" ")}`;
    output.hidden = false;
  }

  async function sendChat() {
    const input = document.getElementById("chatMessage");
    const button = document.getElementById("chatSubmit");
    const message = input.value.trim();
    if (!message) return;
    button.disabled = true;
    document.getElementById("chatState").textContent = "WORKING";
    document.getElementById("chatState").className = "sandbox-state derived";
    try {
      const response = await fetch(chatEndpoint(), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message, session_id: chatSessionId, workspace_id: snapshotData.workspace_id, scenario_id: activeScenarioId }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Sandbox chat failed.");
      applyScenarioScene(result.scene_state);
      renderChatResult(result);
      input.value = "";
    } catch (error) {
      document.getElementById("chatResponse").textContent = error.message || "Sandbox chat failed.";
      document.getElementById("chatState").textContent = "UNAVAILABLE";
      document.getElementById("chatState").className = "sandbox-state fail";
    } finally {
      button.disabled = false;
    }
  }

  function renderFacts(snapshot) {
    const identity = snapshot.parcel_identity;
    const evidence = snapshot.evidence;
    const areaM2 = Number(evidence.parcel_area_m2 && evidence.parcel_area_m2.value);
    const area = Number.isFinite(areaM2) ? `${areaM2.toLocaleString()} m2 / ${(areaM2 / 4046.8564224).toFixed(2)} acres` : "Not provided";
    const freshness = snapshot.is_expired ? "STALE" : `CURRENT until ${new Date(snapshot.expires_at * 1000).toLocaleString()}`;
    const facts = [
      ["Parcel", identity.parcel_id],
      ["Address", identity.parcel_address || "Not provided"],
      ["Area", area],
      ["Geometry source", identity.parcel_data_source || "MIREYE"],
      ["Match", `${identity.parcel_match_type} / ${identity.parcel_match_distance_m} m`],
      ["Zoning", evidence.parcel_zoning.value || "Not provided"],
      ["Evidence freshness", freshness],
    ];
    const factsElement = document.getElementById("snapshotFacts");
    factsElement.replaceChildren(...facts.map(([term, value]) => {
      const row = document.createElement("div");
      const label = document.createElement("dt");
      const detail = document.createElement("dd");
      label.textContent = term;
      detail.textContent = value;
      row.append(label, detail);
      return row;
    }));
    document.getElementById("parcelTitle").textContent = identity.parcel_address || identity.parcel_id;
    document.getElementById("parcelMeta").textContent = `${identity.parcel_id} | ${identity.parcel_match_type}`;
    document.getElementById("snapshotState").textContent = snapshot.is_expired ? "SNAPSHOT STALE" : "MIREYE SNAPSHOT";
  }

  function addSceneLayers() {
    const parcel = sceneState.observed.find((object) => object.id === "parcel_boundary");
    const point = sceneState.observed.find((object) => object.id === "resolution_point");
    const centroid = sceneState.derived.find((object) => object.id === "parcel_centroid");
    const ground = sceneState.derived.find((object) => object.id === "flat_ground_plane");

    map.addSource("sandbox-ground", { type: "geojson", data: { type: "Feature", geometry: ground.geometry } });
    map.addLayer({ id: "sandbox-ground-fill", type: "fill", source: "sandbox-ground", paint: { "fill-color": "#64748b", "fill-opacity": 0.12 } });
    map.addSource("sandbox-parcel", { type: "geojson", data: { type: "Feature", geometry: parcel.geometry } });
    map.addLayer({ id: "sandbox-parcel-fill", type: "fill", source: "sandbox-parcel", paint: { "fill-color": "#0e7490", "fill-opacity": 0.18 } });
    map.addLayer({ id: "sandbox-parcel-line", type: "line", source: "sandbox-parcel", paint: { "line-color": "#0e7490", "line-width": 3 } });
    map.addSource("sandbox-point", { type: "geojson", data: { type: "Feature", geometry: point.geometry } });
    map.addLayer({ id: "sandbox-point-circle", type: "circle", source: "sandbox-point", paint: { "circle-radius": 7, "circle-color": "#0e7490", "circle-stroke-width": 2, "circle-stroke-color": "#ffffff" } });
    map.addSource("sandbox-centroid", { type: "geojson", data: { type: "Feature", geometry: centroid.geometry } });
    map.addLayer({ id: "sandbox-centroid-circle", type: "circle", source: "sandbox-centroid", paint: { "circle-radius": 4, "circle-color": "#6d28d9" } });
    map.addSource("sandbox-proposed", { type: "geojson", data: proposedFeatures() });
    map.addLayer({
      id: "sandbox-proposed-extrusion",
      type: "fill-extrusion",
      source: "sandbox-proposed",
      paint: {
        "fill-extrusion-color": "#f97316",
        "fill-extrusion-height": ["get", "height_m"],
        "fill-extrusion-base": 0,
        "fill-extrusion-opacity": 0.78,
      },
    });
    const bounds = new maplibregl.LngLatBounds();
    const extendCoordinates = (coordinates) => {
      if (typeof coordinates[0] === "number") bounds.extend(coordinates);
      else coordinates.forEach(extendCoordinates);
    };
    extendCoordinates(parcel.geometry.coordinates);
    map.fitBounds(bounds, { padding: 64, maxZoom: 18, duration: 0 });
  }

  function initializeMap() {
    if (!window.maplibregl) {
      showError("MapLibre GL JS failed to load.");
      return;
    }
    map = new maplibregl.Map({
      container: "sandboxMap",
      style: {
        version: 8,
        sources: {},
        layers: [{ id: "conceptual-background", type: "background", paint: { "background-color": "#dbeafe" } }],
      },
      center: [sceneState.camera.center.lng, sceneState.camera.center.lat],
      zoom: sceneState.camera.zoom,
      pitch: sceneState.camera.pitch,
      bearing: sceneState.camera.bearing,
      maxPitch: 75,
      attributionControl: true,
    });
    map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");
    map.on("load", addSceneLayers);
    map.on("moveend", () => {
      const center = map.getCenter();
      sceneState.camera = { center: { lng: center.lng, lat: center.lat }, zoom: map.getZoom(), pitch: map.getPitch(), bearing: map.getBearing() };
      document.getElementById("cameraReadout").textContent = `PITCH ${Math.round(sceneState.camera.pitch)} | BEARING ${Math.round(sceneState.camera.bearing)}`;
    });
  }

  async function loadSandbox() {
    const id = snapshotId();
    if (!id) {
      showError("A SiteSnapshot ID is required.");
      return;
    }
    try {
      const [sceneResponse, snapshotResponse] = await Promise.all([fetch(sceneEndpoint()), fetch(snapshotEndpoint())]);
      if (!sceneResponse.ok || !snapshotResponse.ok) {
        const response = !sceneResponse.ok ? sceneResponse : snapshotResponse;
        const error = await response.json().catch(() => ({}));
        throw new Error(error.detail || `Snapshot request failed (${response.status}).`);
      }
      sceneState = await sceneResponse.json();
      const snapshot = await snapshotResponse.json();
      snapshotData = snapshot;
      const initialScenarioId = requestedScenarioId();
      if (initialScenarioId) {
        const scenarioResponse = await fetch(`/v1/sandbox/scenarios/${encodeURIComponent(initialScenarioId)}`);
        const scenario = await scenarioResponse.json();
        if (!scenarioResponse.ok) throw new Error(scenario.detail || "Initial scenario request failed.");
        if (scenario.site_snapshot_id !== id) throw new Error("Initial scenario does not reference this SiteSnapshot.");
        sceneState = scenario.scene_state;
        registerScenario(scenario);
      } else {
        renderScenarioFacts(null, null);
      }
      window.MireyeSandboxScene = sceneState;
      renderFacts(snapshot);
      syncControls();
      initializeMap();
      bindControls();
      document.querySelectorAll(".sandbox-demo-prompts button").forEach((button) => {
        button.addEventListener("click", () => {
          document.getElementById("chatMessage").value = button.textContent;
          document.getElementById("chatMessage").focus();
        });
      });
      document.getElementById("chatSubmit").addEventListener("click", sendChat);
      document.getElementById("scenarioSave").addEventListener("click", () => saveScenario().catch(showError));
      document.getElementById("scenarioBranch").addEventListener("click", () => branchScenario().catch(showError));
      document.getElementById("scenarioSelect").addEventListener("change", () => selectScenario().catch(showError));
      document.getElementById("scenarioCompare").addEventListener("click", () => compareScenarios().catch(showError));
    } catch (error) {
      showError(error.message || "Unable to load SiteSnapshot.");
    }
  }

  document.addEventListener("DOMContentLoaded", loadSandbox);
})();
