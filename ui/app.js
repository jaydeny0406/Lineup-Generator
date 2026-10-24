
    const form = document.querySelector("#optimizer-form");
    const runButton = document.querySelector("#run-button");
    const saveButton = document.querySelector("#save-button");
    const loadButton = document.querySelector("#load-button");
    const loadFileInput = document.querySelector("#load-file");
    const seasonTypeInput = document.querySelector("#season-type");
    const indoorSprintSettings = document.querySelector("#indoor-sprint-settings");
    const indoorSprintDistanceInput = document.querySelector("#indoor-sprint-distance");
    const schoolUrlInput = document.querySelector("#school-url");
    const genderInput = document.querySelector("#gender");
    const opponentsInput = document.querySelector("#opponents");
    const injuredAthletesInput = document.querySelector("#injured-athletes");
    const teamEventLimitInput = document.querySelector("#team-event-limit");
    const athleteLimitList = document.querySelector("#athlete-limit-list");
    const addAthleteLimitButton = document.querySelector("#add-athlete-limit");
    const results = document.querySelector("#results");
    const errors = document.querySelector("#errors");
    const divisionTabs = document.querySelector("#division-tabs");
    const eventSortControls = document.querySelector("#event-sort");
    const athletePanel = document.querySelector("#athlete-panel");
    const athletePanelHead = document.querySelector("#athlete-panel-head");
    const athletePanelName = document.querySelector("#athlete-panel-name");
    const athletePanelCount = document.querySelector("#athlete-panel-count");
    const athletePanelEvents = document.querySelector("#athlete-panel-events");
    const athletePanelClose = document.querySelector("#athlete-panel-close");
    const teamPointsList = document.querySelector("#team-points");
    const scoreChange = document.querySelector("#score-change");
    const scoreChangeDelta = document.querySelector("#score-change-delta");
    const scoreChangeDetail = document.querySelector("#score-change-detail");
    const APP_BUILD_VERSION = "2026.09.13-team-limits-score-delta-v31";
    const PROJECT_SCHEMA_VERSION = 1;
    const OUTDOOR_EVENT_SORT_ORDERS = {
      schedule: [
        "4x800 relay", "4x100 relay", "3200m", "110h", "100m", "800m",
        "4x200 relay", "400m", "300h", "1600m", "200m", "4x400 relay",
        "shot put", "discus", "high jump", "pole vault", "long jump", "triple jump"
      ],
      distance: [
        "100m", "200m", "400m", "800m", "1600m", "3200m", "110h", "300h",
        "4x100 relay", "4x200 relay", "4x400 relay", "4x800 relay",
        "shot put", "discus", "high jump", "pole vault", "long jump", "triple jump"
      ]
    };
    const OUTDOOR_ALL_EVENTS = OUTDOOR_EVENT_SORT_ORDERS.schedule;
    const OUTDOOR_RELAY_EVENTS = ["4x100 relay", "4x200 relay", "4x400 relay", "4x800 relay"];
    const FIELD_EVENTS = new Set(["high jump", "pole vault", "discus", "shot put", "long jump", "triple jump"]);
    const DISTANCE_EVENTS = new Set(["4x800 relay", "800m", "1600m", "3200m"]);
    const LONG_DISTANCE_EVENTS = new Set(["1600m", "3200m"]);
    const DISTANCE_THREE_EVENT_ALLOWED_EVENTS = new Set(["4x800 relay", "800m", "400m", "4x400 relay"]);
    const OUTDOOR_RUNNING_ORDER = {
      "4x800 relay": 1,
      "4x100 relay": 2,
      "3200m": 3,
      "110h": 4,
      "100m": 5,
      "800m": 6,
      "4x200 relay": 7,
      "400m": 8,
      "300h": 9,
      "1600m": 10,
      "200m": 11,
      "4x400 relay": 12
    };
    let divisionResults = null;
    let activeDivision = "mens";
    let currentResult = null;
    let activeEventSort = "distance";
    let athleteIndex = new Map();
    let selectedAthleteKey = "";
    let selectedEventInfo = "";
    let panelDrag = null;
    let editState = null;
    let manualScoreChanges = {};

    seasonTypeInput.addEventListener("change", updateSeasonControls);
    updateSeasonControls();

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      await optimize("/api/optimize", {
        schoolUrl: schoolUrlInput.value.trim(),
        opponentUrls: opponentsInput.value.split(/\n+/).map(x => x.trim()).filter(Boolean),
        injuredAthletes: injuredAthletesInput.value.split(/\n+/).map(x => x.trim()).filter(Boolean),
        athleteEventLimits: collectAthleteEventLimits(),
        teamEventLimit: Number(teamEventLimitInput.value),
        gender: genderInput.value,
        seasonType: seasonTypeInput.value,
        indoorSprintDistance: indoorSprintDistanceInput.value
      });
    });

    addAthleteLimitButton.addEventListener("click", () => addAthleteLimitRow());
    athleteLimitList.addEventListener("click", (event) => {
      const removeButton = event.target.closest("[data-remove-athlete-limit]");
      if (removeButton) removeButton.closest(".athlete-limit-row")?.remove();
    });
    saveButton.addEventListener("click", saveLineupProject);
    loadButton.addEventListener("click", () => loadFileInput.click());
    loadFileInput.addEventListener("change", loadLineupProject);
    results.addEventListener("click", (event) => {
      const infoButton = event.target.closest(".event-info");
      if (infoButton) {
        event.stopPropagation();
        if (event.target.closest(".event-info-popover")) return;
        toggleEventInfo(infoButton.dataset.eventInfo || "");
        return;
      }
      const button = event.target.closest(".athlete-chip");
      if (!button) return;
      closeEventInfo();
      openAthletePanel(button.dataset.athleteName || button.textContent.trim());
    });
    athletePanelEvents.addEventListener("click", (event) => {
      const actionButton = event.target.closest("[data-edit-action]");
      if (actionButton) {
        startCoachEdit(actionButton.dataset.editAction, actionButton.dataset.athlete, actionButton.dataset.event);
        return;
      }
      const eventButton = event.target.closest("[data-target-event]");
      if (eventButton && editState) {
        editState.targetEvent = eventButton.dataset.targetEvent;
        renderCoachEditPanel();
        return;
      }
      const choiceButton = event.target.closest("[data-choice-role]");
      if (choiceButton && editState) {
        editState[choiceButton.dataset.choiceRole] = choiceButton.dataset.athlete;
        renderCoachEditPanel();
        return;
      }
      const commandButton = event.target.closest("[data-edit-command]");
      if (commandButton) handleEditCommand(commandButton.dataset.editCommand);
    });
    athletePanelClose.addEventListener("click", closeAthletePanel);
    athletePanelHead.addEventListener("pointerdown", startPanelDrag);
    document.addEventListener("pointermove", dragAthletePanel);
    document.addEventListener("pointerup", stopPanelDrag);
    document.addEventListener("click", (event) => {
      if (!event.target.closest(".event-info")) closeEventInfo();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeEventInfo();
    });
    window.addEventListener("resize", () => {
      if (!athletePanel.hidden && !panelDrag) placePanelOnSide();
    });
    divisionTabs.addEventListener("click", (event) => {
      const button = event.target.closest("[data-division]");
      if (!button || !divisionResults) return;
      activeDivision = button.dataset.division;
      updateDivisionTabs();
      renderSingle(divisionResults[activeDivision] || {});
    });
    eventSortControls.addEventListener("click", (event) => {
      const button = event.target.closest("[data-sort]");
      if (!button || button.dataset.sort === activeEventSort) return;
      activeEventSort = button.dataset.sort;
      updateEventSortControls();
      if (currentResult) renderSingle(currentResult);
    });

    async function optimize(url, payload) {
      runButton.disabled = true;
      manualScoreChanges = {};
      results.innerHTML = "";
      errors.innerHTML = "";
      closeAthletePanel();
      try {
        const response = await fetch(url, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload)
        });
        const data = await response.json();
        render(data);
      } catch (error) {
        errors.innerHTML = `<div class="error">${escapeHtml(error.message)}</div>`;
      } finally {
        runButton.disabled = false;
      }
    }

    function saveLineupProject() {
      if (!currentResult && !divisionResults) {
        errors.innerHTML = `<div class="error">Generate or load a lineup before saving.</div>`;
        return;
      }
      const project = {
        type: "track-lineup-project",
        schemaVersion: PROJECT_SCHEMA_VERSION,
        appVersion: APP_BUILD_VERSION,
        savedAt: new Date().toISOString(),
        form: currentFormState(),
        activeEventSort,
        activeDivision,
        manualScoreChanges: cloneResult(manualScoreChanges),
        result: divisionResults
          ? {mode: "both", division_results: cloneResult(divisionResults)}
          : cloneResult(currentResult)
      };
      downloadJson(project, projectFileName(project));
      errors.innerHTML = `<div class="notice">Lineup project saved. It includes the current lineup, relays, points, and parsed school/opponent data.</div>`;
    }

    async function loadLineupProject(event) {
      const file = event.target.files?.[0];
      if (!file) return;
      try {
        const project = JSON.parse(await file.text());
        restoreLineupProject(project);
      } catch (error) {
        errors.innerHTML = `<div class="error">Could not load saved lineup: ${escapeHtml(error.message)}</div>`;
      } finally {
        event.target.value = "";
      }
    }

    function restoreLineupProject(project) {
      if (!project || project.type !== "track-lineup-project") {
        throw new Error("This does not look like a Track Lineup Optimizer project file.");
      }
      if (Number(project.schemaVersion || 0) > PROJECT_SCHEMA_VERSION) {
        throw new Error("This project file was saved by a newer app version.");
      }
      if (!project.result || typeof project.result !== "object") {
        throw new Error("The saved project does not include lineup results.");
      }
      restoreFormState(project.form || {});
      manualScoreChanges = project.manualScoreChanges && typeof project.manualScoreChanges === "object"
        ? cloneResult(project.manualScoreChanges)
        : {};
      activeEventSort = ["distance", "schedule"].includes(project.activeEventSort) ? project.activeEventSort : "distance";
      updateEventSortControls();
      closeAthletePanel();
      if (project.result.mode === "both" && project.result.division_results) {
        divisionResults = project.result.division_results;
        activeDivision = project.activeDivision in divisionResults ? project.activeDivision : "mens";
        divisionTabs.hidden = false;
        updateDivisionTabs();
        renderSingle(divisionResults[activeDivision] || {});
      } else {
        divisionResults = null;
        divisionTabs.hidden = true;
        renderSingle(project.result);
      }
      errors.innerHTML = `<div class="notice">Loaded saved lineup from ${escapeHtml(project.savedAt ? new Date(project.savedAt).toLocaleString() : fileDateFallback())}.</div>`;
    }

    function currentFormState() {
      return {
        schoolUrl: schoolUrlInput.value.trim(),
        opponentUrls: opponentsInput.value.split(/\n+/).map(x => x.trim()).filter(Boolean),
        injuredAthletes: injuredAthletesInput.value.split(/\n+/).map(x => x.trim()).filter(Boolean),
        athleteEventLimits: collectAthleteEventLimits(),
        teamEventLimit: Number(teamEventLimitInput.value),
        gender: genderInput.value,
        seasonType: seasonTypeInput.value,
        indoorSprintDistance: indoorSprintDistanceInput.value
      };
    }

    function restoreFormState(state) {
      seasonTypeInput.value = ["outdoor", "indoor"].includes(state.seasonType)
        ? state.seasonType
        : "outdoor";
      indoorSprintDistanceInput.value = ["55", "60"].includes(String(state.indoorSprintDistance))
        ? String(state.indoorSprintDistance)
        : "55";
      updateSeasonControls();
      schoolUrlInput.value = state.schoolUrl || "";
      opponentsInput.value = Array.isArray(state.opponentUrls) ? state.opponentUrls.join("\n") : "";
      injuredAthletesInput.value = Array.isArray(state.injuredAthletes) ? state.injuredAthletes.join("\n") : "";
      teamEventLimitInput.value = ["2", "3", "4"].includes(String(state.teamEventLimit))
        ? String(state.teamEventLimit)
        : "4";
      restoreAthleteEventLimits(state.athleteEventLimits || []);
      if (["mens", "womens", "both"].includes(state.gender)) genderInput.value = state.gender;
    }

    function updateSeasonControls() {
      indoorSprintSettings.hidden = seasonTypeInput.value !== "indoor";
    }

    function addAthleteLimitRow(athlete = "", maxEvents = 2) {
      const row = document.createElement("div");
      row.className = "athlete-limit-row";
      row.innerHTML = `
        <input class="athlete-limit-name" type="text" value="${escapeHtml(athlete)}" placeholder="Athlete name" aria-label="Athlete name">
        <select class="athlete-limit-max" aria-label="Maximum events">
          ${[1, 2, 3, 4].map(limit => `<option value="${limit}" ${Number(maxEvents) === limit ? "selected" : ""}>Max ${limit}</option>`).join("")}
        </select>
        <button class="athlete-limit-remove" type="button" data-remove-athlete-limit aria-label="Remove athlete limit">x</button>
      `;
      athleteLimitList.appendChild(row);
    }

    function collectAthleteEventLimits() {
      const limits = new Map();
      athleteLimitList.querySelectorAll(".athlete-limit-row").forEach(row => {
        const athlete = row.querySelector(".athlete-limit-name")?.value.trim() || "";
        const maxEvents = Number(row.querySelector(".athlete-limit-max")?.value || 4);
        if (!athlete) return;
        const key = athleteLimitKey(athlete);
        const current = limits.get(key);
        if (!current || maxEvents < current.maxEvents) limits.set(key, {athlete, maxEvents});
      });
      return [...limits.values()];
    }

    function restoreAthleteEventLimits(limits) {
      athleteLimitList.innerHTML = "";
      if (!Array.isArray(limits)) return;
      limits.forEach(limit => addAthleteLimitRow(limit.athlete || limit.name || "", limit.maxEvents || limit.max_events || 2));
    }

    function downloadJson(data, filename) {
      const blob = new Blob([JSON.stringify(data, null, 2)], {type: "application/json"});
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }

    function projectFileName(project) {
      const formState = project.form || {};
      const teamId = String(formState.schoolUrl || "").match(/team\/(\d+)/)?.[1] || "lineup";
      const division = formState.gender === "both" ? "both" : (formState.gender || activeDivision || "lineup");
      const date = new Date().toISOString().slice(0, 10);
      return `track-lineup-${safeFilePart(teamId)}-${safeFilePart(division)}-${date}.json`;
    }

    function safeFilePart(value) {
      return String(value || "lineup").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "lineup";
    }

    function fileDateFallback() {
      return "saved file";
    }

    function render(data) {
      if (data.mode === "both" && data.division_results) {
        divisionResults = data.division_results;
        activeDivision = "mens";
        divisionTabs.hidden = false;
        updateDivisionTabs();
        renderSingle(divisionResults[activeDivision] || {});
        return;
      }
      divisionResults = null;
      divisionTabs.hidden = true;
      renderSingle(data);
    }

    function updateDivisionTabs() {
      divisionTabs.querySelectorAll("[data-division]").forEach(button => {
        button.classList.toggle("active", button.dataset.division === activeDivision);
      });
    }

    function updateEventSortControls() {
      eventSortControls.querySelectorAll("[data-sort]").forEach(button => {
        button.classList.toggle("active", button.dataset.sort === activeEventSort);
      });
    }

    function currentEventSortOrders() {
      const context = currentResult?.edit_context || {};
      return {
        schedule: Array.isArray(context.schedule_order) && context.schedule_order.length
          ? context.schedule_order
          : OUTDOOR_EVENT_SORT_ORDERS.schedule,
        distance: Array.isArray(context.distance_order) && context.distance_order.length
          ? context.distance_order
          : OUTDOOR_EVENT_SORT_ORDERS.distance
      };
    }

    function currentAllEvents() {
      const events = currentResult?.edit_context?.events;
      return Array.isArray(events) && events.length ? events : OUTDOOR_ALL_EVENTS;
    }

    function currentRelayEvents() {
      const events = currentResult?.edit_context?.relay_events;
      return new Set(Array.isArray(events) && events.length ? events : OUTDOOR_RELAY_EVENTS);
    }

    function currentRunningOrder() {
      const order = currentResult?.edit_context?.running_order;
      return order && typeof order === "object" ? order : OUTDOOR_RUNNING_ORDER;
    }

    function renderSingleLegacy(data) {
      closeAthletePanel();
      athleteIndex = buildAthleteIndex(data);
      document.querySelector("#total-points").textContent = Number(data.total_points || 0).toFixed(1);
      document.querySelector("#school-count").textContent = data.scraped?.school_records || 0;
      document.querySelector("#opponent-count").textContent = data.scraped?.opponent_records || 0;
      renderTeamScores(data);
      errors.innerHTML = (data.errors || []).map(error => `<div class="error">${escapeHtml(error)}</div>`).join("");
      const eventPoints = data.event_points || {};
      const cards = [];
      for (const [event, entries] of Object.entries(data.lineup || {})) {
        if (!entries.length) continue;
        cards.push(`
          <article class="event-card">
            <div class="event-head"><h3>${escapeHtml(titleCase(event))}</h3><span class="points">${Number(eventPoints[event] || 0).toFixed(1)} pts</span></div>
            <ol>${entries.map(entry => `<li><strong>${escapeHtml(entry.athlete)}</strong> ${escapeHtml(entry.adjusted_mark)} <span class="muted">(${formatPlace(entry.projected_place_label)} - ${formatPoints(entry.projected_points)} points)${entry.mark !== entry.adjusted_mark ? ` · PR ${escapeHtml(entry.mark)}` : ""}</span></li>`).join("")}</ol>
          </article>
        `);
      }
      for (const [event, relay] of Object.entries(data.relays || {})) {
        cards.push(`
          <article class="event-card relay">
            <div class="event-head"><h3>${escapeHtml(titleCase(event))}</h3><span class="points">${Number(relay.projected_points || 0).toFixed(1)} pts</span></div>
            <ol>${relay.athletes.map(name => `<li><strong>${escapeHtml(name)}</strong></li>`).join("")}</ol>
            <div class="muted">Projected ${escapeHtml(relay.projected_mark || "n/a")} · ${escapeHtml(relay.method || "relay")} ${relay.source_mark ? `from ${escapeHtml(relay.source_mark)}` : ""}</div>
          </article>
        `);
      }
      results.innerHTML = cards.join("") || `<div class="panel">No lineup could be generated from the parsed records.</div>`;
    }

    function renderSingle(data) {
      closeAthletePanel();
      closeEventInfo();
      currentResult = data || {};
      athleteIndex = buildAthleteIndex(currentResult);
      document.querySelector("#total-points").textContent = Number(currentResult.total_points || 0).toFixed(1);
      document.querySelector("#school-count").textContent = currentResult.scraped?.school_records || 0;
      document.querySelector("#opponent-count").textContent = currentResult.scraped?.opponent_records || 0;
      renderTeamScores(currentResult);
      renderScoreChange();
      errors.innerHTML = (currentResult.errors || []).map(error => `<div class="error">${escapeHtml(error)}</div>`).join("");
      const eventPoints = currentResult.event_points || {};
      const cards = [];
      for (const event of sortedEventNames(currentResult)) {
        const entries = currentResult.lineup?.[event] || [];
        const relay = currentResult.relays?.[event];
        if (entries.length) cards.push(renderIndividualEventCard(event, entries, eventPoints));
        if (relay) cards.push(renderRelayEventCard(event, relay));
      }
      results.innerHTML = cards.join("") || `<div class="panel">No lineup could be generated from the parsed records.</div>`;
    }

    function scoreChangeKey() {
      return divisionResults ? `division:${activeDivision}` : "single";
    }

    function renderScoreChange() {
      const change = manualScoreChanges[scoreChangeKey()];
      if (!change) {
        scoreChange.hidden = true;
        scoreChange.classList.remove("positive", "negative", "neutral");
        return;
      }
      const before = Number(change.before || 0);
      const after = Number(change.after || 0);
      const delta = after - before;
      const isNeutral = Math.abs(delta) < 0.005;
      scoreChange.hidden = false;
      scoreChange.classList.toggle("positive", delta > 0.005);
      scoreChange.classList.toggle("negative", delta < -0.005);
      scoreChange.classList.toggle("neutral", isNeutral);
      scoreChangeDelta.textContent = isNeutral
        ? "0.0 points"
        : `${delta > 0 ? "+" : ""}${delta.toFixed(1)} points`;
      scoreChangeDetail.textContent = isNeutral
        ? `No estimated net change (${before.toFixed(1)} to ${after.toFixed(1)} projected points).`
        : `Projected team score changed from ${before.toFixed(1)} to ${after.toFixed(1)} points.`;
    }

    function renderIndividualEventCard(event, entries, eventPoints) {
      return `
        <article class="event-card">
          <div class="event-head"><div class="event-title"><h3>${escapeHtml(titleCase(event))}</h3>${eventInfoIcon(event)}</div><span class="points">${Number(eventPoints[event] || 0).toFixed(1)} pts</span></div>
          <ol>${entries.map(entry => `<li>${athleteButton(entry.athlete)} ${escapeHtml(entry.adjusted_mark)}${markOriginBadge(entry.mark_origin)} <span class="muted">(${formatPlace(entry.projected_place_label)} - ${formatPoints(entry.projected_points)} points)${entry.mark !== entry.adjusted_mark ? ` - PR ${escapeHtml(entry.mark)}` : ""}</span></li>`).join("")}</ol>
        </article>
      `;
    }

    function markOriginBadge(origin) {
      const normalized = String(origin || "").toLowerCase();
      if (!['historical', 'predicted'].includes(normalized)) return "";
      const label = normalized === 'predicted' ? 'Predicted' : 'Historical';
      return `<span class="mark-origin ${normalized}">${label}</span>`;
    }

    function eventInfoIcon(event) {
      const rows = (currentResult.event_standings?.[event] || []).slice(0, 8);
      if (!rows.length) return "";
      const isOpen = selectedEventInfo === event;
      return `
        <button class="event-info ${isOpen ? "open" : ""}" type="button" data-event-info="${escapeHtml(event)}" aria-expanded="${isOpen ? "true" : "false"}" aria-label="Projected top eight places for ${escapeHtml(titleCase(event))}">i
          <span class="event-info-popover" role="tooltip">
            <span class="standings-title">${escapeHtml(titleCase(event))} projected places</span>
            <span class="standings-list">
              ${rows.map(renderStandingRow).join("")}
            </span>
          </span>
        </button>
      `;
    }

    function toggleEventInfo(event) {
      selectedEventInfo = selectedEventInfo === event ? "" : event;
      updateEventInfoPopovers();
    }

    function closeEventInfo() {
      if (!selectedEventInfo) return;
      selectedEventInfo = "";
      updateEventInfoPopovers();
    }

    function updateEventInfoPopovers() {
      results.querySelectorAll(".event-info").forEach(button => {
        const isOpen = Boolean(selectedEventInfo) && button.dataset.eventInfo === selectedEventInfo;
        button.classList.toggle("open", isOpen);
        button.setAttribute("aria-expanded", isOpen ? "true" : "false");
      });
    }

    function renderStandingRow(row) {
      return `
        <span class="standing-row ${row.team_role === "school" ? "school" : ""}">
          <span class="standing-place">${escapeHtml(row.place_label || ordinalLabel(row.place))}</span>
          <span class="standing-name"><strong>${escapeHtml(row.athlete || "Unknown athlete")}</strong><span>${escapeHtml(row.school || "Unknown school")}</span></span>
          <span class="standing-mark">${escapeHtml(row.projected_mark || "n/a")}${markOriginBadge(row.mark_origin)}</span>
          <span class="standing-points">${formatPoints(row.projected_points)} pts</span>
        </span>
      `;
    }

    function renderRelayEventCard(event, relay) {
      return `
        <article class="event-card relay">
          <div class="event-head"><div class="event-title"><h3>${escapeHtml(titleCase(event))}</h3>${eventInfoIcon(event)}</div><span class="points">${Number(relay.projected_points || 0).toFixed(1)} pts</span></div>
          <ol>${relay.athletes.map(name => `<li>${athleteButton(name)}</li>`).join("")}</ol>
          <div class="muted">Projected ${escapeHtml(relay.projected_mark || "n/a")} - ${escapeHtml(relay.method || "relay")} ${relay.source_mark ? `from ${escapeHtml(relay.source_mark)}` : ""}</div>
        </article>
      `;
    }

    function renderTeamScores(data) {
      const scores = data?.team_points || {};
      let entries = Object.entries(scores);
      const schoolName = data?.scraped?.school_name || "Your Team";
      if (!entries.length && Number(data?.total_points || 0) > 0) {
        entries = [[schoolName, Number(data.total_points || 0)]];
      }
      let highlightedMain = false;
      teamPointsList.innerHTML = entries.length
        ? entries.map(([team, points]) => {
          const number = Number(points || 0);
          const isMain = team === schoolName || (!highlightedMain && Math.abs(number - Number(data?.total_points || 0)) < 0.01);
          if (isMain) highlightedMain = true;
          return `
            <span class="team-score ${isMain ? "school" : ""}">
              <span class="team-score-name">${escapeHtml(team)}</span>
              <span class="team-score-points">${number.toFixed(1)}</span>
            </span>
          `;
        }).join("")
        : `<span class="muted">No projected team scores yet</span>`;
    }

    function sortedEventNames(data) {
      const names = new Set();
      for (const [event, entries] of Object.entries(data.lineup || {})) {
        if ((entries || []).length) names.add(event);
      }
      for (const [event, relay] of Object.entries(data.relays || {})) {
        if (relay) names.add(event);
      }
      return [...names].sort((a, b) => eventSortRank(a) - eventSortRank(b) || titleCase(a).localeCompare(titleCase(b)));
    }

    function eventSortRank(event) {
      const orders = currentEventSortOrders();
      const order = orders[activeEventSort] || orders.schedule;
      const rank = order.indexOf(event);
      return rank >= 0 ? rank : 1000;
    }

    function athleteButton(name) {
      return `<button class="athlete-chip" type="button" data-athlete-key="${escapeHtml(athleteKey(name))}" data-athlete-name="${escapeHtml(name)}">${escapeHtml(name)}</button>`;
    }

    function buildAthleteIndex(data) {
      const index = new Map();
      for (const [event, entries] of Object.entries(data.lineup || {})) {
        for (const entry of entries || []) {
          addAthleteEvent(index, entry.athlete, {
            event,
            mark: entry.adjusted_mark || entry.mark || "n/a",
            markOrigin: entry.mark_origin || "",
            place: entry.projected_place_label || "unplaced",
            points: Number(entry.projected_points || 0),
            type: "Individual"
          });
        }
      }
      for (const [event, relay] of Object.entries(data.relays || {})) {
        for (const name of relay.athletes || []) {
          addAthleteEvent(index, name, {
            event,
            mark: relay.projected_mark || "n/a",
            place: relayPlaceFromPoints(Number(relay.projected_points || 0)),
            points: Number(relay.projected_points || 0),
            type: "Relay"
          });
        }
      }
      return index;
    }

    function addAthleteEvent(index, athlete, detail) {
      const key = athleteKey(athlete);
      if (!index.has(key)) index.set(key, {name: athlete, events: []});
      index.get(key).events.push(detail);
    }

    function openAthletePanel(name) {
      const key = athleteKey(name);
      const athlete = athleteIndex.get(key);
      if (!athlete) return;
      selectedAthleteKey = key;
      highlightAthlete();
      athletePanelName.textContent = athlete.name;
      const maxEvents = athleteMaxEvents(athlete.name);
      athletePanelCount.textContent = `${athlete.events.length} ${athlete.events.length === 1 ? "event" : "events"}${maxEvents < 4 ? ` (max ${maxEvents})` : ""}`;
      const athleteEvents = [...athlete.events].sort((a, b) => eventSortRank(a.event) - eventSortRank(b.event) || titleCase(a.event).localeCompare(titleCase(b.event)));
      const addAction = athlete.events.length < maxEvents ? `
        <li class="athlete-panel-actions">
          <button class="mini-button add-action" type="button" data-edit-action="add" data-athlete="${escapeHtml(athlete.name)}" data-event="">Add Event</button>
        </li>
      ` : "";
      athletePanelEvents.innerHTML = addAction + athleteEvents.map(detail => `
        <li class="athlete-event-item">
          <div class="athlete-event-top">
            <span>${escapeHtml(titleCase(detail.event))}</span>
            <span class="athlete-event-mark">${escapeHtml(detail.mark)}${markOriginBadge(detail.markOrigin)}</span>
          </div>
          <div class="athlete-event-meta">
            <span class="athlete-pill">${escapeHtml(detail.type)}</span>
            <span class="athlete-pill">${formatPlace(detail.place)}</span>
            <span class="athlete-pill points-pill">${formatPoints(detail.points)} pts</span>
          </div>
          <div class="athlete-event-actions">
            <button class="mini-button move-action" type="button" data-edit-action="move" data-athlete="${escapeHtml(athlete.name)}" data-event="${escapeHtml(detail.event)}">Move</button>
            <button class="mini-button remove-action" type="button" data-edit-action="remove" data-athlete="${escapeHtml(athlete.name)}" data-event="${escapeHtml(detail.event)}">Remove</button>
          </div>
        </li>
      `).join("");
      athletePanel.hidden = false;
      document.body.classList.add("athlete-panel-open");
      placePanelOnSide();
    }

    function closeAthletePanel() {
      editState = null;
      selectedAthleteKey = "";
      highlightAthlete();
      athletePanel.hidden = true;
      document.body.classList.remove("athlete-panel-open");
      panelDrag = null;
    }

    function highlightAthlete() {
      results.querySelectorAll(".athlete-chip").forEach(button => {
        button.classList.toggle("selected", Boolean(selectedAthleteKey) && button.dataset.athleteKey === selectedAthleteKey);
      });
    }

    function placePanelOnSide() {
      if (athletePanel.hidden) return;
      const height = athletePanel.offsetHeight || 420;
      athletePanel.style.left = `${window.innerWidth <= 880 ? 12 : 18}px`;
      athletePanel.style.top = `${window.innerWidth <= 880 ? Math.max(12, window.innerHeight - height - 12) : 112}px`;
      athletePanel.style.right = "auto";
      athletePanel.style.bottom = "auto";
    }

    function startPanelDrag(event) {
      if (event.target.closest(".athlete-panel-close")) return;
      const rect = athletePanel.getBoundingClientRect();
      panelDrag = {
        pointerId: event.pointerId,
        offsetX: event.clientX - rect.left,
        offsetY: event.clientY - rect.top
      };
      athletePanelHead.setPointerCapture(event.pointerId);
    }

    function dragAthletePanel(event) {
      if (!panelDrag || event.pointerId !== panelDrag.pointerId) return;
      const width = athletePanel.offsetWidth;
      const height = athletePanel.offsetHeight;
      const left = Math.min(Math.max(8, event.clientX - panelDrag.offsetX), Math.max(8, window.innerWidth - width - 8));
      const top = Math.min(Math.max(8, event.clientY - panelDrag.offsetY), Math.max(8, window.innerHeight - height - 8));
      athletePanel.style.left = `${left}px`;
      athletePanel.style.top = `${top}px`;
      athletePanel.style.right = "auto";
      athletePanel.style.bottom = "auto";
    }

    function stopPanelDrag(event) {
      if (!panelDrag || event.pointerId !== panelDrag.pointerId) return;
      panelDrag = null;
    }

    function startCoachEdit(mode, athlete, sourceEvent) {
      if (!currentResult?.edit_context) {
        errors.innerHTML = `<div class="error">Generate a lineup before editing assignments.</div>`;
        return;
      }
      const maxEvents = athleteMaxEvents(athlete);
      if (mode === "add" && athleteEventList(athlete).length >= maxEvents) {
        errors.innerHTML = `<div class="error">${escapeHtml(athlete)} is already at the maximum of ${maxEvents} ${maxEvents === 1 ? "event" : "events"}.</div>`;
        return;
      }
      editState = {
        mode,
        athlete,
        sourceEvent: sourceEvent || "",
        targetEvent: "",
        sourceReplacement: "",
        targetRemoval: ""
      };
      selectedAthleteKey = athleteKey(athlete);
      highlightAthlete();
      renderCoachEditPanel();
      athletePanel.hidden = false;
      document.body.classList.add("athlete-panel-open");
      placePanelOnSide();
    }

    function renderCoachEditPanel() {
      if (!editState) return;
      athletePanelName.textContent = editPanelTitle();
      athletePanelCount.textContent = editState.sourceEvent ? titleCase(editState.sourceEvent) : "New event";
      const needsSourceReplacement = editState.mode !== "add";
      const needsTargetEvent = editState.mode !== "remove";
      const sourceSuggestions = needsSourceReplacement ? replacementSuggestions(editState.sourceEvent, editState.athlete) : [];
      if (needsSourceReplacement && editState.sourceReplacement && !sourceSuggestions.some(item => item.athlete === editState.sourceReplacement)) {
        editState.sourceReplacement = "";
      }
      const targetStatus = needsTargetEvent && editState.targetEvent
        ? targetEventStatus(editState.athlete, editState.sourceEvent, editState.targetEvent)
        : {reasons: []};
      const targetChoices = needsTargetEvent && editState.targetEvent
        ? eventAthletes(editState.targetEvent).filter(name => athleteKey(name) !== athleteKey(editState.athlete))
        : [];
      if (editState.targetRemoval && !targetChoices.includes(editState.targetRemoval)) {
        editState.targetRemoval = "";
      }
      const canApply = Boolean(
        editState.mode === "remove"
          ? editState.sourceReplacement
          : (
            editState.targetEvent
            && !targetStatus.reasons.length
            && (!targetChoices.length || editState.targetRemoval)
            && (editState.mode === "add" || editState.sourceReplacement)
          )
      );
      athletePanelEvents.innerHTML = `
        <li class="coach-edit-block">
          <p class="coach-edit-title">${escapeHtml(editPanelLead())}</p>
          ${needsTargetEvent ? renderEventScheduleChoices(targetStatus) : ""}
          ${needsSourceReplacement ? renderReplacementQuestion(
            "sourceReplacement",
            `Who will replace ${editState.athlete} in ${titleCase(editState.sourceEvent)}?`,
            sourceSuggestions,
            editState.sourceReplacement
          ) : ""}
          ${needsTargetEvent && editState.targetEvent ? renderTargetRemovalQuestion(targetChoices) : ""}
          <div class="edit-actions">
            <button class="mini-button" type="button" data-edit-command="back">Back</button>
            <button type="button" data-edit-command="apply" ${canApply ? "" : "disabled"}>Apply Change</button>
          </div>
        </li>
      `;
    }

    function editPanelTitle() {
      if (editState.mode === "add") return `Add event for ${editState.athlete}`;
      if (editState.mode === "move") return `Move ${editState.athlete}`;
      return `Remove ${editState.athlete}`;
    }

    function editPanelLead() {
      if (editState.mode === "add") return `Add an event for ${editState.athlete}`;
      if (editState.mode === "move") return `Move ${editState.athlete} from ${titleCase(editState.sourceEvent)}`;
      return `Remove ${editState.athlete} from ${titleCase(editState.sourceEvent)}`;
    }

    function renderEventScheduleChoices(selectedStatus) {
      const currentEvents = new Set(athleteEventList(editState.athlete));
      const selectedAlert = editState.targetEvent
        ? (
          selectedStatus.reasons.length
            ? `<div class="edit-alert">${selectedStatus.reasons.map(escapeHtml).join(" ")}</div>`
            : `<div class="edit-alert ok">Valid target selected: ${escapeHtml(titleCase(editState.targetEvent))}.</div>`
        )
        : `<div class="edit-warning">Current events are light blue and disabled. Yellow events need review; click one to see why.</div>`;
      return `
        <div>
          <p class="coach-edit-question">Which event should ${escapeHtml(editState.athlete)} move into?</p>
          ${selectedAlert}
          <div class="event-choice-grid">
            ${currentAllEvents().map(event => {
              const status = targetEventStatus(editState.athlete, editState.sourceEvent, event);
              const isCurrent = currentEvents.has(event);
              const classes = [
                "event-choice",
                isCurrent ? "current" : "",
                !isCurrent && status.reasons.length ? "warning" : "",
                editState.targetEvent === event ? "selected" : ""
              ].filter(Boolean).join(" ");
              return `<button class="${classes}" type="button" data-target-event="${escapeHtml(event)}" ${isCurrent ? "disabled" : ""}>${escapeHtml(titleCase(event))}</button>`;
            }).join("")}
          </div>
        </div>
      `;
    }

    function renderReplacementQuestion(role, question, choices, selected) {
      return `
        <div>
          <p class="coach-edit-question">${escapeHtml(question)}</p>
          <div class="choice-list">
            ${choices.length ? choices.map(choice => renderChoiceCard(role, choice, selected)).join("") : `<div class="edit-warning">No available recorded replacement was found for ${escapeHtml(titleCase(editState.sourceEvent))}.</div>`}
          </div>
        </div>
      `;
    }

    function renderTargetRemovalQuestion(choices) {
      if (!choices.length) {
        return `<div class="edit-warning">No athlete needs to be removed from ${escapeHtml(titleCase(editState.targetEvent))} because there is an open spot.</div>`;
      }
      return `
        <div>
          <p class="coach-edit-question">${escapeHtml(`Who will ${editState.athlete} replace in ${titleCase(editState.targetEvent)}?`)}</p>
          <div class="choice-list">
            ${choices.map(name => renderChoiceCard("targetRemoval", {
              athlete: name,
              mark: athleteEventMark(name, editState.targetEvent),
              warnings: []
            }, editState.targetRemoval)).join("")}
          </div>
        </div>
      `;
    }

    function renderChoiceCard(role, choice, selected) {
      const warnings = choice.warnings?.length ? `<span class="choice-warning-text">${escapeHtml(choice.warnings.join(" "))}</span>` : "";
      const eventSummary = athleteEventList(choice.athlete).map(titleCase).join(", ") || "No current events";
      return `
        <button class="choice-card ${selected === choice.athlete ? "selected" : ""}" type="button" data-choice-role="${escapeHtml(role)}" data-athlete="${escapeHtml(choice.athlete)}">
          <strong class="choice-name-line">
            ${escapeHtml(choice.athlete)}
            <span class="event-asterisk" tabindex="0">*
              <span class="event-asterisk-tip">Current events: ${escapeHtml(eventSummary)}</span>
            </span>
          </strong>
          <span>${escapeHtml(choice.mark || "recorded mark")}</span>
          ${warnings}
        </button>
      `;
    }

    function handleEditCommand(command) {
      if (!editState) return;
      if (command === "back") {
        openAthletePanel(editState.athlete);
        return;
      }
      if (command === "apply") applyCoachEdit();
    }

    async function applyCoachEdit() {
      if (!editState || !currentResult) return;
      const next = cloneResult(currentResult);
      try {
        if (editState.mode === "remove") {
          replaceEventAthlete(next, editState.sourceEvent, editState.athlete, editState.sourceReplacement);
        } else if (editState.mode === "add") {
          addAthleteToEvent(next, editState.targetEvent, editState.athlete, editState.targetRemoval);
        } else {
          replaceEventAthlete(next, editState.sourceEvent, editState.athlete, editState.sourceReplacement);
          addAthleteToEvent(next, editState.targetEvent, editState.athlete, editState.targetRemoval);
        }
        await rescoreEditedLineup(next);
      } catch (error) {
        errors.innerHTML = `<div class="error">${escapeHtml(error.message)}</div>`;
      }
    }

    async function rescoreEditedLineup(next) {
      athletePanelCount.textContent = "rescoring...";
      const previousPoints = Number(currentResult?.total_points || 0);
      const changeKey = scoreChangeKey();
      const response = await fetch("/api/rescore", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          lineup: next.lineup || {},
          relays: next.relays || {},
          edit_context: currentResult.edit_context || {}
        })
      });
      const data = await response.json();
      if (!response.ok) throw new Error((data.errors || [data.error || "Could not rescore edited lineup."])[0]);
      manualScoreChanges[changeKey] = {
        before: previousPoints,
        after: Number(data.total_points || 0)
      };
      if (divisionResults) divisionResults[activeDivision] = data;
      renderSingle(data);
    }

    function replaceEventAthlete(result, event, outgoingAthlete, incomingAthlete) {
      if (!incomingAthlete) throw new Error(`Choose a replacement for ${titleCase(event)}.`);
      if (currentRelayEvents().has(event)) {
        replaceRelayAthlete(result, event, outgoingAthlete, incomingAthlete);
        return;
      }
      const entries = result.lineup?.[event] || [];
      const nextEntries = entries.filter(entry => athleteKey(entry.athlete) !== athleteKey(outgoingAthlete));
      if (!nextEntries.some(entry => athleteKey(entry.athlete) === athleteKey(incomingAthlete))) {
        nextEntries.push(entryForAthlete(incomingAthlete, event));
      }
      result.lineup[event] = nextEntries.slice(0, 3);
    }

    function addAthleteToEvent(result, event, athlete, removedAthlete) {
      if (currentRelayEvents().has(event)) {
        if (!removedAthlete) throw new Error(`Choose who ${athlete} will replace in ${titleCase(event)}.`);
        replaceRelayAthlete(result, event, removedAthlete, athlete);
        return;
      }
      const entries = [...(result.lineup?.[event] || [])];
      const nextEntries = removedAthlete
        ? entries.filter(entry => athleteKey(entry.athlete) !== athleteKey(removedAthlete))
        : entries;
      if (!nextEntries.some(entry => athleteKey(entry.athlete) === athleteKey(athlete))) {
        nextEntries.push(entryForAthlete(athlete, event));
      }
      result.lineup[event] = nextEntries.slice(0, 3);
    }

    function replaceRelayAthlete(result, event, outgoingAthlete, incomingAthlete) {
      const relay = result.relays?.[event];
      if (!relay) throw new Error(`${titleCase(event)} is not currently in the lineup.`);
      const index = (relay.athletes || []).findIndex(name => athleteKey(name) === athleteKey(outgoingAthlete));
      if (index < 0) throw new Error(`${outgoingAthlete} is not in ${titleCase(event)}.`);
      const oldLeg = relayLegValue(event, outgoingAthlete) ?? relayLegTimeAt(relay, index);
      const newLeg = relayLegValue(event, incomingAthlete);
      if (!Number.isFinite(newLeg)) throw new Error(`${incomingAthlete} does not have a relay-compatible mark for ${titleCase(event)}.`);
      relay.athletes[index] = incomingAthlete;
      const oldSeconds = Number(relay.projected_seconds || 0);
      relay.projected_seconds = Math.max(0, oldSeconds + newLeg - oldLeg);
      relay.projected_mark = formatSeconds(relay.projected_seconds);
      relay.method = "coach edited";
      relay.source_mark = "old relay time plus leg-time difference";
      relay.leg_times = Array.isArray(relay.leg_times) && relay.leg_times.length === 4
        ? relay.leg_times.map((value, legIndex) => legIndex === index ? newLeg : value)
        : [];
    }

    function replacementSuggestions(event, removedAthlete) {
      const existing = new Set(eventAthletes(event).map(athleteKey));
      const candidates = eventCandidates(event)
        .filter(choice => athleteKey(choice.athlete) !== athleteKey(removedAthlete))
        .filter(choice => !existing.has(athleteKey(choice.athlete)))
        .filter(choice => athleteEventList(choice.athlete).length < athleteMaxEvents(choice.athlete))
        .filter(choice => !hasAdjacentRunningEvent(choice.athlete, event))
        .map(choice => ({...choice, warnings: replacementWarnings(choice.athlete, event)}));
      candidates.sort((a, b) => eventBetterValue(event, a.value, b.value));
      return candidates.slice(0, 6);
    }

    function eventCandidates(event) {
      if (currentRelayEvents().has(event)) {
        const legs = currentResult.edit_context?.relay_leg_values?.[event] || {};
        return Object.entries(legs).map(([athlete, value]) => ({
          athlete,
          value: Number(value),
          mark: formatSeconds(Number(value))
        }));
      }
      const best = new Map();
      for (const perf of currentResult.edit_context?.school_performances || []) {
        if (perf.event !== event) continue;
        const key = athleteKey(perf.athlete);
        const current = best.get(key);
        if (!current || eventBetterValue(event, perf.value, current.value) < 0) {
          best.set(key, {athlete: perf.athlete, value: Number(perf.value), mark: perf.mark});
        }
      }
      return [...best.values()];
    }

    function replacementWarnings(athlete, event) {
      const events = [...athleteEventList(athlete), event];
      const warnings = [];
      if (hasForbidden400Double(events)) warnings.push("400/4x400 warning.");
      if (distanceLimitExceeded(events)) warnings.push("distance load warning.");
      return warnings;
    }

    function targetEventStatus(athlete, sourceEvent, targetEvent) {
      const currentEvents = athleteEventList(athlete);
      const afterMove = currentEvents.filter(event => event !== sourceEvent);
      const reasons = [];
      if (targetEvent === sourceEvent) reasons.push(`${athlete} is already in ${titleCase(sourceEvent)}.`);
      if (afterMove.includes(targetEvent)) reasons.push(`${athlete} is already entered in ${titleCase(targetEvent)}.`);
      if (!hasRecordedEventMark(athlete, targetEvent)) reasons.push(`${athlete} does not have a recorded mark for ${titleCase(targetEvent)}.`);
      const proposedEvents = [...afterMove, targetEvent];
      const maxEvents = athleteMaxEvents(athlete);
      if (proposedEvents.length > maxEvents) reasons.push(`${athlete} would exceed the maximum of ${maxEvents} ${maxEvents === 1 ? "event" : "events"}.`);
      if (hasForbidden400Double(proposedEvents)) reasons.push(`${athlete} would be in both the 400m and 4x400 relay.`);
      if (distanceLimitExceeded(proposedEvents)) reasons.push(`${athlete} would exceed the distance-event limit.`);
      if (hasAdjacentRunningPair(proposedEvents)) reasons.push(`${athlete} would have back to back running events.`);
      return {reasons};
    }

    function eventAthletes(event) {
      if (currentRelayEvents().has(event)) return [...(currentResult.relays?.[event]?.athletes || [])];
      return (currentResult.lineup?.[event] || []).map(entry => entry.athlete);
    }

    function athleteEventList(athlete) {
      const key = athleteKey(athlete);
      const events = [];
      for (const [event, entries] of Object.entries(currentResult?.lineup || {})) {
        if ((entries || []).some(entry => athleteKey(entry.athlete) === key)) events.push(event);
      }
      for (const [event, relay] of Object.entries(currentResult?.relays || {})) {
        if ((relay.athletes || []).some(name => athleteKey(name) === key)) events.push(event);
      }
      return events;
    }

    function athleteEventMark(athlete, event) {
      if (currentRelayEvents().has(event)) {
        const value = relayLegValue(event, athlete);
        return Number.isFinite(value) ? formatSeconds(value) : "relay leg";
      }
      const entry = (currentResult.lineup?.[event] || []).find(item => athleteKey(item.athlete) === athleteKey(athlete));
      if (entry) return entry.adjusted_mark || entry.mark || "recorded mark";
      const perf = bestPerformance(athlete, event);
      return perf?.mark || "recorded mark";
    }

    function hasRecordedEventMark(athlete, event) {
      if (currentRelayEvents().has(event)) return Number.isFinite(relayLegValue(event, athlete));
      return Boolean(bestPerformance(athlete, event));
    }

    function bestPerformance(athlete, event) {
      const key = athleteKey(athlete);
      let best = null;
      for (const perf of currentResult.edit_context?.school_performances || []) {
        if (perf.event !== event || athleteKey(perf.athlete) !== key) continue;
        if (!best || eventBetterValue(event, perf.value, best.value) < 0) best = perf;
      }
      return best;
    }

    function entryForAthlete(athlete, event) {
      const perf = bestPerformance(athlete, event);
      return {
        athlete,
        mark: perf?.mark || "n/a",
        adjusted_mark: perf?.mark || "n/a",
        projected_place_label: "unplaced",
        projected_points: 0
      };
    }

    function relayLegValue(event, athlete) {
      const value = currentResult.edit_context?.relay_leg_values?.[event]?.[athlete];
      return value === undefined ? NaN : Number(value);
    }

    function relayLegTimeAt(relay, index) {
      const explicit = Number(relay.leg_times?.[index]);
      if (Number.isFinite(explicit)) return explicit;
      const projected = Number(relay.projected_seconds || 0);
      return projected > 0 ? projected / 4 : 0;
    }

    function hasAdjacentRunningEvent(athlete, event) {
      const runningOrder = currentRunningOrder();
      if (!(event in runningOrder)) return false;
      const order = runningOrder[event];
      return athleteEventList(athlete).some(existing => existing in runningOrder && Math.abs(runningOrder[existing] - order) === 1);
    }

    function hasAdjacentRunningPair(events) {
      const runningOrder = currentRunningOrder();
      const ordered = events.filter(event => event in runningOrder).map(event => runningOrder[event]).sort((a, b) => a - b);
      return ordered.some((value, index) => index > 0 && value - ordered[index - 1] <= 1);
    }

    function distanceLimitExceeded(events) {
      if (!events.some(event => DISTANCE_EVENTS.has(event))) return false;
      if (events.some(event => LONG_DISTANCE_EVENTS.has(event))) return events.length > 2;
      if (events.length <= 2) return false;
      return events.length > 3 || !isUnder1600DistanceLoad(events);
    }

    function isUnder1600DistanceLoad(events) {
      return events.some(event => DISTANCE_EVENTS.has(event))
        && !events.some(event => LONG_DISTANCE_EVENTS.has(event))
        && events.every(event => DISTANCE_THREE_EVENT_ALLOWED_EVENTS.has(event));
    }

    function hasForbidden400Double(events) {
      return events.includes("400m")
        && events.includes("4x400 relay")
        && !isUnder1600DistanceLoad(events);
    }

    function eventBetterValue(event, a, b) {
      return FIELD_EVENTS.has(event) ? Number(b) - Number(a) : Number(a) - Number(b);
    }

    function cloneResult(value) {
      return JSON.parse(JSON.stringify(value));
    }

    function formatSeconds(value) {
      const seconds = Number(value);
      if (!Number.isFinite(seconds)) return "n/a";
      if (seconds >= 60) {
        const minutes = Math.floor(seconds / 60);
        const rest = (seconds - minutes * 60).toFixed(2).padStart(5, "0");
        return `${minutes}:${rest}`;
      }
      return seconds.toFixed(2);
    }

    function titleCase(value) {
      if (value === "55h" || value === "60h") return `${value.slice(0, -1)}m Hurdles`;
      return value.replace(/\b\w/g, letter => letter.toUpperCase()).replace("Relay", "Relay");
    }

    function formatPoints(value) {
      const number = Number(value || 0);
      return Number.isInteger(number) ? String(number) : number.toFixed(1);
    }

    function formatPlace(label) {
      return label && label !== "unplaced" ? `${escapeHtml(label)} place` : "unplaced";
    }

    function relayPlaceFromPoints(points) {
      const places = {10: "1st", 8: "2nd", 6: "3rd", 4: "4th", 2: "5th"};
      return places[points] || "unplaced";
    }

    function ordinalLabel(value) {
      const number = Number(value || 0);
      if (!number) return "n/a";
      const mod100 = number % 100;
      if (mod100 >= 10 && mod100 <= 20) return `${number}th`;
      const suffix = {1: "st", 2: "nd", 3: "rd"}[number % 10] || "th";
      return `${number}${suffix}`;
    }

    function athleteKey(value) {
      return String(value || "").trim().toLowerCase().replace(/\s+/g, " ");
    }

    function athleteLimitKey(value) {
      return String(value || "").trim().toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
    }

    function athleteMaxEvents(athlete) {
      const context = currentResult?.edit_context || {};
      const limits = context.athlete_event_limits || {};
      const teamLimit = Number(context.team_event_limit || context.max_events_per_athlete || 4);
      const athleteLimit = Number(limits[athleteLimitKey(athlete)] || 4);
      const limit = Math.min(teamLimit, athleteLimit);
      return Number.isInteger(limit) && limit >= 1 && limit <= 4 ? limit : 4;
    }

    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, char => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#039;"
      }[char]));
    }
  