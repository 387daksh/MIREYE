(() => {
  const EARTH_RADIUS_M = 6371008.8;
  const sceneEndpoint = () => `/v1/sandbox/site/snapshots/${encodeURIComponent(snapshotId())}/scene`;
  const snapshotEndpoint = () => `/v1/sandbox/site/snapshots/${encodeURIComponent(snapshotId())}`;
  const chatEndpoint = () => `/v1/sandbox/${encodeURIComponent(snapshotId())}/chat`;
  const scenarioEndpoint = () => `/v1/sandbox/${encodeURIComponent(snapshotId())}/scenarios`;
  const freshnessEndpoint = () => `/v1/sandbox/site/${encodeURIComponent(snapshotId())}/freshness`;
  const refreshQuoteEndpoint = () => `/v1/sandbox/site/${encodeURIComponent(snapshotId())}/refresh/quote`;
  let sceneState;
  let snapshotData;
  let worldData = null;
  let activeScenarioId = null;
  const scenarioRevisions = new Map();
  let activeSpendPlan = null;
  let map;
  const chatSessionId = window.crypto && window.crypto.randomUUID ? window.crypto.randomUUID() : `sandbox-${Date.now()}`;

  function snapshotId() {
    const parts = window.location.pathname.split("/").filter(Boolean);
    return decodeURIComponent(parts[parts.length - 1] || "");
  }

  function requestedScenarioId() {
    return new URLSearchParams(window.location.search).get("scenario");
  }

  function requestedWorldSnapshotId() {
    return new URLSearchParams(window.location.search).get("world");
  }

  function showError(message) {
    const error = document.getElementById("sandboxError");
    error.textContent = message;
    error.hidden = false;
    document.getElementById("snapshotState").textContent = "Site unavailable";
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

  function syncControls() {
    const object = sceneState.proposed[0];
    const facts = object ? [
      ["Capacity", `${Number(object.attributes.capacity_mw)} MW`],
      ["Footprint", `${Number(object.geometry_local.width_m)} × ${Number(object.geometry_local.length_m)} m`],
      ["Height", `${Number(object.geometry_local.height_m)} m`],
      ["Rotation", `${Number(object.geometry_local.rotation_deg)}°`],
    ] : [["Status", "No proposed design"]];
    renderFactsList(document.getElementById("designFacts"), facts);
  }

  const constraintNames = {
    footprint_inside_parcel: "Fits inside parcel", minimum_setback: "Setback", footprint_area: "Footprint area",
    parcel_coverage: "Parcel coverage", object_collision: "Blocked areas", resolution_point_outside_fema_sfha: "Flood at resolution point",
    max_nwi_wetland_fraction_of_parcel: "Mapped wetlands", max_nwi_wetland_acres_on_parcel: "Mapped wetlands",
    max_resolution_point_slope_degrees: "Slope at resolution point", max_resolution_point_substation_distance_m: "Substation proximity",
    max_resolution_point_transmission_distance_m: "Transmission proximity", max_resolution_point_major_road_distance_m: "Road proximity",
    parcel_zoning_code_in: "Raw zoning code", parcel_outside_fema_sfha: "Whole-parcel flood", industrial_zoning: "Industrial zoning",
    sufficient_grid_capacity: "Grid capacity", legal_access: "Legal road access",
  };

  function constraintName(id) {
    return constraintNames[id] || String(id).replaceAll("_", " ").replace(/^./, (value) => value.toUpperCase());
  }

  function renderFactsList(element, facts) {
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

  function stateBadge(outcome) {
    const badge = document.createElement("span");
    badge.className = `sandbox-state ${String(outcome).toLowerCase()}`;
    badge.textContent = outcome;
    return badge;
  }

  function renderEvaluation(container, evaluation) {
    container.replaceChildren();
    const summary = document.createElement("div");
    summary.className = "evaluation-summary";
    summary.append(stateBadge(evaluation.overall_status));
    const summaryText = document.createElement("span");
    summaryText.textContent = `${evaluation.constraint_results.length} requirement${evaluation.constraint_results.length === 1 ? "" : "s"} checked`;
    summary.append(summaryText);
    container.append(summary);
    evaluation.constraint_results.forEach((item) => {
      const row = document.createElement("div");
      row.className = "evaluation-item";
      row.append(stateBadge(item.outcome));
      const copy = document.createElement("div");
      copy.className = "evaluation-copy";
      const title = document.createElement("strong");
      title.textContent = constraintName(item.constraint_id);
      const reason = document.createElement("p");
      reason.textContent = item.explanation;
      copy.append(title, reason);
      row.append(copy);
      container.append(row);
    });
    container.hidden = false;
  }

  function renderScenarioFacts(scenario, evaluation) {
    const object = sceneState && sceneState.proposed[0];
    const metrics = object && evaluation ? evaluation.derived_geometry_metrics[object.id] : null;
    const facts = [
      ["Capacity", object ? `${Number(object.attributes.capacity_mw)} MW` : "No proposal"],
      ["Footprint", metrics ? `${Number(metrics.footprint_area_m2).toLocaleString()} m²` : object ? `${(Number(object.geometry_local.width_m) * Number(object.geometry_local.length_m)).toLocaleString()} m²` : "-"],
      ["Evaluation", evaluation ? evaluation.overall_status : "UNRESOLVED"],
    ];
    renderFactsList(document.getElementById("scenarioFacts"), facts);
  }

  function renderChatResult(result) {
    const chatState = document.getElementById("chatState");
    const response = document.getElementById("chatResponse");
    const trace = document.getElementById("chatTrace");
    const evaluation = document.getElementById("chatEvaluation");
    response.textContent = result.message;
    const activity = {
      get_site_context: "Reviewed the site", propose_data_center: "Created a proposed facility", transform_object: "Updated the layout",
      evaluate_scenario: "Re-evaluated the site", get_evidence: "Reviewed MIREYE sources", remove_object: "Removed a proposed object",
      reset_proposals: "Reset proposed designs", check_evidence_freshness: "Checked MIREYE freshness",
      quote_mireye_refresh: "Prepared a refresh estimate", confirm_and_refresh_evidence: "Refreshed MIREYE intelligence",
    };
    trace.textContent = result.tool_trace.map((item) => activity[item.tool] || "Completed a validated site action").join(" · ");
    trace.hidden = !trace.textContent;
    if (result.evaluation) {
      renderEvaluation(evaluation, result.evaluation);
      chatState.textContent = `Evaluation: ${result.evaluation.overall_status}`;
    } else {
      evaluation.hidden = true;
      chatState.textContent = "Ready";
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
    const label = scenarioLabel(scenario.scenario_id);
    const scenarioScene = scenario.scene_state || sceneState;
    option.textContent = `${label} · ${scenarioScene.proposed[0] ? Number(scenarioScene.proposed[0].attributes.capacity_mw) + " MW" : "No proposal"}`;
    select.value = scenario.scenario_id;
    const compareSelect = document.getElementById("scenarioCompareSelect");
    let compareOption = Array.from(compareSelect.options).find((item) => item.value === scenario.scenario_id);
    if (!compareOption) {
      compareOption = document.createElement("option");
      compareOption.value = scenario.scenario_id;
      compareSelect.append(compareOption);
    }
    compareOption.textContent = option.textContent;
    document.getElementById("scenarioTitle").textContent = label;
    document.getElementById("scenarioRevision").textContent = "Saved";
    document.getElementById("scenarioBranch").disabled = false;
    document.getElementById("scenarioCompare").disabled = scenarioRevisions.size < 2;
    renderScenarioFacts(scenario, evaluation);
  }

  function scenarioLabel(scenarioId) {
    const ids = Array.from(scenarioRevisions.keys());
    const index = ids.indexOf(scenarioId);
    return `Scenario ${String.fromCharCode(65 + Math.max(0, index))}`;
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
        world_snapshot_id: sceneState.world_snapshot_id || null,
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
    const leftName = scenarioLabel(activeScenarioId);
    const rightName = scenarioLabel(other);
    const changedConstraints = Object.entries(comparison.constraint_changes || {}).map(([id, values]) => [
      constraintName(id), values.before ? values.before.outcome : "—", values.after ? values.after.outcome : "—",
    ]);
    const rows = changedConstraints.length ? changedConstraints : [["Deterministic outcome", "No change", "No change"]];
    output.replaceChildren();
    const heading = document.createElement("div");
    heading.className = "comparison-row";
    ["", leftName, rightName].forEach((text) => { const cell = document.createElement("span"); cell.textContent = text; heading.append(cell); });
    output.append(heading);
    rows.forEach((values) => {
      const row = document.createElement("div"); row.className = "comparison-row";
      values.forEach((text) => { const cell = document.createElement("span"); cell.textContent = text; row.append(cell); });
      output.append(row);
    });
    const summary = document.createElement("div");
    summary.className = "comparison-summary";
    summary.textContent = comparison.dominance.result === "neither"
      ? `${comparison.what_changed.join(" ")} Neither option dominates under the current deterministic evaluation.`
      : `${comparison.dominance.result === "left" ? leftName : rightName} is stronger under the matching evaluated requirements.`;
    output.append(summary);
    output.hidden = false;
  }

  async function sendChat() {
    const input = document.getElementById("chatMessage");
    const button = document.getElementById("chatSubmit");
    const message = input.value.trim();
    if (!message) return;
    button.disabled = true;
    document.getElementById("chatState").textContent = "Working with the site…";
    try {
      const response = await fetch(chatEndpoint(), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message, session_id: chatSessionId, workspace_id: snapshotData.workspace_id, scenario_id: activeScenarioId, world_snapshot_id: sceneState.world_snapshot_id || null }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Sandbox chat failed.");
      applyScenarioScene(result.scene_state);
      renderChatResult(result);
      input.value = "";
    } catch (error) {
      document.getElementById("chatResponse").textContent = error.message || "Sandbox chat failed.";
      document.getElementById("chatState").textContent = "Mireye couldn't complete that change.";
    } finally {
      button.disabled = false;
    }
  }

  function renderFacts(snapshot) {
    const identity = snapshot.parcel_identity;
    const evidence = snapshot.evidence;
    const areaM2 = Number(evidence.parcel_area_m2 && evidence.parcel_area_m2.value);
    const area = Number.isFinite(areaM2) ? `${areaM2.toLocaleString()} m2 / ${(areaM2 / 4046.8564224).toFixed(2)} acres` : "Not provided";
    const facts = [
      ["Area", area],
      ["Zoning code", evidence.parcel_zoning.value || "Unresolved"],
      ["Parcel match", identity.parcel_match_type === "exact_intersect" ? "Verified exact" : "Unresolved"],
    ];
    renderFactsList(document.getElementById("snapshotFacts"), facts);
    document.getElementById("parcelTitle").textContent = identity.parcel_address || identity.parcel_id;
    document.getElementById("siteName").textContent = identity.parcel_address || "Verified property";
    document.getElementById("parcelMeta").textContent = `${identity.parcel_data_source || "MIREYE"} · exact parcel match`;
    document.getElementById("snapshotState").textContent = snapshot.is_expired ? "Site intelligence needs an update" : "MIREYE intelligence current";
    renderSources(snapshot);
  }

  function renderSources(snapshot) {
    const now = Date.now() / 1000;
    const rows = Object.values(snapshot.evidence || {}).sort((a, b) => String(a.field).localeCompare(String(b.field))).map((record) => {
      const row = document.createElement("div");
      row.className = "source-row";
      const name = document.createElement("strong");
      const source = document.createElement("span");
      const freshness = document.createElement("span");
      name.textContent = constraintName(record.field);
      source.textContent = `${record.source || "MIREYE source"} · ${record.confidence || "confidence not stated"}`;
      const fresh = Number(record.expires_at) > now && record.value != null && ["ok", null, undefined].includes(record.status);
      freshness.className = fresh ? "fresh" : "stale";
      freshness.textContent = fresh ? `Current · captured ${relativeTime(record.observed_at)}` : "Stale or unresolved";
      row.append(name, source, freshness);
      return row;
    });
    document.getElementById("sourcesList").replaceChildren(...rows);
  }

  function relativeTime(timestamp) {
    const seconds = Math.max(0, Math.round(Date.now() / 1000 - Number(timestamp || 0)));
    if (seconds < 60) return "just now";
    if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} hr ago`;
    return `${Math.floor(seconds / 86400)} days ago`;
  }

  function renderMireyeFreshness(payload) {
    const fresh = payload.fresh_fields || [];
    const stale = [...(payload.stale_fields || []), ...(payload.missing_fields || []), ...(payload.incompatible_fields || []), ...(payload.deprecated_fields || [])];
    const state = document.getElementById("mireyeFreshnessState");
    const cached = Object.values(snapshotData.evidence || {}).filter((record) => record.carried_from_snapshot_id).length;
    renderFactsList(document.getElementById("mireyeFreshnessFacts"), [["Verified", String(fresh.length)], ["Unresolved", String(stale.length)], ["Cached", String(cached)]]);
    state.className = `status-dot ${payload.refresh_required ? "unresolved" : "pass"}`;
    document.getElementById("intelligenceHeadline").textContent = payload.refresh_required ? "Update recommended" : `Refreshed ${relativeTime(snapshotData.observed_at)}`;
    document.getElementById("mireyeRefreshQuote").disabled = !payload.refresh_required;
    document.getElementById("mireyeRefreshNote").textContent = payload.refresh_required
      ? `${stale.length} field${stale.length === 1 ? " is" : "s are"} stale or unresolved.`
      : `${fresh.length} fields are within their recorded freshness windows.`;
  }

  async function loadMireyeFreshness() {
    const response = await fetch(freshnessEndpoint());
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "Evidence freshness check failed.");
    renderMireyeFreshness(payload);
  }

  async function quoteMireyeRefresh() {
    const button = document.getElementById("mireyeRefreshQuote");
    button.disabled = true;
    try {
      const response = await fetch(refreshQuoteEndpoint(), { method: "POST" });
      const plan = await response.json();
      if (!response.ok) throw new Error(plan.detail || "MIREYE refresh quote failed.");
      if (plan.status === "NO_REFRESH_REQUIRED") {
        renderMireyeFreshness(plan.freshness);
        return;
      }
      activeSpendPlan = plan;
      const credits = plan.expected_credits == null ? "an unavailable credit estimate" : `${plan.expected_credits} credits`;
      const expiry = new Date(plan.quote_expires_at * 1000).toLocaleString();
      document.getElementById("refreshQuoteText").textContent = `${plan.requested_fields.length} fields need an update. Estimated cost: ${credits}. Quote expires ${expiry}. Nothing has been charged.`;
      document.getElementById("refreshConfirmPanel").hidden = false;
      document.getElementById("mireyeRefreshConfirm").disabled = false;
    } catch (error) {
      document.getElementById("mireyeRefreshNote").textContent = error.message || "MIREYE couldn't prepare a refresh estimate.";
    } finally {
      button.disabled = false;
    }
  }

  async function confirmMireyeRefresh() {
    if (!activeSpendPlan) return;
    const button = document.getElementById("mireyeRefreshConfirm");
    button.disabled = true;
    try {
      const response = await fetch(`/v1/sandbox/site/refresh/${encodeURIComponent(activeSpendPlan.spend_plan_id)}/confirm`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirmed: true }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "MIREYE refresh failed.");
      window.sessionStorage.setItem("mireyeRefreshResult", JSON.stringify({
        snapshot_id: result.snapshot.snapshot_id,
        changed_values: Object.keys(result.snapshot_diff.field_changes || {}).length,
        reevaluated_scenarios: (result.evaluation_runs || []).length,
      }));
      window.location.assign(`/sandbox/${encodeURIComponent(result.snapshot.snapshot_id)}`);
    } catch (error) {
      document.getElementById("mireyeRefreshNote").textContent = `${error.message || "MIREYE couldn't refresh this site."} Your previous site intelligence is still available.`;
      button.disabled = false;
    }
  }

  function worldLayer(name) {
    return worldData && worldData.layers.find((layer) => layer.layer === name);
  }

  function renderWorld() {
    if (!worldData) return;
    const terrain = worldLayer("terrain");
    const roads = worldLayer("roads");
    const warnings = worldData.layers.flatMap((layer) => layer.warnings || []);
    const conflicts = worldData.quality_conflicts || [];
    const facts = [
      ["Terrain", terrain && terrain.availability === "AVAILABLE" ? `${terrain.terrain.actual_resolution_m} m / ${terrain.terrain.vertical_reference}` : "Unavailable"],
      ["Road features", roads && roads.availability === "AVAILABLE" ? String(roads.roads.feature_count) : "Unavailable"],
      ["Conflicts", String(conflicts.length)],
    ];
    renderFactsList(document.getElementById("worldFacts"), facts);
    const sources = document.getElementById("worldSources");
    sources.replaceChildren(...worldData.source_manifest.map((entry) => {
      const row = document.createElement("div");
      const source = entry.source;
      row.textContent = `${entry.layer}: ${source.provider}, ${source.release || source.source_id || source.dataset}`;
      return row;
    }));
    document.getElementById("worldWarning").textContent = conflicts.length
      ? `CONFLICT: ${conflicts.join(" ")}`
      : warnings.join(" ");
    document.getElementById("worldState").textContent = conflicts.length ? "Source conflict" : "Observed layers";
    document.getElementById("worldPanel").hidden = false;
    document.getElementById("worldDetails").hidden = false;
    document.getElementById("groundStateChip").textContent = terrain && terrain.availability === "AVAILABLE"
      ? `Observed USGS terrain · ${terrain.terrain.actual_resolution_m} m resolution`
      : "Conceptual flat ground";
  }

  async function loadWorld(worldSnapshotId) {
    if (!worldSnapshotId) return null;
    const response = await fetch(`/v1/sandbox/world-snapshots/${encodeURIComponent(worldSnapshotId)}`);
    const world = await response.json();
    if (!response.ok) throw new Error(world.detail || "The observed terrain and road layers could not be loaded.");
    if (world.site_snapshot_id !== snapshotId()) throw new Error("These observed world layers belong to a different site.");
    return world;
  }

  function addWorldLayers() {
    if (!worldData) return;
    const terrain = worldLayer("terrain");
    const roads = worldLayer("roads");
    if (terrain && terrain.availability === "AVAILABLE") {
      map.addSource("world-terrain", {
        type: "raster-dem", tiles: terrain.render.tiles, tileSize: terrain.render.tile_size,
        minzoom: terrain.render.minzoom, maxzoom: terrain.render.maxzoom, encoding: terrain.render.encoding,
      });
      map.setTerrain({ source: "world-terrain", exaggeration: 1 });
      document.getElementById("terrainToggle").disabled = false;
    }
    if (roads && roads.availability === "AVAILABLE") {
      map.addSource("world-roads", { type: "geojson", data: roads.render.url });
      map.addLayer({ id: "world-roads-line", type: "line", source: "world-roads", paint: { "line-color": "#334155", "line-width": 2.4, "line-opacity": 0.9 } });
      document.getElementById("roadsToggle").disabled = false;
    }
  }

  function bindWorldControls() {
    document.getElementById("terrainToggle").addEventListener("change", (event) => {
      if (map && map.getSource("world-terrain")) map.setTerrain(event.target.checked ? { source: "world-terrain", exaggeration: 1 } : null);
    });
    document.getElementById("roadsToggle").addEventListener("change", (event) => {
      if (map && map.getLayer("world-roads-line")) map.setLayoutProperty("world-roads-line", "visibility", event.target.checked ? "visible" : "none");
    });
  }

  function addSceneLayers() {
    const parcel = sceneState.observed.find((object) => object.id === "parcel_boundary");
    const point = sceneState.observed.find((object) => object.id === "resolution_point");
    const centroid = sceneState.derived.find((object) => object.id === "parcel_centroid");
    const ground = sceneState.derived.find((object) => object.id === "flat_ground_plane");

    addWorldLayers();
    map.addSource("sandbox-ground", { type: "geojson", data: { type: "Feature", geometry: ground.geometry } });
    map.addLayer({ id: "sandbox-ground-fill", type: "fill", source: "sandbox-ground", paint: { "fill-color": "#64748b", "fill-opacity": 0.12 } });
    map.addSource("sandbox-parcel", { type: "geojson", data: { type: "Feature", geometry: parcel.geometry } });
    map.addLayer({ id: "sandbox-parcel-fill", type: "fill", source: "sandbox-parcel", paint: { "fill-color": "#0e7490", "fill-opacity": 0.18 } });
    map.addLayer({ id: "sandbox-parcel-line", type: "line", source: "sandbox-parcel", paint: { "line-color": "#0e7490", "line-width": 3 } });
    map.addSource("sandbox-point", { type: "geojson", data: { type: "Feature", geometry: point.geometry } });
    map.addLayer({ id: "sandbox-point-circle", type: "circle", source: "sandbox-point", paint: { "circle-radius": 7, "circle-color": "#0e7490", "circle-stroke-width": 2, "circle-stroke-color": "#ffffff" } });
    map.addSource("sandbox-centroid", { type: "geojson", data: { type: "Feature", geometry: centroid.geometry } });
    map.addLayer({ id: "sandbox-centroid-circle", type: "circle", source: "sandbox-centroid", paint: { "circle-radius": 4, "circle-color": "#315b86" } });
    map.addSource("sandbox-proposed", { type: "geojson", data: proposedFeatures() });
    map.addLayer({
      id: "sandbox-proposed-extrusion",
      type: "fill-extrusion",
      source: "sandbox-proposed",
      paint: {
        "fill-extrusion-color": "#e95920",
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
        layers: [{ id: "conceptual-background", type: "background", paint: { "background-color": "#dfe8e4" } }],
      },
      center: [sceneState.camera.center.lng, sceneState.camera.center.lat],
      zoom: sceneState.camera.zoom,
      pitch: sceneState.camera.pitch,
      bearing: sceneState.camera.bearing,
      maxPitch: 75,
      attributionControl: true,
    });
    window.MireyeSandboxMap = map;
    map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");
    map.on("load", addSceneLayers);
    map.on("moveend", () => {
      const center = map.getCenter();
      sceneState.camera = { center: { lng: center.lng, lat: center.lat }, zoom: map.getZoom(), pitch: map.getPitch(), bearing: map.getBearing() };
      document.getElementById("cameraReadout").textContent = `${Math.round(sceneState.camera.pitch)}° pitch · ${Math.round(sceneState.camera.bearing)}° bearing`;
    });
  }

  async function loadSandbox() {
    const id = snapshotId();
    if (!id) {
      showError("Choose a verified site before opening the workspace.");
      return;
    }
    try {
      const [sceneResponse, snapshotResponse] = await Promise.all([fetch(sceneEndpoint()), fetch(snapshotEndpoint())]);
      if (!sceneResponse.ok || !snapshotResponse.ok) {
        const response = !sceneResponse.ok ? sceneResponse : snapshotResponse;
        const error = await response.json().catch(() => ({}));
        throw new Error(error.detail || "The site could not be loaded.");
      }
      sceneState = await sceneResponse.json();
      const snapshot = await snapshotResponse.json();
      snapshotData = snapshot;
      const initialScenarioId = requestedScenarioId();
      if (initialScenarioId) {
        const scenarioResponse = await fetch(`/v1/sandbox/scenarios/${encodeURIComponent(initialScenarioId)}`);
        const scenario = await scenarioResponse.json();
        if (!scenarioResponse.ok) throw new Error(scenario.detail || "The requested design option could not be loaded.");
        if (scenario.site_snapshot_id !== id) throw new Error("That design option belongs to a different site.");
        sceneState = scenario.scene_state;
        registerScenario(scenario);
      } else {
        const requestedWorldId = requestedWorldSnapshotId();
        if (requestedWorldId) sceneState.world_snapshot_id = requestedWorldId;
        renderScenarioFacts(null, null);
      }
      worldData = await loadWorld(sceneState.world_snapshot_id);
      renderWorld();
      window.MireyeSandboxScene = sceneState;
      renderFacts(snapshot);
      try {
        await loadMireyeFreshness();
        const refreshResult = JSON.parse(window.sessionStorage.getItem("mireyeRefreshResult") || "null");
        if (refreshResult && refreshResult.snapshot_id === id) {
          document.getElementById("intelligenceHeadline").textContent = "MIREYE refreshed the site";
          document.getElementById("mireyeRefreshNote").textContent = `${refreshResult.changed_values} value${refreshResult.changed_values === 1 ? "" : "s"} changed. ${refreshResult.reevaluated_scenarios} scenario${refreshResult.reevaluated_scenarios === 1 ? " was" : "s were"} re-evaluated.`;
          window.sessionStorage.removeItem("mireyeRefreshResult");
        }
      } catch (error) {
        document.getElementById("mireyeFreshnessState").className = "status-dot fail";
        document.getElementById("intelligenceHeadline").textContent = "Freshness unavailable";
        document.getElementById("mireyeRefreshNote").textContent = error.message || "MIREYE freshness could not be checked.";
      }
      syncControls();
      initializeMap();
      bindWorldControls();
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
      document.getElementById("mireyeRefreshQuote").addEventListener("click", () => quoteMireyeRefresh().catch(showError));
      document.getElementById("mireyeRefreshConfirm").addEventListener("click", () => confirmMireyeRefresh().catch(showError));
      document.getElementById("mireyeRefreshCancel").addEventListener("click", () => {
        activeSpendPlan = null;
        document.getElementById("refreshConfirmPanel").hidden = true;
        document.getElementById("mireyeRefreshConfirm").disabled = true;
      });
      document.getElementById("viewSources").addEventListener("click", () => document.getElementById("sourcesDialog").showModal());
      document.getElementById("closeSources").addEventListener("click", () => document.getElementById("sourcesDialog").close());
      document.getElementById("chatMessage").addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); sendChat(); }
      });
    } catch (error) {
      showError(error.message || "Unable to load this site.");
    }
  }

  document.addEventListener("DOMContentLoaded", loadSandbox);
})();
