(() => {
  const EARTH_RADIUS_M = 6371008.8;
  const sceneEndpoint = () => `/v1/sandbox/site/snapshots/${encodeURIComponent(snapshotId())}/scene`;
  const snapshotEndpoint = () => `/v1/sandbox/site/snapshots/${encodeURIComponent(snapshotId())}`;
  const chatEndpoint = () => `/v1/sandbox/${encodeURIComponent(snapshotId())}/chat`;
  const scenarioEndpoint = () => `/v1/sandbox/${encodeURIComponent(snapshotId())}/scenarios`;
  const freshnessEndpoint = () => `/v1/sandbox/site/${encodeURIComponent(snapshotId())}/freshness`;
  const refreshQuoteEndpoint = () => `/v1/sandbox/site/${encodeURIComponent(snapshotId())}/refresh/quote?profile=data_center_siting`;
  let sceneState;
  let snapshotData;
  let worldData = null;
  let intelligencePlan = null;
  let activeScenarioId = null;
  const scenarioRevisions = new Map();
  const scenarioCache = new Map();
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

  function rectangleFeature(id, geometry, properties) {
    const halfWidth = Number(geometry.width_m) / 2;
    const halfLength = Number(geometry.length_m) / 2;
    const radians = Number(geometry.rotation_deg) * Math.PI / 180;
    const corners = [[-halfWidth, -halfLength], [halfWidth, -halfLength], [halfWidth, halfLength], [-halfWidth, halfLength]];
    const coordinates = corners.map(([x, y]) => localToLngLat(
      x * Math.cos(radians) - y * Math.sin(radians) + Number(geometry.center_xy_m[0]),
      x * Math.sin(radians) + y * Math.cos(radians) + Number(geometry.center_xy_m[1]),
      sceneState.frame,
    ));
    coordinates.push(coordinates[0]);
    return { type: "Feature", geometry: { type: "Polygon", coordinates: [coordinates] }, properties: { id, ...properties } };
  }

  function componentGeometry(parentGeometry, relative) {
    const radians = Number(parentGeometry.rotation_deg) * Math.PI / 180;
    const x = Number(relative.center_uv[0]) * Number(parentGeometry.width_m);
    const y = Number(relative.center_uv[1]) * Number(parentGeometry.length_m);
    return {
      center_xy_m: [
        Number(parentGeometry.center_xy_m[0]) + x * Math.cos(radians) - y * Math.sin(radians),
        Number(parentGeometry.center_xy_m[1]) + x * Math.sin(radians) + y * Math.cos(radians),
      ],
      width_m: Number(relative.width_ratio) * Number(parentGeometry.width_m),
      length_m: Number(relative.length_ratio) * Number(parentGeometry.length_m),
      height_m: Number(relative.height_m),
      rotation_deg: Number(parentGeometry.rotation_deg) + Number(relative.rotation_offset_deg || 0),
    };
  }

  function proposedFeatures() {
    const features = [];
    sceneState.proposed.forEach((object) => {
      const parentClass = object.components ? "campus_boundary" : (object.render_class || "building");
      features.push(rectangleFeature(object.id, object.geometry_local, {
        label: object.components ? "Campus planning envelope" : object.kind,
        kind: object.kind,
        render_class: parentClass,
        semantic_class: object.semantic_class || `proposed_${object.kind}`,
        height_m: parentClass === "building" ? Number(object.geometry_local.height_m) : 0,
        capacity_mw: Number(object.attributes.capacity_mw),
      }));
      (object.components || []).forEach((component) => {
        const geometry = componentGeometry(object.geometry_local, component.geometry_relative);
        features.push(rectangleFeature(component.id, geometry, {
          label: component.label,
          kind: component.kind,
          render_class: component.render_class,
          semantic_class: component.semantic_class,
          phase: component.attributes.phase,
          height_m: Number(geometry.height_m),
          capacity_mw: Number(component.attributes.capacity_mw || 0),
        }));
      });
    });
    return { type: "FeatureCollection", features };
  }

  function setSourceData(sourceId, data) {
    if (!map) return;
    const source = map.getSource(sourceId);
    if (source) source.setData(data);
  }

  function syncControls() {
    const object = sceneState.proposed[0];
    const area = object ? Number(object.geometry_local.width_m) * Number(object.geometry_local.length_m) : 0;
    const facts = object ? [
      ["Phase 1", `${Number(object.attributes.capacity_mw)} MW`],
      ["Expansion target", `${Number(object.attributes.expansion_target_mw || object.attributes.capacity_mw)} MW`],
      ["Planning envelope", `${Math.round(area).toLocaleString()} m²`],
      ["Campus elements", String((object.components || []).length)],
    ] : [["Status", "No proposed design"]];
    renderFactsList(document.getElementById("designFacts"), facts);
    const components = object ? object.components || [] : [];
    document.getElementById("campusElements").replaceChildren(...components.map((component) => {
      const item = document.createElement("li");
      const name = document.createElement("span");
      const phase = document.createElement("small");
      name.textContent = component.label;
      phase.textContent = component.attributes.phase === "FUTURE" ? "Future" : "Phase 1";
      item.append(name, phase);
      return item;
    }));
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
      ["Land envelope", metrics ? `${Number(metrics.footprint_area_m2).toLocaleString()} m²` : object ? `${(Number(object.geometry_local.width_m) * Number(object.geometry_local.length_m)).toLocaleString()} m²` : "-"],
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
      get_site_context: "Reviewed the site", propose_data_center: "Created a conceptual campus", transform_object: "Updated the layout",
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
    scenarioCache.set(scenario.scenario_id, scenario);
    const select = document.getElementById("scenarioSelect");
    let option = Array.from(select.options).find((item) => item.value === scenario.scenario_id);
    if (!option) {
      option = document.createElement("option");
      option.value = scenario.scenario_id;
      select.append(option);
    }
    const label = scenarioLabel(scenario.scenario_id);
    const scenarioScene = scenario.scene_state || sceneState;
    const proposal = scenarioScene.proposed[0];
    option.textContent = `${label} · ${proposal ? `${Number(proposal.attributes.capacity_mw)} MW / ${String(proposal.attributes.layout_strategy || "concept").replaceAll("_", " ")}` : "No proposal"}`;
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
    loadFeasibility().catch(() => {});
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
    const leftScenario = scenarioCache.get(activeScenarioId);
    const rightScenario = scenarioCache.get(other);
    const planningRows = [];
    if (leftScenario && rightScenario) {
      const leftObject = leftScenario.scene_state.proposed[0];
      const rightObject = rightScenario.scene_state.proposed[0];
      planningRows.push(
        ["Capacity", `${Number(leftObject.attributes.capacity_mw)} MW`, `${Number(rightObject.attributes.capacity_mw)} MW`],
        ["Land envelope", `${Math.round(Number(leftObject.geometry_local.width_m) * Number(leftObject.geometry_local.length_m)).toLocaleString()} m²`, `${Math.round(Number(rightObject.geometry_local.width_m) * Number(rightObject.geometry_local.length_m)).toLocaleString()} m²`],
        ["Evaluation", leftScenario.evaluation.overall_status, rightScenario.evaluation.overall_status],
      );
    }
    const changedConstraints = Object.entries(comparison.constraint_changes || {}).map(([id, values]) => [
      constraintName(id), values.before ? values.before.outcome : "—", values.after ? values.after.outcome : "—",
    ]);
    const rows = [...planningRows, ...(changedConstraints.length ? changedConstraints : [["Constraint outcomes", "No change", "No change"]])];
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
    const area = Number.isFinite(areaM2) ? `${(areaM2 / 4046.8564224).toFixed(1)} acres` : "Not provided";
    const facts = [
      ["Parcel area", area],
      ["Identity", identity.parcel_match_type === "exact_intersect" ? "Exact parcel match" : "Unresolved"],
    ];
    renderFactsList(document.getElementById("snapshotFacts"), facts);
    document.getElementById("parcelTitle").textContent = identity.parcel_address || identity.parcel_id;
    document.getElementById("siteName").textContent = identity.parcel_address || "Verified property";
    document.getElementById("parcelMeta").textContent = `${identity.parcel_data_source || "MIREYE"} · exact parcel match`;
    document.getElementById("snapshotState").textContent = snapshot.is_expired ? "Site intelligence needs an update" : "MIREYE intelligence current";
    renderSources(snapshot);
  }

  function usableEvidence(field) {
    const record = snapshotData && snapshotData.evidence && snapshotData.evidence[field];
    return record && record.status === "ok" && record.value != null && Number(record.expires_at) > Date.now() / 1000 ? record.value : null;
  }

  function distanceContext(field, label) {
    const raw = usableEvidence(field);
    if (raw === null) return "";
    const value = Number(raw);
    return Number.isFinite(value) ? `${label}: ${(value / 1000).toFixed(1)} km. ` : "";
  }

  function renderFeasibility(evaluation) {
    const results = Object.fromEntries(evaluation.constraint_results.map((item) => [item.constraint_id, item]));
    const zoning = usableEvidence("parcel_zoning");
    const pointFlood = usableEvidence("within_floodplain_polygon");
    const water = usableEvidence("within_water_service_area");
    const cards = [
      ["Land", results.footprint_inside_parcel],
      ["Flood", {
        ...results.parcel_outside_fema_sfha,
        explanation: `${pointFlood === null ? "Resolution-point evidence is unavailable. " : `Resolution point is ${pointFlood ? "inside" : "outside"} the mapped FEMA floodplain. `}${results.parcel_outside_fema_sfha.explanation}`,
      }],
      ["Transmission", {
        ...results.transmission_available_capacity_mw,
        explanation: `${distanceContext("nearest_transmission_line_distance_m", "Nearest mapped line")}${results.transmission_available_capacity_mw.explanation}`,
      }],
      ["Road access", {
        ...results.legal_access,
        explanation: `${distanceContext("nearest_major_road_distance_m", "Nearest mapped major road")}${results.legal_access.explanation}`,
      }],
      ["Zoning", {
        ...results.industrial_zoning,
        explanation: `${zoning ? `Raw parcel code: ${zoning}. ` : "Raw parcel code is unavailable. "}${results.industrial_zoning.explanation}`,
      }],
      ["Grid", results.sufficient_grid_capacity],
      ["Water", {
        ...results.utilities_available,
        explanation: `${water === null ? "Mapped water service-area evidence is unavailable. " : `Mapped service-area flag: ${water ? "yes" : "no"}. `}${results.utilities_available.explanation}`,
      }],
      ["Expansion", {
        outcome: "UNRESOLVED",
        explanation: "The 300 MW reserve is conceptual geometry; future power, water, grading, and construction feasibility are not proven.",
      }],
    ];
    const container = document.getElementById("feasibilityCards");
    container.replaceChildren(...cards.map(([label, item]) => {
      const card = document.createElement("article");
      card.className = "feasibility-card";
      const heading = document.createElement("div");
      const title = document.createElement("strong");
      title.textContent = label;
      heading.append(title, stateBadge(item.outcome));
      const reason = document.createElement("p");
      reason.textContent = item.explanation;
      card.append(heading, reason);
      return card;
    }));
  }

  async function loadFeasibility() {
    if (!sceneState || !snapshotData || !sceneState.proposed.length) return;
    const requestedConstraints = [
      { constraint_id: "footprint_inside_parcel" },
      { constraint_id: "parcel_outside_fema_sfha" },
      { constraint_id: "transmission_available_capacity_mw" },
      { constraint_id: "legal_access" },
      { constraint_id: "industrial_zoning" },
      { constraint_id: "sufficient_grid_capacity" },
      { constraint_id: "utilities_available" },
    ];
    const response = await fetch(`/v1/sandbox/site/${encodeURIComponent(snapshotId())}/evaluate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scene_state: sceneState, requested_constraints: requestedConstraints }),
    });
    const evaluation = await response.json();
    if (!response.ok) throw new Error(evaluation.detail || "Site feasibility could not be evaluated.");
    renderFeasibility(evaluation);
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
      if (record.source_url) {
        const link = document.createElement("a");
        link.href = record.source_url;
        link.target = "_blank";
        link.rel = "noreferrer";
        link.textContent = " Source";
        source.append(link);
      }
      if (record.description) name.title = record.description;
      const fresh = Number(record.expires_at) > now && record.value != null && ["ok", null, undefined].includes(record.status);
      freshness.className = fresh ? "fresh" : "stale";
      freshness.textContent = fresh ? `Current · captured ${relativeTime(record.observed_at)}` : "Stale or unresolved";
      row.append(name, source, freshness);
      return row;
    });
    const worldRows = (worldData ? worldData.source_manifest : []).map((entry) => {
      const row = document.createElement("div");
      row.className = "source-row";
      const name = document.createElement("strong");
      const source = document.createElement("span");
      const freshness = document.createElement("span");
      name.textContent = `Observed ${String(entry.layer).replaceAll("_", " ")}`;
      source.textContent = `${entry.source.provider} · ${entry.source.release || entry.source.dataset || entry.source.source_id}`;
      freshness.className = "fresh";
      freshness.textContent = "Pinned WorldSnapshot source";
      row.append(name, source, freshness);
      return row;
    });
    document.getElementById("sourcesList").replaceChildren(...worldRows, ...rows);
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
    const refreshNeeded = [...(payload.stale_fields || []), ...(payload.missing_fields || []), ...(payload.incompatible_fields || []), ...(payload.deprecated_fields || [])];
    const planned = intelligencePlan?.fields || Object.keys(snapshotData.evidence || {});
    const verified = planned.filter((field) => {
      const record = snapshotData.evidence?.[field];
      return record && record.status === "ok" && record.value !== null && record.value !== undefined && Number(record.expires_at) > Date.now() / 1000;
    });
    const unresolved = planned.filter((field) => !verified.includes(field));
    const state = document.getElementById("mireyeFreshnessState");
    const cached = Object.values(snapshotData.evidence || {}).filter((record) => record.carried_from_snapshot_id).length;
    renderFactsList(document.getElementById("mireyeFreshnessFacts"), [["Verified", String(verified.length)], ["Unresolved", String(unresolved.length)], ["Cached", String(cached)]]);
    state.className = `status-dot ${payload.refresh_required ? "unresolved" : "pass"}`;
    document.getElementById("intelligenceHeadline").textContent = payload.refresh_required ? "Update recommended" : `Refreshed ${relativeTime(snapshotData.observed_at)}`;
    document.getElementById("mireyeRefreshQuote").disabled = !payload.refresh_required;
    document.getElementById("mireyeRefreshNote").textContent = payload.refresh_required
      ? `${refreshNeeded.length} field${refreshNeeded.length === 1 ? " needs" : "s need"} enrichment or refresh.`
      : `${verified.length} verified · ${unresolved.length} unresolved · all ${fresh.length} results are within their freshness windows.`;
  }

  async function loadMireyeFreshness() {
    const response = await fetch(`/v1/sandbox/site/${encodeURIComponent(snapshotId())}/intelligence-plan`);
    intelligencePlan = await response.json();
    if (!response.ok) throw new Error(intelligencePlan.detail || "Evidence planning failed.");
    renderMireyeFreshness(intelligencePlan.freshness);
    if (intelligencePlan.freshness.refresh_required) {
      document.getElementById("mireyeRefreshNote").textContent = `${intelligencePlan.freshness.refresh_fields.length} of ${intelligencePlan.field_count} project-relevant fields need enrichment or refresh.`;
    }
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
    const buildings = worldLayer("buildings");
    const water = worldLayer("water");
    const landCover = worldLayer("land_cover");
    const warnings = worldData.layers.flatMap((layer) => layer.warnings || []);
    const conflicts = worldData.quality_conflicts || [];
    const count = (layer, key) => layer && layer.availability === "AVAILABLE" ? String(layer[key].feature_count) : "Unavailable";
    renderFactsList(document.getElementById("worldFacts"), [
      ["Terrain", terrain && terrain.availability === "AVAILABLE" ? `${terrain.terrain.actual_resolution_m} m / ${terrain.terrain.vertical_reference}` : "Unavailable"],
      ["Road features", count(roads, "roads")],
      ["Building footprints", count(buildings, "buildings")],
      ["Water features", count(water, "water")],
      ["Land-cover features", count(landCover, "land_cover")],
      ["Conflicts", String(conflicts.length)],
    ]);
    const sources = document.getElementById("worldSources");
    sources.replaceChildren(...worldData.source_manifest.map((entry) => {
      const row = document.createElement("div");
      const source = entry.source;
      row.textContent = `${entry.layer}: ${source.provider}, ${source.release || source.source_id || source.dataset}`;
      return row;
    }));
    document.getElementById("worldWarning").textContent = conflicts.length ? `CONFLICT: ${conflicts.join(" ")}` : warnings.join(" ");
    document.getElementById("worldState").textContent = conflicts.length ? "Source conflict" : "Observed layers";
    document.getElementById("worldPanel").hidden = false;
    document.getElementById("worldDetails").hidden = false;
    document.getElementById("groundStateChip").textContent = terrain && terrain.availability === "AVAILABLE"
      ? `Observed physical context · USGS terrain ${terrain.terrain.actual_resolution_m} m`
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

  async function ensureWorld() {
    const requiredLayers = ["terrain", "roads"];
    if (sceneState.world_snapshot_id) {
      const existing = await loadWorld(sceneState.world_snapshot_id);
      const available = new Set(existing.layers.filter((layer) => layer.availability === "AVAILABLE").map((layer) => layer.layer));
      if (requiredLayers.every((layer) => available.has(layer))) return existing;
    }
    document.getElementById("groundStateChip").textContent = "Loading real physical context...";
    const response = await fetch("/v1/sandbox/world-snapshots", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ site_snapshot_id: snapshotId(), requested_layers: requiredLayers }),
    });
    const world = await response.json();
    if (!response.ok) throw new Error(world.detail || "The observed terrain and road layers could not be prepared.");
    sceneState.world_snapshot_id = world.world_snapshot_id;
    const url = new URL(window.location.href);
    url.searchParams.set("world", world.world_snapshot_id);
    window.history.replaceState({}, "", url);
    return world;
  }

  function addWorldLayers() {
    if (!worldData) return;
    const terrain = worldLayer("terrain");
    const roads = worldLayer("roads");
    const buildings = worldLayer("buildings");
    const water = worldLayer("water");
    const landCover = worldLayer("land_cover");
    if (terrain && terrain.availability === "AVAILABLE") {
      map.addSource("world-terrain", {
        type: "raster-dem", tiles: terrain.render.tiles, tileSize: terrain.render.tile_size,
        minzoom: terrain.render.minzoom, maxzoom: terrain.render.maxzoom, encoding: terrain.render.encoding,
        bounds: worldData.query_aoi.bbox,
      });
      map.setTerrain({ source: "world-terrain", exaggeration: 1.25 });
      map.addLayer({ id: "world-terrain-hillshade", type: "hillshade", source: "world-terrain", paint: { "hillshade-shadow-color": "#4f5b55", "hillshade-highlight-color": "#f7f5ec", "hillshade-accent-color": "#87938c", "hillshade-exaggeration": 0.55 } });
      document.getElementById("terrainToggle").disabled = false;
    }
    if (landCover && landCover.availability === "AVAILABLE") {
      map.addSource("world-land-cover", { type: "geojson", data: landCover.render.url });
      map.addLayer({ id: "world-land-cover-fill", type: "fill", source: "world-land-cover", paint: {
        "fill-color": ["match", ["get", "subtype"], "forest", "#53705b", "grass", "#879b73", "crop", "#a7a77c", "#81917d"],
        "fill-opacity": 0.24,
      } });
      document.getElementById("landCoverToggle").disabled = false;
    }
    if (water && water.availability === "AVAILABLE") {
      map.addSource("world-water", { type: "geojson", data: water.render.url });
      map.addLayer({ id: "world-water-fill", type: "fill", source: "world-water", paint: { "fill-color": "#6f9fac", "fill-opacity": 0.62 } });
      map.addLayer({ id: "world-water-line", type: "line", source: "world-water", paint: { "line-color": "#537f8c", "line-width": 1 } });
      document.getElementById("waterToggle").disabled = false;
    }
    if (buildings && buildings.availability === "AVAILABLE") {
      map.addSource("world-buildings", { type: "geojson", data: buildings.render.url });
      map.addLayer({ id: "world-buildings-footprints", type: "fill", source: "world-buildings", paint: { "fill-color": "#727876", "fill-opacity": 0.5, "fill-outline-color": "#555c59" } });
      map.addLayer({
        id: "world-buildings-extrusion", type: "fill-extrusion", source: "world-buildings",
        filter: [">", ["to-number", ["get", "height_m"], 0], 0],
        paint: { "fill-extrusion-color": "#7b817e", "fill-extrusion-height": ["to-number", ["get", "height_m"], 0], "fill-extrusion-opacity": 0.68 },
      });
      document.getElementById("buildingsToggle").disabled = false;
    }
    if (roads && roads.availability === "AVAILABLE") {
      map.addSource("world-roads", { type: "geojson", data: roads.render.url });
      map.addLayer({ id: "world-roads-casing", type: "line", source: "world-roads", paint: {
        "line-color": "#fbfaf4", "line-opacity": 0.9,
        "line-width": ["interpolate", ["linear"], ["zoom"], 11, ["match", ["get", "class"], "motorway", 3.6, "trunk", 3.4, "primary", 3, "secondary", 2.5, "tertiary", 2, 1.2], 16, ["match", ["get", "class"], "motorway", 10, "trunk", 9, "primary", 8, "secondary", 6.5, "tertiary", 5, 3]],
      } });
      map.addLayer({ id: "world-roads-line", type: "line", source: "world-roads", paint: {
        "line-color": ["match", ["get", "class"], "motorway", "#4d565c", "trunk", "#586168", "primary", "#687178", "secondary", "#7d8588", "tertiary", "#929795", "#a7aaa5"],
        "line-width": ["interpolate", ["linear"], ["zoom"], 11, ["match", ["get", "class"], "motorway", 2.2, "trunk", 2, "primary", 1.8, "secondary", 1.4, "tertiary", 1.1, 0.7], 16, ["match", ["get", "class"], "motorway", 6.5, "trunk", 6, "primary", 5, "secondary", 4, "tertiary", 3, 1.8]],
        "line-opacity": 0.92,
      } });
      document.getElementById("roadsToggle").disabled = false;
    }
  }

  function bindWorldControls() {
    document.getElementById("terrainToggle").addEventListener("change", (event) => {
      if (map && map.getSource("world-terrain")) {
        map.setTerrain(event.target.checked ? { source: "world-terrain", exaggeration: 1.25 } : null);
        if (map.getLayer("world-terrain-hillshade")) map.setLayoutProperty("world-terrain-hillshade", "visibility", event.target.checked ? "visible" : "none");
      }
    });
    document.getElementById("roadsToggle").addEventListener("change", (event) => {
      ["world-roads-casing", "world-roads-line"].forEach((layer) => {
        if (map && map.getLayer(layer)) map.setLayoutProperty(layer, "visibility", event.target.checked ? "visible" : "none");
      });
    });
    const bindLayers = (controlId, layerIds) => document.getElementById(controlId).addEventListener("change", (event) => {
      layerIds.forEach((layer) => {
        if (map && map.getLayer(layer)) map.setLayoutProperty(layer, "visibility", event.target.checked ? "visible" : "none");
      });
    });
    bindLayers("buildingsToggle", ["world-buildings-footprints", "world-buildings-extrusion"]);
    bindLayers("waterToggle", ["world-water-fill", "world-water-line"]);
    bindLayers("landCoverToggle", ["world-land-cover-fill"]);
  }

  function addSceneLayers() {
    const parcel = sceneState.observed.find((object) => object.id === "parcel_boundary");
    const point = sceneState.observed.find((object) => object.id === "resolution_point");
    const centroid = sceneState.derived.find((object) => object.id === "parcel_centroid");
    const ground = sceneState.derived.find((object) => object.id === "flat_ground_plane");

    addWorldLayers();
    const terrain = worldLayer("terrain");
    if (!terrain || terrain.availability !== "AVAILABLE") {
      map.addSource("sandbox-ground", { type: "geojson", data: { type: "Feature", geometry: ground.geometry } });
      map.addLayer({ id: "sandbox-ground-fill", type: "fill", source: "sandbox-ground", paint: { "fill-color": "#64748b", "fill-opacity": 0.12 } });
    }
    map.addSource("sandbox-parcel", { type: "geojson", data: { type: "Feature", geometry: parcel.geometry } });
    map.addLayer({ id: "sandbox-parcel-fill", type: "fill", source: "sandbox-parcel", paint: { "fill-color": "#0e7490", "fill-opacity": 0.18 } });
    map.addLayer({ id: "sandbox-parcel-line", type: "line", source: "sandbox-parcel", paint: { "line-color": "#0e7490", "line-width": 3 } });
    map.addSource("sandbox-point", { type: "geojson", data: { type: "Feature", geometry: point.geometry } });
    map.addLayer({ id: "sandbox-point-circle", type: "circle", source: "sandbox-point", paint: { "circle-radius": 7, "circle-color": "#0e7490", "circle-stroke-width": 2, "circle-stroke-color": "#ffffff" } });
    map.addSource("sandbox-centroid", { type: "geojson", data: { type: "Feature", geometry: centroid.geometry } });
    map.addLayer({ id: "sandbox-centroid-circle", type: "circle", source: "sandbox-centroid", paint: { "circle-radius": 4, "circle-color": "#315b86" } });
    map.addSource("sandbox-proposed", { type: "geojson", data: proposedFeatures() });
    map.addLayer({
      id: "sandbox-proposed-surfaces", type: "fill", source: "sandbox-proposed",
      filter: ["in", ["get", "render_class"], ["literal", ["surface", "access", "reserve"]]],
      paint: {
        "fill-color": ["match", ["get", "render_class"], "access", "#667178", "reserve", "#8ca17f", "#b8bab3"],
        "fill-opacity": ["match", ["get", "render_class"], "reserve", 0.32, 0.72],
      },
    });
    map.addLayer({
      id: "sandbox-proposed-extrusion",
      type: "fill-extrusion",
      source: "sandbox-proposed",
      filter: ["in", ["get", "render_class"], ["literal", ["building", "utility"]]],
      paint: {
        "fill-extrusion-color": ["match", ["get", "render_class"], "utility", "#657b86", "#aeb9ba"],
        "fill-extrusion-height": ["get", "height_m"],
        "fill-extrusion-base": 0,
        "fill-extrusion-opacity": 0.94,
      },
    });
    map.addLayer({
      id: "sandbox-proposed-outline", type: "line", source: "sandbox-proposed",
      filter: ["in", ["get", "render_class"], ["literal", ["campus_boundary", "reserve"]]],
      paint: {
        "line-color": ["match", ["get", "render_class"], "reserve", "#607558", "#9a5a38"],
        "line-width": ["match", ["get", "render_class"], "reserve", 1.5, 2.2],
        "line-dasharray": [2, 2],
      },
    });
    const bounds = new maplibregl.LngLatBounds();
    const extendCoordinates = (coordinates) => {
      if (typeof coordinates[0] === "number") bounds.extend(coordinates);
      else coordinates.forEach(extendCoordinates);
    };
    extendCoordinates(parcel.geometry.coordinates);
    const worldBounds = worldData && worldData.query_aoi && worldData.query_aoi.bbox;
    const southWest = worldBounds ? { lng: worldBounds[0], lat: worldBounds[1] } : bounds.getSouthWest();
    const northEast = worldBounds ? { lng: worldBounds[2], lat: worldBounds[3] } : bounds.getNorthEast();
    const lngPad = worldBounds ? 0 : Math.max((northEast.lng - southWest.lng) * 0.18, 0.0015);
    const latPad = worldBounds ? 0 : Math.max((northEast.lat - southWest.lat) * 0.18, 0.0015);
    const contextBounds = new maplibregl.LngLatBounds(
      [southWest.lng - lngPad, southWest.lat - latPad],
      [northEast.lng + lngPad, northEast.lat + latPad],
    );
    map.fitBounds(contextBounds, { padding: { top: 72, right: 72, bottom: 190, left: 72 }, maxZoom: 16.5, duration: 0 });
    map.easeTo({ pitch: 58, bearing: -20, duration: 0 });
  }

  function initializeMap() {
    if (!window.maplibregl) {
      showError("MapLibre GL JS failed to load.");
      return;
    }
    const worldBounds = worldData && worldData.query_aoi && worldData.query_aoi.bbox;
    const initialCenter = worldBounds
      ? [(worldBounds[0] + worldBounds[2]) / 2, (worldBounds[1] + worldBounds[3]) / 2]
      : [sceneState.camera.center.lng, sceneState.camera.center.lat];
    map = new maplibregl.Map({
      container: "sandboxMap",
      style: {
        version: 8,
        sources: {
          "osm-basemap": {
            type: "raster", tileSize: 256, maxzoom: 19,
            tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
            attribution: "&copy; OpenStreetMap contributors",
          },
        },
        layers: [
          { id: "map-background", type: "background", paint: { "background-color": "#d9dfda" } },
          { id: "osm-basemap", type: "raster", source: "osm-basemap", paint: { "raster-opacity": 0.82, "raster-saturation": -0.42, "raster-contrast": 0.08 } },
        ],
      },
      center: initialCenter,
      zoom: worldBounds ? 12.5 : sceneState.camera.zoom,
      pitch: worldBounds ? 58 : sceneState.camera.pitch,
      bearing: worldBounds ? -20 : sceneState.camera.bearing,
      maxPitch: 75,
      attributionControl: true,
    });
    window.MireyeSandboxMap = map;
    map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");
    map.addControl(new maplibregl.ScaleControl({ maxWidth: 120, unit: "metric" }), "bottom-right");
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
      worldData = await ensureWorld();
      renderWorld();
      window.MireyeSandboxScene = sceneState;
      renderFacts(snapshot);
      await loadFeasibility();
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
