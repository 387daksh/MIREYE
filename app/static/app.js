(() => {
  let activeRequestId = null;
  const form = document.getElementById("requestForm");
  const input = document.getElementById("requestInput");
  const candidateInput = document.getElementById("candidateInput");
  const runButton = document.getElementById("runButton");
  const work = document.getElementById("workSection");
  const progress = document.getElementById("progressList");
  const notice = document.getElementById("notice");
  const choices = document.getElementById("choiceList");
  const confirmation = document.getElementById("confirmationPanel");
  const results = document.getElementById("results");
  const propertyHandoff = document.getElementById("propertyHandoff");
  const propertyHandoffForm = document.getElementById("propertyHandoffForm");
  const propertyHandoffInput = document.getElementById("propertyHandoffInput");
  const projectPanel = document.getElementById("projectPanel");
  const projectAgent = document.getElementById("projectAgent");
  let activeMessage = "";
  let activeProjectId = null;
  let activeProjectSpendPlan = null;
  const projectAgentSessionId = `diligence-${crypto.randomUUID()}`;

  const statusLabel = {
    complete: "Complete", pending: "Waiting", confirmation_required: "Ready for confirmation",
    needs_input: "Needs your input", unavailable: "Unavailable", active: "In progress",
  };

  function resetOutput() {
    work.hidden = false;
    notice.hidden = true;
    choices.hidden = true;
    confirmation.hidden = true;
    results.hidden = true;
    propertyHandoff.hidden = true;
    projectPanel.hidden = true;
    projectAgent.hidden = true;
    document.getElementById("requestSummary").hidden = true;
    renderStages([
      { id: "understand", label: "Understanding request", status: "active" },
      { id: "discover", label: "Resolving candidate locations", status: "pending" },
      { id: "enrich", label: "Checking MIREYE site intelligence", status: "pending" },
      { id: "evaluate", label: "Evaluating constraints", status: "pending" },
    ]);
    work.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderStages(stages) {
    progress.replaceChildren(...stages.map((stage) => {
      const row = document.createElement("li");
      row.className = `progress-item ${stage.status}`;
      const marker = document.createElement("span");
      marker.className = "progress-marker";
      marker.textContent = stage.status === "complete" ? "✓" : stage.status === "unavailable" ? "–" : "";
      const copy = document.createElement("div");
      const label = document.createElement("strong");
      const state = document.createElement("span");
      label.textContent = stage.label;
      state.textContent = statusLabel[stage.status] || stage.status;
      copy.append(label, state);
      row.append(marker, copy);
      return row;
    }));
  }

  function renderUnderstanding(items) {
    const section = document.getElementById("requestSummary");
    const tags = document.getElementById("summaryTags");
    tags.replaceChildren(...items.map((item) => {
      const tag = document.createElement("span");
      tag.textContent = item;
      return tag;
    }));
    section.hidden = !items.length;
  }

  function showNotice(title, message, tone = "info") {
    notice.className = `product-notice ${tone}`;
    document.getElementById("noticeTitle").textContent = title;
    document.getElementById("noticeMessage").textContent = message;
    notice.hidden = false;
  }

  function renderChoices(items) {
    choices.replaceChildren(...items.map((item) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "choice-button";
      button.innerHTML = `<span>${escapeHtml(item.label)}</span><span aria-hidden="true">→</span>`;
      button.addEventListener("click", () => selectCandidate(item.index));
      return button;
    }));
    choices.hidden = false;
  }

  function renderConfirmation(data) {
    const credits = data.estimated_credits == null ? "MIREYE will confirm the cost before retrieval" : `Estimated cost: ${data.estimated_credits} credits`;
    document.getElementById("confirmationText").textContent = `${data.location_label}. ${data.field_count} relevant fields will be checked. ${credits}.`;
    confirmation.hidden = false;
  }

  function outcomeBadge(outcome) {
    const value = outcome || "UNRESOLVED";
    return `<span class="outcome ${value.toLowerCase()}">${escapeHtml(value)}</span>`;
  }

  function formatDistance(meters) {
    if (!Number.isFinite(Number(meters))) return "Not verified";
    return Number(meters) >= 1000 ? `${(Number(meters) / 1000).toFixed(1)} km` : `${Math.round(Number(meters))} m`;
  }

  function renderCandidates(items) {
    const list = document.getElementById("candidateList");
    list.replaceChildren(...items.map((candidate, index) => {
      const card = document.createElement("article");
      card.className = "candidate-card";
      const checks = candidate.checks.length ? candidate.checks.map((check) => `
        <div class="candidate-check">
          <span>${escapeHtml(check.label)}</span>${outcomeBadge(check.outcome)}
          <small>${escapeHtml(check.reason)}</small>
        </div>`).join("") : `<div class="candidate-check"><span>Site evidence</span>${outcomeBadge("UNRESOLVED")}<small>No project constraints were supplied.</small></div>`;
      card.innerHTML = `
        <div class="candidate-topline"><span>Site ${index + 1}</span>${outcomeBadge(candidate.overall_status)}</div>
        <div class="candidate-title"><div><h3>${escapeHtml(candidate.title)}</h3><p>${candidate.area_acres == null ? "Area not provided" : `${candidate.area_acres.toLocaleString()} acres`}</p></div><a class="primary-button" href="${encodeURI(candidate.sandbox_url)}">Open site <span aria-hidden="true">→</span></a></div>
        <div class="candidate-quick-facts"><div><span>Transmission</span><strong>${formatDistance(candidate.transmission_distance_m)}</strong></div><div><span>Road</span><strong>${formatDistance(candidate.road_distance_m)}</strong></div><div><span>Zoning code</span><strong>${escapeHtml(candidate.zoning_code || "Unresolved")}</strong></div></div>
        <div class="candidate-checks">${checks}</div>`;
      return card;
    }));
    results.hidden = false;
  }

  function renderResponse(payload) {
    activeProjectId = null;
    activeProjectSpendPlan = null;
    activeRequestId = payload.request_id;
    renderUnderstanding(payload.understanding || []);
    renderStages(payload.stages || []);
    notice.hidden = true;
    choices.hidden = true;
    confirmation.hidden = true;
    results.hidden = true;
    propertyHandoff.hidden = true;
    projectPanel.hidden = true;
    projectAgent.hidden = true;
    if (payload.status === "DISCOVERY_UNAVAILABLE") {
      showNotice("Start with a specific property", payload.message, "warning");
      propertyHandoff.hidden = false;
      propertyHandoffInput.focus();
    }
    else if (payload.status === "MIREYE_UNAVAILABLE") showNotice("MIREYE is not connected", payload.message, "warning");
    else if (payload.status === "NOT_FOUND") showNotice("Property not found", payload.message, "warning");
    else if (payload.status === "CLARIFICATION_REQUIRED") {
      showNotice("One detail before I continue", payload.message);
      if (payload.choices) renderChoices(payload.choices);
    } else if (payload.status === "CONFIRMATION_REQUIRED") renderConfirmation(payload.confirmation);
    else if (payload.status === "COMPLETE") {
      showNotice("Site intelligence ready", payload.message, "success");
      renderCandidates(payload.candidates || []);
    }
  }

  async function post(url, body) {
    const response = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof payload.detail === "string" ? payload.detail : "Mireye couldn't complete that request.");
    return payload;
  }

  async function submitRequest(event) {
    event.preventDefault();
    if (!input.value.trim()) return;
    activeMessage = input.value.trim();
    resetOutput();
    runButton.disabled = true;
    runButton.textContent = "Working…";
    try {
      const candidates = candidateInput.value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
      if (candidates.length) {
        const workspaceId = sessionStorage.getItem("mireye-workspace-id") || `workspace-${crypto.randomUUID()}`;
        sessionStorage.setItem("mireye-workspace-id", workspaceId);
        renderDiligenceProject(await post("/v1/diligence/projects", { workspace_id: workspaceId, message: input.value.trim(), candidates }));
      } else {
        renderResponse(await post("/v1/product/requests", { message: input.value.trim() }));
      }
    } catch (error) {
      showNotice("I couldn't complete that request", error.message, "warning");
    } finally {
      runButton.disabled = false;
      runButton.innerHTML = `Run <span aria-hidden="true">→</span>`;
    }
  }

  function renderDiligenceProject(project) {
    activeProjectId = project.project_id;
    activeProjectSpendPlan = project.spend_plan;
    renderUnderstanding(project.request.understanding || []);
    notice.hidden = true;
    choices.hidden = true;
    propertyHandoff.hidden = true;
    projectPanel.hidden = true;
    projectAgent.hidden = true;
    confirmation.hidden = true;
    results.hidden = true;
    const resolved = project.candidates.filter((item) => ["RESOLVED", "ENRICHED"].includes(item.reconciliation_status)).length;
    if (project.status === "CANDIDATES_SUPPLIED") {
      renderStages([
        { id: "understand", label: "Understanding request", status: "complete" },
        { id: "discover", label: "Resolving supplied candidates", status: "confirmation_required" },
        { id: "enrich", label: "Checking MIREYE site intelligence", status: "pending" },
        { id: "evaluate", label: "Evaluating constraints", status: "pending" },
      ]);
      document.getElementById("projectTitle").textContent = `${project.candidate_count} candidate${project.candidate_count === 1 ? "" : "s"} supplied`;
      document.getElementById("projectText").textContent = "MIREYE will resolve only these properties, then prepare an exact enrichment quote. No statewide search or synthetic parcel data will be used.";
      projectPanel.hidden = false;
    } else if (project.status === "AWAITING_ENRICHMENT_APPROVAL") {
      renderStages([
        { id: "understand", label: "Understanding request", status: "complete" },
        { id: "discover", label: "Resolving supplied candidates", status: "complete" },
        { id: "enrich", label: "Checking MIREYE site intelligence", status: "confirmation_required" },
        { id: "evaluate", label: "Evaluating constraints", status: "pending" },
      ]);
      const credits = project.spend_plan.expected_credits == null ? "MIREYE returned no numerical estimate" : `Estimated cost: ${project.spend_plan.expected_credits} credits`;
      document.getElementById("confirmationText").textContent = `${resolved} resolved candidate${resolved === 1 ? "" : "s"}. ${project.spend_plan.requested_fields.length} required fields will be checked. ${credits}.`;
      document.getElementById("confirmButton").textContent = "Approve MIREYE enrichment";
      confirmation.hidden = false;
    } else if (project.status === "EVALUATED") {
      renderStages([
        { id: "understand", label: "Understanding request", status: "complete" },
        { id: "discover", label: "Resolving supplied candidates", status: "complete" },
        { id: "enrich", label: "Checking MIREYE site intelligence", status: "complete" },
        { id: "evaluate", label: "Evaluating constraints", status: "complete" },
      ]);
      const byId = new Map(project.candidates.map((item) => [item.candidate_id, item]));
      renderCandidates(project.ranking.map((ranked) => {
        const candidate = byId.get(ranked.candidate_id);
        const summary = candidate.summary || {};
        return {
          title: summary.title || String(candidate.raw_input), area_acres: summary.area_acres,
          transmission_distance_m: summary.transmission_distance_m, road_distance_m: summary.road_distance_m,
          zoning_code: summary.zoning_code, overall_status: ranked.overall_status,
          sandbox_url: summary.sandbox_url || "#", checks: ranked.constraint_results.map((item) => ({ label: constraintName(item.constraint_id), outcome: item.outcome, reason: item.explanation })),
        };
      }));
      projectAgent.hidden = false;
      showNotice("MIREYE shortlist ready", `${project.ranking.length} supplied candidates were ranked from deterministic evidence outcomes.`, "success");
    } else {
      showNotice("Candidate resolution needs attention", "Some supplied candidates could not be resolved automatically. Review the candidate inputs or select an explicit match.", "warning");
    }
  }

  function constraintName(value) {
    return String(value).replace(/^max_resolution_point_/, "").replace(/^resolution_point_/, "").replace(/^parcel_/, "").replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  async function planDiligenceProject() {
    const button = document.getElementById("projectPlanButton");
    button.disabled = true;
    try {
      renderDiligenceProject(await post(`/v1/diligence/projects/${encodeURIComponent(activeProjectId)}/plan`, { confirmed_resolution: true }));
    } catch (error) {
      showNotice("MIREYE could not resolve the shortlist", error.message, "warning");
    } finally {
      button.disabled = false;
    }
  }

  async function sendProjectChat(event) {
    event.preventDefault();
    const field = document.getElementById("projectChatInput");
    const message = field.value.trim();
    if (!message || !activeProjectId) return;
    const response = document.getElementById("projectAgentResponse");
    response.textContent = "Reviewing the deterministic shortlist...";
    try {
      const result = await post(`/v1/diligence/projects/${encodeURIComponent(activeProjectId)}/chat`, { message, session_id: projectAgentSessionId });
      response.textContent = result.message;
      field.value = "";
    } catch (error) {
      response.textContent = error.message;
    }
  }

  async function watchProject() {
    if (!activeProjectId) return;
    const state = await post(`/v1/diligence/projects/${encodeURIComponent(activeProjectId)}/watch`, { enabled: true });
    document.getElementById("projectAgentResponse").textContent = state.enabled ? "This shortlist is saved for explicit freshness checks." : "Watch mode is off.";
  }

  async function checkProject() {
    if (!activeProjectId) return;
    const state = await post(`/v1/diligence/projects/${encodeURIComponent(activeProjectId)}/check-now`, {});
    const stale = state.candidate_states.filter((item) => item.status !== "CURRENT").length;
    document.getElementById("projectAgentResponse").textContent = stale ? `${stale} candidate${stale === 1 ? " needs" : "s need"} a MIREYE refresh quote.` : "All saved candidate evidence is currently fresh.";
  }

  function submitPropertyHandoff(event) {
    event.preventDefault();
    const property = propertyHandoffInput.value.trim();
    if (!property) {
      propertyHandoffInput.focus();
      return;
    }
    input.value = `${activeMessage || input.value.trim()} at ${property}`;
    form.requestSubmit();
  }

  async function selectCandidate(index) {
    choices.hidden = true;
    renderStages([
      { id: "understand", label: "Understanding request", status: "complete" },
      { id: "discover", label: "Resolving candidate locations", status: "active" },
      { id: "enrich", label: "Checking MIREYE site intelligence", status: "pending" },
      { id: "evaluate", label: "Evaluating constraints", status: "pending" },
    ]);
    try { renderResponse(await post(`/v1/product/requests/${encodeURIComponent(activeRequestId)}/select`, { candidate_index: index })); }
    catch (error) { showNotice("I couldn't use that property", error.message, "warning"); }
  }

  async function confirmRequest() {
    const button = document.getElementById("confirmButton");
    button.disabled = true;
    button.textContent = "Checking site…";
    renderStages([
      { id: "understand", label: "Understanding request", status: "complete" },
      { id: "discover", label: "Resolving candidate locations", status: "complete" },
      { id: "enrich", label: "Checking MIREYE site intelligence", status: "active" },
      { id: "evaluate", label: "Evaluating constraints", status: "pending" },
    ]);
    try {
      if (activeProjectId && activeProjectSpendPlan) {
        renderDiligenceProject(await post(`/v1/diligence/projects/${encodeURIComponent(activeProjectId)}/enrich`, { spend_plan_id: activeProjectSpendPlan.spend_plan_id, confirmed: true }));
      } else {
        renderResponse(await post(`/v1/product/requests/${encodeURIComponent(activeRequestId)}/confirm`, { confirmed: true }));
      }
    }
    catch (error) { showNotice("MIREYE couldn't analyze this property", `${error.message} No existing site data was changed.`, "warning"); }
    finally { button.disabled = false; button.textContent = "Continue with MIREYE"; }
  }

  function escapeHtml(value) {
    const div = document.createElement("div");
    div.textContent = String(value);
    return div.innerHTML;
  }

  form.addEventListener("submit", submitRequest);
  document.getElementById("projectPlanButton").addEventListener("click", planDiligenceProject);
  document.getElementById("projectChatForm").addEventListener("submit", sendProjectChat);
  document.getElementById("watchProject").addEventListener("click", () => watchProject().catch((error) => showNotice("Watch check failed", error.message, "warning")));
  document.getElementById("checkProject").addEventListener("click", () => checkProject().catch((error) => showNotice("Freshness check failed", error.message, "warning")));
  propertyHandoffForm.addEventListener("submit", submitPropertyHandoff);
  document.getElementById("confirmButton").addEventListener("click", confirmRequest);
  document.querySelectorAll(".prompt-examples button").forEach((button) => button.addEventListener("click", () => {
    input.value = button.textContent;
    input.focus();
  }));
})();
