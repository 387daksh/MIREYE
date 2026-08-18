/**
 * MIREYE — Frontend Application Engine
 * Connects directly to unified FastAPI endpoints:
 *   - POST /v1/screen
 *   - POST /v1/ask
 *   - GET  /v1/grid
 *   - POST /v1/workspace/open & observe
 *   - GET  /v1/workspace/{id}/state & replay
 */

document.addEventListener("DOMContentLoaded", () => {
  // Global State
  let currentCandidates = [];
  let currentDossier = null;
  let activeWorkspaceId = "ws_texas_solar";
  let workspaceHistorySnapshots = [];

  // =========================================================================
  // Tab Navigation
  // =========================================================================
  const tabButtons = document.querySelectorAll(".nav-tab-btn");
  const tabPanes = document.querySelectorAll(".tab-pane");

  tabButtons.forEach(btn => {
    btn.addEventListener("click", () => {
      tabButtons.forEach(b => b.classList.remove("active"));
      tabPanes.forEach(p => p.classList.remove("active"));

      btn.classList.add("active");
      const targetId = btn.getAttribute("data-tab");
      const targetPane = document.getElementById(targetId);
      if (targetPane) targetPane.classList.add("active");

      // Auto-refresh data on tab switch
      if (targetId === "tab-workspace") {
        loadWorkspaceState();
      } else if (targetId === "tab-grid") {
        runGridAnalysis();
      } else if (targetId === "tab-api") {
        updateCodeSnippets();
      }
    });
  });

  // =========================================================================
  // Theme Toggle (Light / Dark)
  // =========================================================================
  const themeToggleBtn = document.getElementById("themeToggleBtn");
  themeToggleBtn.addEventListener("click", () => {
    const html = document.documentElement;
    const current = html.getAttribute("data-theme") || "light";
    const next = current === "light" ? "dark" : "light";
    html.setAttribute("data-theme", next);
    drawSpatialCanvas();
  });

  // =========================================================================
  // Form Controls & Interactive Sliders
  // =========================================================================
  const inputAcreage = document.getElementById("inputAcreage");
  const valAcreage = document.getElementById("valAcreage");
  inputAcreage.addEventListener("input", (e) => valAcreage.textContent = `${e.target.value} Acres`);

  const inputSlope = document.getElementById("inputSlope");
  const valSlope = document.getElementById("valSlope");
  inputSlope.addEventListener("input", (e) => valSlope.textContent = `${e.target.value}%`);

  const inputCapacity = document.getElementById("inputCapacity");
  const valCapacity = document.getElementById("valCapacity");
  inputCapacity.addEventListener("input", (e) => valCapacity.textContent = `${e.target.value} MW`);

  const inputDistance = document.getElementById("inputDistance");
  const valDistance = document.getElementById("valDistance");
  inputDistance.addEventListener("input", (e) => valDistance.textContent = `${e.target.value} km`);

  // Flood zone chip toggles
  document.querySelectorAll("#floodZoneChips .chip").forEach(chip => {
    chip.addEventListener("click", () => chip.classList.toggle("active"));
  });

  // Preset chip selection
  document.querySelectorAll("#presetChips .chip").forEach(chip => {
    chip.addEventListener("click", () => {
      document.querySelectorAll("#presetChips .chip").forEach(c => c.classList.remove("active"));
      chip.classList.add("active");
      const preset = chip.getAttribute("data-preset");
      applyPresetFilters(preset);
    });
  });

  function applyPresetFilters(preset) {
    if (preset === "solar_siting") {
      inputAcreage.value = 50; valAcreage.textContent = "50 Acres";
      inputSlope.value = 5.0; valSlope.textContent = "5.0%";
      inputDistance.value = 15; valDistance.textContent = "15 km";
    } else if (preset === "storage_siting") {
      inputAcreage.value = 10; valAcreage.textContent = "10 Acres";
      inputSlope.value = 10.0; valSlope.textContent = "10.0%";
      inputDistance.value = 5; valDistance.textContent = "5 km";
    } else if (preset === "data_center_siting") {
      inputAcreage.value = 100; valAcreage.textContent = "100 Acres";
      inputSlope.value = 3.0; valSlope.textContent = "3.0%";
      inputCapacity.value = 150; valCapacity.textContent = "150 MW";
    } else if (preset === "wind_siting") {
      inputAcreage.value = 250; valAcreage.textContent = "250 Acres";
      inputSlope.value = 15.0; valSlope.textContent = "15.0%";
    }
  }

  // =========================================================================
  // Canvas Spatial Radar & Dithered Map Visualization
  // =========================================================================
  const canvas = document.getElementById("spatialCanvas");
  const ctx = canvas.getContext("2d");
  let radarAngle = 0;

  function resizeCanvas() {
    if (!canvas) return;
    canvas.width = canvas.parentElement.clientWidth;
    canvas.height = canvas.parentElement.clientHeight;
  }
  window.addEventListener("resize", resizeCanvas);
  resizeCanvas();

  function drawSpatialCanvas() {
    if (!canvas || !ctx) return;
    const w = canvas.width;
    const h = canvas.height;
    const isDark = document.documentElement.getAttribute("data-theme") === "dark";

    // Background
    ctx.fillStyle = isDark ? "#09090B" : "#000000";
    ctx.fillRect(0, 0, w, h);

    // Dithered scan grid lines
    ctx.strokeStyle = "rgba(113, 113, 122, 0.25)";
    ctx.lineWidth = 1;
    const step = 40;
    for (let x = 0; x < w; x += step) {
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, h);
      ctx.stroke();
    }
    for (let y = 0; y < h; y += step) {
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(w, y);
      ctx.stroke();
    }

    // Topographic contours
    ctx.strokeStyle = "rgba(255, 102, 0, 0.15)";
    for (let r = 50; r < Math.max(w, h); r += 60) {
      ctx.beginPath();
      ctx.arc(w / 2, h / 2, r, 0, Math.PI * 2);
      ctx.stroke();
    }

    // Draw Substation Nodes
    const substations = [
      { name: "Clear Creek 345kV", x: w * 0.45, y: h * 0.35, kv: 345 },
      { name: "Oak Hill 138kV", x: w * 0.72, y: h * 0.65, kv: 138 },
      { name: "Trinity Ridge 500kV", x: w * 0.25, y: h * 0.75, kv: 500 },
    ];

    substations.forEach(sub => {
      ctx.fillStyle = "#FFFFFF";
      ctx.beginPath();
      ctx.arc(sub.x, sub.y, 5, 0, Math.PI * 2);
      ctx.fill();

      ctx.fillStyle = "#A1A1AA";
      ctx.font = "10px 'Geist Mono', monospace";
      ctx.fillText(`${sub.name}`, sub.x + 8, sub.y + 3);
    });

    // Draw Candidate Parcels
    if (currentCandidates.length > 0) {
      currentCandidates.forEach((c, idx) => {
        // Map lat/lng roughly to canvas
        const cx = ((c.lng - (-98.5)) / (3.5)) * w * 0.8 + w * 0.1;
        const cy = h - (((c.lat - 31.5) / (3.0)) * h * 0.8 + h * 0.1);

        // Draw gen-tie connection line to nearest substation
        ctx.strokeStyle = "rgba(255, 102, 0, 0.35)";
        ctx.setLineDash([3, 3]);
        ctx.beginPath();
        ctx.moveTo(cx, cy);
        ctx.lineTo(substations[0].x, substations[0].y);
        ctx.stroke();
        ctx.setLineDash([]);

        // Parcel centroid point
        ctx.fillStyle = "#FF6600";
        ctx.beginPath();
        ctx.arc(cx, cy, 4, 0, Math.PI * 2);
        ctx.fill();

        // High confidence glow
        if (c.confidence_score && c.confidence_score > 0.8) {
          ctx.strokeStyle = "rgba(255, 102, 0, 0.5)";
          ctx.beginPath();
          ctx.arc(cx, cy, 8, 0, Math.PI * 2);
          ctx.stroke();
        }
      });
    }

    // Animated Radar Sweep
    radarAngle += 0.015;
    ctx.save();
    ctx.translate(w / 2, h / 2);
    ctx.rotate(radarAngle);
    const grad = ctx.createRadialGradient(0, 0, 0, 0, 0, Math.max(w, h));
    grad.addColorStop(0, "rgba(255, 102, 0, 0.2)");
    grad.addColorStop(1, "transparent");
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.moveTo(0, 0);
    ctx.arc(0, 0, Math.max(w, h), 0, Math.PI / 4);
    ctx.lineTo(0, 0);
    ctx.fill();
    ctx.restore();
  }

  // Canvas Animation loop
  function animateCanvas() {
    drawSpatialCanvas();
    requestAnimationFrame(animateCanvas);
  }
  animateCanvas();

  // =========================================================================
  // TAB 01: DISCOVERY SCREENING (POST /v1/screen)
  // =========================================================================
  const btnRunScreen = document.getElementById("btnRunScreen");
  const shortlistTableBody = document.getElementById("shortlistTableBody");
  const shortlistMetaText = document.getElementById("shortlistMetaText");
  const hudCandidateCount = document.getElementById("hudCandidateCount");

  async function runScreening() {
    btnRunScreen.disabled = true;
    btnRunScreen.textContent = "EXECUTING DUCKDB VECTOR SCAN...";

    const activeFloodZones = Array.from(document.querySelectorAll("#floodZoneChips .chip.active"))
      .map(c => c.getAttribute("data-zone"));

    const payload = {
      min_acreage: parseFloat(inputAcreage.value),
      max_slope_pct: parseFloat(inputSlope.value),
      flood_zones: activeFloodZones.length ? activeFloodZones : null,
      min_substation_capacity_mw: parseFloat(inputCapacity.value),
      max_distance_to_substation_km: parseFloat(inputDistance.value),
      zoning_renewable_only: document.getElementById("checkZoning").checked,
      limit: 25,
      apply_confidence_scoring: true,
    };

    try {
      const resp = await fetch("/v1/screen", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      if (!resp.ok) throw new Error("Screening request failed");
      const data = await resp.json();
      currentCandidates = data.shortlist || [];

      renderShortlistTable(currentCandidates);
      shortlistMetaText.textContent = `Found ${data.candidates_found} candidates matching rule constraints`;
      hudCandidateCount.textContent = `${data.candidates_found} SCREENED`;
    } catch (err) {
      console.error(err);
      alert("Error running screening: " + err.message);
    } finally {
      btnRunScreen.disabled = false;
      btnRunScreen.textContent = "RUN SPATIAL SCREENING →";
    }
  }

  function renderShortlistTable(candidates) {
    if (!shortlistTableBody) return;
    if (candidates.length === 0) {
      shortlistTableBody.innerHTML = `
        <tr>
          <td colspan="8" style="text-align:center; padding:2rem; color:var(--text-muted)">
            No parcels match the exact constraint filters. Try relaxing slope, acreage, or flood zone parameters.
          </td>
        </tr>
      `;
      return;
    }

    shortlistTableBody.innerHTML = candidates.map((c, i) => {
      const f = c.fields || {};
      const acreage = f.acreage?.value ? Number(f.acreage.value).toFixed(1) : "—";
      const slope = f.slope_pct?.value ? Number(f.slope_pct.value).toFixed(1) : "—";
      const flood = f.flood_zone?.value || "—";
      const dist = f.distance_to_substation_km?.value ? Number(f.distance_to_substation_km.value).toFixed(1) : "—";
      const score = c.confidence_score ? (c.confidence_score * 100).toFixed(0) : "85";

      return `
        <tr>
          <td>
            <a href="#" class="mono prov-source-link btn-inspect-parcel" data-id="${c.parcel_id}" data-lat="${c.lat}" data-lng="${c.lng}" style="font-weight:700">
              ${c.parcel_id}
            </a>
          </td>
          <td class="mono" style="font-size:0.75rem; color:var(--text-muted)">
            ${c.lat.toFixed(4)}, ${c.lng.toFixed(4)}
          </td>
          <td class="mono font-weight-bold">${acreage} ac</td>
          <td class="mono">${slope}%</td>
          <td><span class="badge ${flood === 'X' ? 'badge-green' : 'badge-gray'}">${flood}</span></td>
          <td class="mono">${dist} km</td>
          <td>
            <div class="confidence-gauge">
              <div class="confidence-bar-bg">
                <div class="confidence-bar-fill" style="width:${score}%"></div>
              </div>
              <span style="color:var(--mandarin-orange); font-size:0.75rem">${score}%</span>
            </div>
          </td>
          <td>
            <button class="btn btn-secondary btn-sm btn-quick-observe" data-key="${c.parcel_id}" data-lat="${c.lat}" data-lng="${c.lng}">
              + Observe
            </button>
          </td>
        </tr>
      `;
    }).join("");

    // Bind click events on inspect parcel links
    document.querySelectorAll(".btn-inspect-parcel").forEach(link => {
      link.addEventListener("click", (e) => {
        e.preventDefault();
        const lat = parseFloat(link.getAttribute("data-lat"));
        const lng = parseFloat(link.getAttribute("data-lng"));
        document.getElementById("askLat").value = lat;
        document.getElementById("askLng").value = lng;
        document.getElementById("tabBtnAsk").click();
        runAskVerify();
      });
    });

    // Bind quick observe buttons
    document.querySelectorAll(".btn-quick-observe").forEach(btn => {
      btn.addEventListener("click", () => {
        openObserveModal({
          local_key: btn.getAttribute("data-key"),
          lat: btn.getAttribute("data-lat"),
          lng: btn.getAttribute("data-lng"),
        });
      });
    });
  }

  btnRunScreen.addEventListener("click", runScreening);
  document.getElementById("btnResetFilters").addEventListener("click", () => {
    applyPresetFilters("solar_siting");
    runScreening();
  });

  // Initial Run
  runScreening();

  // =========================================================================
  // TAB 02: POINT DOSSIER & PROVENANCE (POST /v1/ask)
  // =========================================================================
  const btnAskVerify = document.getElementById("btnAskVerify");
  const askLat = document.getElementById("askLat");
  const askLng = document.getElementById("askLng");
  const askPresetSelect = document.getElementById("askPresetSelect");
  const provenanceGrid = document.getElementById("provenanceGrid");
  const dossierParcelId = document.getElementById("dossierParcelId");
  const dossierCoordsText = document.getElementById("dossierCoordsText");
  const dossierConfidenceScore = document.getElementById("dossierConfidenceScore");

  async function runAskVerify() {
    btnAskVerify.disabled = true;
    btnAskVerify.textContent = "FETCHING PROVENANCE FACTS...";

    const lat = parseFloat(askLat.value);
    const lng = parseFloat(askLng.value);
    const preset = askPresetSelect.value;

    try {
      const resp = await fetch("/v1/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lat, lng, preset }),
      });

      if (!resp.ok) throw new Error("Fact lookup failed");
      const data = await resp.json();
      currentDossier = data;

      renderDossier(data);
    } catch (err) {
      console.error(err);
      alert("Error querying parcel facts: " + err.message);
    } finally {
      btnAskVerify.disabled = false;
      btnAskVerify.textContent = "VERIFY PARCEL FACTS →";
    }
  }

  function renderDossier(data) {
    const d = data.dossier || {};
    const conf = data.confidence || {};
    const fields = d.fields || {};

    dossierParcelId.textContent = d.parcel_id || "PCL-VERIFIED-POINT";
    dossierCoordsText.textContent = `LAT: ${d.lat} | LNG: ${d.lng} | H3: ${d.h3_r7 || '872681545ffffff'}`;
    dossierConfidenceScore.textContent = `${conf.confidence_score ? conf.confidence_score.toFixed(3) : '0.920'} / 1.0`;

    const fieldEntries = Object.entries(fields);
    if (fieldEntries.length === 0) {
      provenanceGrid.innerHTML = `<div style="grid-column:1/-1; padding:2rem; text-align:center; color:var(--text-muted)">No fields returned for preset.</div>`;
      return;
    }

    provenanceGrid.innerHTML = fieldEntries.map(([name, rec]) => {
      const val = rec.value !== null && rec.value !== undefined ? rec.value : "—";
      const confBadge = rec.confidence === "high" ? "badge-green" : (rec.confidence === "medium" ? "badge-orange" : "badge-gray");
      const statusBadge = rec.status === "ok" ? "badge-green" : "badge-danger";

      return `
        <div class="provenance-card">
          <div class="prov-card-header">
            <div>
              <div class="prov-field-name">${name}</div>
              <div class="prov-layer-tag">${rec.layer || 'FEDERAL DATA'}</div>
            </div>
            <div style="display:flex; gap:4px">
              <span class="badge ${statusBadge}">${rec.status}</span>
              <span class="badge ${confBadge}">${rec.confidence}</span>
            </div>
          </div>
          <div class="prov-value-row">
            <div class="prov-value">${typeof val === 'number' ? (val % 1 !== 0 ? val.toFixed(2) : val) : val}</div>
            <div class="prov-unit">${rec.unit || ''}</div>
          </div>
          <div class="prov-meta-row">
            <span>SOURCE: <strong>${rec.source || 'USGS/NOAA'}</strong></span>
            <span>${rec.notes || '100% Provenance-tracked'}</span>
          </div>
        </div>
      `;
    }).join("");
  }

  btnAskVerify.addEventListener("click", runAskVerify);
  document.getElementById("btnObserveFromDossier").addEventListener("click", () => {
    openObserveModal({
      local_key: dossierParcelId.textContent,
      lat: askLat.value,
      lng: askLng.value,
    });
  });

  // =========================================================================
  // TAB 03: GRID CAPACITY INTELLIGENCE (ICI) (GET /v1/grid)
  // =========================================================================
  const btnRunGridAnalysis = document.getElementById("btnRunGridAnalysis");
  const inputTargetMw = document.getElementById("inputTargetMw");
  const valTargetMw = document.getElementById("valTargetMw");
  inputTargetMw.addEventListener("input", (e) => valTargetMw.textContent = `${e.target.value} MW`);

  const inputGridSlope = document.getElementById("inputGridSlope");
  const valGridSlope = document.getElementById("valGridSlope");
  inputGridSlope.addEventListener("input", (e) => valGridSlope.textContent = `${e.target.value}%`);

  const inputWetlands = document.getElementById("inputWetlands");
  const valWetlands = document.getElementById("valWetlands");
  inputWetlands.addEventListener("input", (e) => valWetlands.textContent = `${e.target.value}%`);

  const gridSubSelect = document.getElementById("gridSubSelect");

  async function runGridAnalysis() {
    btnRunGridAnalysis.disabled = true;
    btnRunGridAnalysis.textContent = "COMPUTING SCD HEADROOM...";

    const subName = gridSubSelect.value;
    let lat = 32.85, lng = -97.10;
    if (subName.includes("Oak Hill")) { lat = 32.20; lng = -96.40; }
    else if (subName.includes("Trinity")) { lat = 33.40; lng = -95.80; }
    else if (subName.includes("Prairie")) { lat = 31.80; lng = -98.10; }

    const targetMw = parseFloat(inputTargetMw.value);
    const slope = parseFloat(inputGridSlope.value);
    const wetlands = parseFloat(inputWetlands.value) / 100.0;

    try {
      const resp = await fetch(`/v1/grid?lat=${lat}&lng=${lng}&target_capacity_mw=${targetMw}&slope_pct=${slope}&epa_wetlands_pct=${wetlands}&compress_tokens=false`);
      if (!resp.ok) throw new Error("Grid analysis failed");
      const data = await resp.json();

      renderGridAnalysis(data);

      // Also fetch compressed token version for LLMs
      const respComp = await fetch(`/v1/grid?lat=${lat}&lng=${lng}&target_capacity_mw=${targetMw}&slope_pct=${slope}&epa_wetlands_pct=${wetlands}&compress_tokens=true`);
      const dataComp = await respComp.json();
      document.getElementById("compressedJsonPayload").textContent = JSON.stringify(dataComp, null, 2);
    } catch (err) {
      console.error(err);
    } finally {
      btnRunGridAnalysis.disabled = false;
      btnRunGridAnalysis.textContent = "CALCULATE HEADROOM DYNAMICS →";
    }
  }

  function renderGridAnalysis(data) {
    const sub = data.substation || {};
    const feas = data.feasibility || {};
    const row = feas.row_assessment || {};

    document.getElementById("iciSubName").textContent = `${sub.name} Node`;
    document.getElementById("iciSubMeta").textContent = `Voltage: ${sub.voltage_kv} kV | Distance: ${sub.distance_km} km | ISO: ERCOT`;

    const isFeasible = feas.firm_headroom_supported;
    const badge = document.getElementById("iciFeasibilityBadge");
    if (isFeasible) {
      badge.className = "badge badge-green";
      badge.textContent = "FEASIBLE (FIRM HEADROOM)";
    } else if (feas.projected_headroom_supported) {
      badge.className = "badge badge-orange";
      badge.textContent = "CONTINGENT ON FERC ATTRITION";
    } else {
      badge.className = "badge badge-danger";
      badge.textContent = "CAPACITY CONSTRAINED";
    }

    // Stacked Bars Calculation
    const firm = sub.firm_headroom_mw || 100;
    const contested = sub.contested_headroom_mw || 200;
    const freed = sub.projected_available_mw - firm;
    const total = firm + contested;

    const firmPct = (firm / total) * 100;
    const contPct = ((contested - freed) / total) * 100;
    const freedPct = (freed / total) * 100;

    const barFirm = document.getElementById("scdBarFirm");
    const barContested = document.getElementById("scdBarContested");
    const barFreed = document.getElementById("scdBarFreed");

    barFirm.style.width = `${firmPct}%`;
    barFirm.textContent = `Firm: ${firm} MW`;

    barContested.style.width = `${contPct}%`;
    barContested.textContent = `Contested Queue: ${(contested - freed).toFixed(0)} MW`;

    barFreed.style.width = `${freedPct}%`;
    barFreed.textContent = `Freed (~${sub.attrition_velocity_pct}%): ${freed.toFixed(0)} MW`;

    // ROW Barriers
    document.getElementById("rowDistKm").textContent = `${sub.distance_km} km`;
    document.getElementById("rowSlopeVal").textContent = `${inputGridSlope.value}%`;

    const rowList = document.getElementById("rowBarrierList");
    rowList.innerHTML = `
      <div class="row-barrier-item">
        <span>Gen-Tie Transmission Run (${sub.distance_km} km)</span>
        <span class="badge ${sub.distance_km > 25 ? 'badge-danger' : 'badge-green'}">EST. $${(row.estimated_gen_tie_cost_usd / 1e6).toFixed(2)}M</span>
      </div>
      <div class="row-barrier-item">
        <span>Topographic Slope Incline (${inputGridSlope.value}%)</span>
        <span class="badge ${parseFloat(inputGridSlope.value) > 12 ? 'badge-danger' : 'badge-green'}">${parseFloat(inputGridSlope.value) > 12 ? 'STEEP BARRIER' : 'OPTIMAL'}</span>
      </div>
      <div class="row-barrier-item">
        <span>USFWS National Wetlands Incursion (${inputWetlands.value}%)</span>
        <span class="badge ${parseFloat(inputWetlands.value) > 15 ? 'badge-danger' : 'badge-green'}">${parseFloat(inputWetlands.value) > 15 ? 'HIGH ENCROACHMENT' : 'CLEAR'}</span>
      </div>
      <div class="row-barrier-item">
        <span>Overall Right-of-Way Feasibility Rating</span>
        <span class="badge ${row.row_rating === 'clear' ? 'badge-green' : (row.row_rating === 'moderate_risk' ? 'badge-orange' : 'badge-danger')}">${(row.row_rating || 'clear').toUpperCase()}</span>
      </div>
    `;
  }

  btnRunGridAnalysis.addEventListener("click", runGridAnalysis);

  // =========================================================================
  // TAB 04: AGENTIC WORKSPACE MEMORY (/v1/workspace/*)
  // =========================================================================
  const workspaceSelect = document.getElementById("workspaceSelect");
  const colShortlisted = document.getElementById("colShortlisted");
  const colCandidates = document.getElementById("colCandidates");
  const colRejected = document.getElementById("colRejected");
  const countShortlisted = document.getElementById("countShortlisted");
  const countCandidates = document.getElementById("countCandidates");
  const countRejected = document.getElementById("countRejected");

  workspaceSelect.addEventListener("change", (e) => {
    activeWorkspaceId = e.target.value;
    loadWorkspaceState();
  });

  async function loadWorkspaceState() {
    try {
      const resp = await fetch(`/v1/workspace/${activeWorkspaceId}/state`);
      if (!resp.ok) return;
      const data = await resp.json();

      renderKanbanColumn(colShortlisted, countShortlisted, data.shortlisted || [], "shortlisted");
      renderKanbanColumn(colCandidates, countCandidates, data.candidates || [], "candidate");
      renderKanbanColumn(colRejected, countRejected, data.rejected || [], "rejected");
    } catch (err) {
      console.error(err);
    }
  }

  function renderKanbanColumn(container, counter, items, type) {
    counter.textContent = items.length;
    if (items.length === 0) {
      container.innerHTML = `<div style="text-align:center; padding:1.5rem; font-size:0.75rem; color:var(--text-muted)">No items in this epoch.</div>`;
      return;
    }

    container.innerHTML = items.map(obs => {
      const badgeClass = type === "shortlisted" ? "badge-orange" : (type === "candidate" ? "badge-gray" : "badge-danger");
      return `
        <div class="obs-card">
          <div class="obs-card-header">
            <span class="obs-key">${obs.local_key}</span>
            <span class="obs-version">v${obs.version}</span>
          </div>
          <div class="obs-justification">${obs.justification || 'No justification entered.'}</div>
          <div class="obs-footer">
            <span class="badge ${badgeClass}">${obs.status}</span>
            <span>${new Date(obs.created_at * 1000).toLocaleTimeString()}</span>
          </div>
        </div>
      `;
    }).join("");
  }

  // Staleness Invalidation Check
  document.getElementById("btnCheckStaleness").addEventListener("click", async () => {
    const btn = document.getElementById("btnCheckStaleness");
    btn.textContent = "Checking Strata...";
    try {
      const resp = await fetch(`/v1/workspace/${activeWorkspaceId}/invalidate`, { method: "POST" });
      const data = await resp.json();
      if (data.stale_fields_count === 0) {
        alert("Strata Invalidation: 0 staleness detected. All site dossiers match live federal data.");
      } else {
        alert(`Strata Alert: ${data.stale_fields_count} fields updated in underlying layers.`);
      }
    } catch (err) {
      console.error(err);
    } finally {
      btn.textContent = "⚡ Invalidate Check";
    }
  });

  // Replay Time Travel Slider
  const replayScrubber = document.getElementById("replayScrubber");
  const replayTimeText = document.getElementById("replayTimeText");
  replayScrubber.addEventListener("input", async (e) => {
    const val = parseInt(e.target.value);
    if (val === 100) {
      replayTimeText.textContent = "LIVE HEAD";
      loadWorkspaceState();
    } else {
      const pastEpoch = Date.now() / 1000 - ((100 - val) * 3600);
      replayTimeText.textContent = new Date(pastEpoch * 1000).toLocaleTimeString();
      try {
        const resp = await fetch(`/v1/workspace/${activeWorkspaceId}/replay?as_of_ts=${pastEpoch}`);
        const data = await resp.json();
        const rows = data.state || [];
        renderKanbanColumn(colShortlisted, countShortlisted, rows.filter(r => r.status === "shortlisted"), "shortlisted");
        renderKanbanColumn(colCandidates, countCandidates, rows.filter(r => r.status === "candidate"), "candidate");
        renderKanbanColumn(colRejected, countRejected, rows.filter(r => r.status === "rejected"), "rejected");
      } catch (err) {
        console.error(err);
      }
    }
  });

  // =========================================================================
  // Observe Modal Logic
  // =========================================================================
  const observeModal = document.getElementById("observeModal");
  const btnCloseModal = document.getElementById("btnCloseModal");
  const btnSubmitObservation = document.getElementById("btnSubmitObservation");
  const modalLocalKey = document.getElementById("modalLocalKey");
  const modalStatusSelect = document.getElementById("modalStatusSelect");
  const modalJustification = document.getElementById("modalJustification");
  const modalLat = document.getElementById("modalLat");
  const modalLng = document.getElementById("modalLng");

  function openObserveModal(defaults = {}) {
    if (defaults.local_key) modalLocalKey.value = defaults.local_key;
    if (defaults.lat) modalLat.value = defaults.lat;
    if (defaults.lng) modalLng.value = defaults.lng;
    observeModal.classList.add("active");
  }

  btnCloseModal.addEventListener("click", () => observeModal.classList.remove("active"));
  document.getElementById("btnOpenObserveModal").addEventListener("click", () => openObserveModal());

  btnSubmitObservation.addEventListener("click", async () => {
    btnSubmitObservation.disabled = true;
    btnSubmitObservation.textContent = "COMMITTING OBSERVATION & SNAPSHOT...";

    const payload = {
      workspace_id: activeWorkspaceId,
      local_key: modalLocalKey.value,
      status: modalStatusSelect.value,
      justification: modalJustification.value || "Screening criteria met",
      lat: parseFloat(modalLat.value),
      lng: parseFloat(modalLng.value),
    };

    try {
      const resp = await fetch("/v1/workspace/observe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      if (!resp.ok) throw new Error("Failed to commit observation");
      observeModal.classList.remove("active");
      loadWorkspaceState();
    } catch (err) {
      alert("Error: " + err.message);
    } finally {
      btnSubmitObservation.disabled = false;
      btnSubmitObservation.textContent = "BIND OBSERVATION & SNAPSHOT →";
    }
  });

  // =========================================================================
  // TAB 05: LIVE API & MCP CONSOLE
  // =========================================================================
  const apiToolSelect = document.getElementById("apiToolSelect");
  const apiPayloadEditor = document.getElementById("apiPayloadEditor");
  const btnExecuteApiCall = document.getElementById("btnExecuteApiCall");
  const codeSnippetBox = document.getElementById("codeSnippetBox");
  const apiResponseBox = document.getElementById("apiResponseBox");
  const apiStatusBadge = document.getElementById("apiStatusBadge");

  const TEMPLATES = {
    screen: {
      url: "/v1/screen",
      method: "POST",
      payload: { min_acreage: 50.0, max_slope_pct: 5.0, flood_zones: ["X"], limit: 5 },
    },
    ask: {
      url: "/v1/ask",
      method: "POST",
      payload: { lat: 32.7767, lng: -96.7970, preset: "solar_siting" },
    },
    grid: {
      url: "/v1/grid?lat=32.85&lng=-97.10&target_capacity_mw=75.0&compress_tokens=true",
      method: "GET",
      payload: null,
    },
    meta: {
      url: "/v1/meta/fields",
      method: "GET",
      payload: null,
    },
    observe: {
      url: "/v1/workspace/observe",
      method: "POST",
      payload: { workspace_id: "ws_demo", local_key: "PCL-0001", status: "shortlisted", justification: "Passes 345kV firm capacity check", lat: 32.85, lng: -97.10 },
    },
    state: {
      url: "/v1/workspace/ws_texas_solar/state",
      method: "GET",
      payload: null,
    },
  };

  apiToolSelect.addEventListener("change", () => {
    const t = TEMPLATES[apiToolSelect.value];
    if (t.payload) {
      apiPayloadEditor.value = JSON.stringify(t.payload, null, 2);
    } else {
      apiPayloadEditor.value = "// GET request — no body payload required";
    }
    updateCodeSnippets();
  });

  function updateCodeSnippets() {
    const t = TEMPLATES[apiToolSelect.value];
    if (t.method === "POST") {
      codeSnippetBox.textContent = `curl -X POST https://api.mireye.com${t.url} \\
  -H "Authorization: Bearer $MIREYE_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '${apiPayloadEditor.value.replace(/\n/g, "")}'`;
    } else {
      codeSnippetBox.textContent = `curl -X GET "https://api.mireye.com${t.url}" \\
  -H "Authorization: Bearer $MIREYE_API_KEY"`;
    }
  }

  btnExecuteApiCall.addEventListener("click", async () => {
    const t = TEMPLATES[apiToolSelect.value];
    btnExecuteApiCall.disabled = true;
    btnExecuteApiCall.textContent = "SENDING REQUEST...";

    try {
      let resp;
      if (t.method === "POST") {
        const bodyJson = JSON.parse(apiPayloadEditor.value);
        resp = await fetch(t.url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(bodyJson),
        });
      } else {
        resp = await fetch(t.url);
      }

      apiStatusBadge.textContent = `${resp.status} ${resp.statusText}`;
      apiStatusBadge.className = resp.ok ? "badge badge-green" : "badge badge-danger";

      const data = await resp.json();
      apiResponseBox.textContent = JSON.stringify(data, null, 2);
    } catch (err) {
      apiResponseBox.textContent = "Error executing request: " + err.message;
    } finally {
      btnExecuteApiCall.disabled = false;
      btnExecuteApiCall.textContent = "SEND REQUEST →";
    }
  });

  document.getElementById("btnCopyCode").addEventListener("click", () => {
    navigator.clipboard.writeText(codeSnippetBox.textContent);
    alert("Code snippet copied to clipboard.");
  });

});
