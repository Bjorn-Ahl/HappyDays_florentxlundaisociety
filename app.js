/*
 * HappyDays — v2 app glue (DOM wiring, event handlers, rendering).
 *
 * Pure calculation lives in calc.js (window.HappyDaysCalc). This file is
 * intentionally thin: read inputs, call calc, paint results.
 *
 * Loaded as a plain <script defer>, so `open index.html` works without a
 * server. JSON data (SMHI irradiance, electricity zones) is fetched at
 * runtime; if that fails (e.g. file:// origin), we fall back to the
 * latitude-zoned heuristics inside calc.js.
 */
(function () {
  "use strict";

  // ---- Constants ---------------------------------------------------------

  // Sweden bounding box — rough rectangle the user is locked into.
  const SWEDEN_BOUNDS = L.latLngBounds([55.0, 10.5], [69.1, 24.2]);
  const SWEDEN_CENTER = [62.5, 16.5];

  // ---- DOM refs ----------------------------------------------------------

  const form = document.getElementById("advisor-form");
  const liveStatus = document.getElementById("live-status");
  const mapReadoutValue = document.getElementById("map-readout-value");
  const latInput = document.getElementById("lat");
  const lngInput = document.getElementById("lng");
  const citySelect = document.getElementById("city-select");
  const panelSelect = document.getElementById("panel-type");
  const panelNote = document.getElementById("panel-type-note");
  const upkeepInput = document.getElementById("upkeep");
  const upkeepOut = document.getElementById("upkeep-out");
  const sellExcess = document.getElementById("sell-excess");
  const feedInWrap = document.getElementById("feed-in-wrap");

  const results = document.getElementById("results");
  const resultsLead = document.getElementById("results-lead");
  const zoneChip = document.getElementById("zone-chip");
  const zoneDetail = document.getElementById("zone-detail");
  const panelSummary = document.getElementById("panel-summary");
  const irradianceSummary = document.getElementById("irradiance-summary");
  const flowSummary = document.getElementById("flow-summary");

  const paybackValue = document.getElementById("payback-value");
  const paybackLabel = document.getElementById("payback-label");
  const paybackSavings = document.getElementById("payback-savings");

  const segSolar = document.getElementById("seg-solar");
  const segGrid = document.getElementById("seg-grid");
  const pctSolar = document.getElementById("pct-solar");
  const pctGrid = document.getElementById("pct-grid");
  const coverageValue = document.getElementById("coverage-value");
  const independenceValue = document.getElementById("independence-value");
  const independenceLabel = document.getElementById("independence-label");

  const costTableBody = document.querySelector("#cost-table tbody");

  // ---- Async-loaded reference data ---------------------------------------

  let irradianceStations = [];      // from data/smhi_irradiance.json
  let irradianceMeta = { source: null, generatedAt: null };
  let electricityZones = null;      // from data/electricity_zones.json
  let localPriceStats = null;       // from data/lund_price_stats.json (SE4 only)

  // ---- Populate form UI --------------------------------------------------

  function populatePanelSelect() {
    const html = (window.PANEL_TYPES || [])
      .map(
        (p) =>
          `<option value="${p.id}"${p.id === "auto" ? " selected" : ""}>${p.label}</option>`
      )
      .join("");
    panelSelect.innerHTML = html;
  }

  function populateCityFallback() {
    const html = (window.SWEDISH_CITIES || [])
      .map((c) => `<option value="${c.name}">${c.name}</option>`)
      .join("");
    citySelect.insertAdjacentHTML("beforeend", html);
  }

  // ---- Leaflet map -------------------------------------------------------

  let map;
  let marker;

  function initMap() {
    map = L.map("map", {
      center: SWEDEN_CENTER,
      zoom: 5,
      minZoom: 4,
      maxZoom: 11,
      maxBounds: SWEDEN_BOUNDS,
      maxBoundsViscosity: 1.0,
      worldCopyJump: false,
    });

    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 11,
      attribution:
        '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(map);

    map.fitBounds(SWEDEN_BOUNDS);

    map.on("click", (e) => {
      setLocation(e.latlng.lat, e.latlng.lng, { source: "map" });
    });
  }

  function setLocation(lat, lng, { source } = {}) {
    if (!SWEDEN_BOUNDS.contains([lat, lng])) return;
    latInput.value = lat.toFixed(5);
    lngInput.value = lng.toFixed(5);
    const readout = `${lat.toFixed(2)}, ${lng.toFixed(2)}`;
    mapReadoutValue.textContent = readout;

    if (!marker) {
      marker = L.marker([lat, lng], {
        draggable: true,
        keyboard: true,
        title: "Chosen location — drag to move",
      }).addTo(map);
      marker.on("dragend", () => {
        const p = marker.getLatLng();
        setLocation(p.lat, p.lng, { source: "drag" });
      });
    } else {
      marker.setLatLng([lat, lng]);
    }

    if (source !== "drag") {
      map.panTo([lat, lng], { animate: true });
    }
  }

  // ---- Async data loading -----------------------------------------------

  async function loadReferenceData() {
    // fetch(url) may fail on file:// origin — we degrade gracefully.
    try {
      const res = await fetch("data/smhi_irradiance.json", { cache: "no-store" });
      if (res.ok) {
        const json = await res.json();
        irradianceStations = Array.isArray(json.stations) ? json.stations : [];
        irradianceMeta = {
          source: typeof json.source === "string" ? json.source : null,
          generatedAt: typeof json.generated_at === "string" ? json.generated_at : null,
        };
      }
    } catch (e) {
      console.warn("[HappyDays] SMHI JSON not loadable; using lat-zoned fallback.", e);
    }
    try {
      const res = await fetch("data/electricity_zones.json", { cache: "no-store" });
      if (res.ok) {
        const json = await res.json();
        electricityZones = json && json.zones ? json.zones : null;
      }
    } catch (e) {
      console.warn("[HappyDays] zones JSON not loadable; using default grid rate.", e);
    }
    try {
      const res = await fetch("data/lund_price_stats.json", { cache: "no-store" });
      if (res.ok) {
        const json = await res.json();
        if (json && json.stats && typeof json.zone === "string") {
          localPriceStats = json;
        }
      }
    } catch (e) {
      console.warn(
        "[HappyDays] local price-stats JSON not loadable; falling back to zone average.",
        e
      );
    }
  }

  // ---- Live output bindings ---------------------------------------------

  function bindLiveOutputs() {
    ["w-cost", "w-independence", "w-sustainability"].forEach((id) => {
      const input = document.getElementById(id);
      const out = document.getElementById(`${id}-out`);
      if (!input || !out) return;
      input.addEventListener("input", () => {
        out.textContent = input.value;
      });
    });

    upkeepInput.addEventListener("input", () => {
      const v = Number(upkeepInput.value);
      upkeepOut.textContent = `${v.toFixed(1)}%`;
    });

    sellExcess.addEventListener("change", () => {
      feedInWrap.hidden = !sellExcess.checked;
    });

    citySelect.addEventListener("change", () => {
      const name = citySelect.value;
      if (!name) return;
      const city = (window.SWEDISH_CITIES || []).find((c) => c.name === name);
      if (city) {
        setLocation(city.lat, city.lng, { source: "city" });
        announce(`Location set to ${city.name}.`);
      }
    });

    panelSelect.addEventListener("change", () => {
      const id = panelSelect.value;
      const p = (window.PANEL_TYPES || []).find((x) => x.id === id);
      if (p) panelNote.textContent = p.note;
    });
  }

  // ---- Live region announcements ----------------------------------------

  function announce(msg) {
    // Nudge aria-live by swapping text even when identical.
    liveStatus.textContent = "";
    setTimeout(() => {
      liveStatus.textContent = msg;
    }, 40);
  }

  // ---- Rendering --------------------------------------------------------

  const fmtSek = new Intl.NumberFormat("sv-SE");

  function renderSummary(rec) {
    const zoneInfo =
      electricityZones && electricityZones[rec.zone]
        ? electricityZones[rec.zone]
        : null;
    zoneChip.textContent = rec.zone;
    const zoneName = zoneInfo ? zoneInfo.name : null;
    if (rec.gridRateSource === "local-price-series") {
      const flatOre = Math.round(rec.gridRateSekPerKwh * 100);
      const dispOre =
        rec.solarDisplacementRateSekPerKwh != null
          ? Math.round(rec.solarDisplacementRateSekPerKwh * 100)
          : flatOre;
      const prefix = zoneName ? `${zoneName} · ` : "";
      zoneDetail.textContent =
        `${prefix}${flatOre} öre/kWh flat, ${dispOre} öre/kWh solar-weighted ` +
        `(2025 Lund hourly)`;
    } else if (zoneInfo) {
      zoneDetail.textContent = `${zoneInfo.name} · ~${zoneInfo.avg_ore_per_kwh} öre/kWh avg`;
    } else {
      zoneDetail.textContent = `grid rate ${(rec.gridRateSekPerKwh).toFixed(2)} SEK/kWh`;
    }

    if (rec.panel) {
      const e = rec.panelOutput;
      const effStr =
        rec.panel.efficiencyPct != null ? `${rec.panel.efficiencyPct}% eff` : "—";
      const autoStr = rec.autoPicked ? " (auto-selected)" : "";
      panelSummary.textContent = `${rec.panel.label}${autoStr} — ${e.installedKwp} kWp, ~${fmtSek.format(e.annualKwh)} kWh/yr, ${effStr}`;
    } else {
      panelSummary.textContent = "—";
    }

    const ir = rec.irradiance;
    const topSource = irradianceMeta.source || ir.source || "unknown source";
    const isFallback = /fallback|approximation|approximate/i.test(topSource);
    const sourceTag = isFallback ? `⚠ Approximated: ${topSource}` : `Source: ${topSource}`;
    if (ir.stationName) {
      irradianceSummary.textContent = `${ir.stationName} — ${ir.annualKwhPerM2} kWh/m²/yr (${ir.distanceKm} km) · ${sourceTag}`;
    } else {
      irradianceSummary.textContent = `${ir.annualKwhPerM2} kWh/m²/yr · ${sourceTag}`;
    }
    irradianceSummary.classList.toggle("fallback-warning", isFallback);

    if (flowSummary) {
      const p = rec.projection || {};
      const self = p.selfConsumedKwh ?? 0;
      const exp = p.exportedKwh ?? 0;
      const rev = p.annualSellRevenue ?? 0;
      const total = self + exp;
      if (total > 0) {
        const selfPct = Math.round((self / total) * 100);
        const expPct = Math.max(0, 100 - selfPct);
        const revStr = rev > 0
          ? ` · ${fmtSek.format(rev)} SEK/yr feed-in revenue`
          : " · not sold to grid";
        flowSummary.textContent =
          `${fmtSek.format(total)} kWh/yr produced — ` +
          `${fmtSek.format(self)} self-consumed (${selfPct}%), ` +
          `${fmtSek.format(exp)} exported (${expPct}%)${revStr}`;
      } else {
        flowSummary.textContent = "—";
      }
    }
  }

  function renderPayback(rec) {
    const annualSavings =
      rec.projection && Number.isFinite(rec.projection.annualSavings)
        ? rec.projection.annualSavings
        : null;

    if (rec.paybackYears == null) {
      paybackValue.classList.add("no-payback");
      paybackValue.innerHTML =
        "Doesn't pay back within 25 years<span class='unit'></span>";
      paybackLabel.textContent =
        "With these inputs, cumulative savings never catch up to the install cost.";

      if (annualSavings == null) {
        paybackSavings.hidden = true;
        paybackSavings.textContent = "";
      } else if (annualSavings >= 0) {
        paybackSavings.hidden = false;
        paybackSavings.classList.remove("is-negative");
        paybackSavings.textContent = `Net savings: ${fmtSek.format(
          annualSavings
        )} SEK/year over the install cost`;
      } else {
        paybackSavings.hidden = false;
        paybackSavings.classList.add("is-negative");
        paybackSavings.textContent = `Net loss: ${fmtSek.format(
          Math.abs(annualSavings)
        )} SEK/year with these inputs`;
      }
    } else {
      paybackValue.classList.remove("no-payback");
      paybackValue.innerHTML = `${rec.paybackYears}<span class="unit">years</span>`;
      paybackLabel.textContent = `years until cumulative savings equal the ${fmtSek.format(
        rec.installCost
      )} SEK install cost`;

      if (annualSavings == null) {
        paybackSavings.hidden = true;
        paybackSavings.textContent = "";
      } else {
        paybackSavings.hidden = false;
        paybackSavings.classList.remove("is-negative");
        paybackSavings.textContent = `~${fmtSek.format(
          annualSavings
        )} SEK/year in average savings`;
      }
    }
  }

  function renderMix({ solar, grid }) {
    const s = Math.round(solar * 100);
    const g = Math.max(0, 100 - s);

    segSolar.style.width = `${s}%`;
    segGrid.style.width = `${g}%`;
    pctSolar.textContent = `${s}%`;
    pctGrid.textContent = `${g}%`;

    document
      .getElementById("mix-bar")
      .setAttribute(
        "aria-label",
        `Recommended energy mix: ${s}% solar, ${g}% grid`
      );
  }

  function independenceTier(score) {
    if (score < 34) return "Low independence — mostly grid-reliant.";
    if (score < 67) return "Moderate independence — meaningful self-supply.";
    return "High independence — largely self-sufficient on paper.";
  }

  function renderMetrics({ coverage, independence }) {
    coverageValue.textContent = `${Math.round(coverage * 100)}%`;
    independenceValue.textContent = independence;
    independenceLabel.textContent = independenceTier(independence);
  }

  function renderTable(rows) {
    // Show years 1–10 in the table (projection spans 25 years).
    const first10 = rows.filter((r) => r.year >= 1 && r.year <= 10);
    costTableBody.innerHTML = first10
      .map(
        (r) =>
          `<tr><td>${r.year}</td><td>${fmtSek.format(r.gridOnly)}</td><td>${fmtSek.format(
            r.mixed
          )}</td></tr>`
      )
      .join("");
  }

  // ---- Submit handler ---------------------------------------------------

  form.addEventListener("submit", (e) => {
    e.preventDefault();

    const lat = Number(latInput.value);
    const lng = Number(lngInput.value);
    const usage = Number(document.getElementById("usage").value);
    const roofAreaM2 = Number(document.getElementById("roof-area").value);
    const budgetRaw = document.getElementById("budget").value;
    const budget = budgetRaw === "" ? undefined : Number(budgetRaw);
    const upkeepFraction = Number(upkeepInput.value) / 100;
    const wCost = Number(document.getElementById("w-cost").value);
    const wInd = Number(document.getElementById("w-independence").value);
    const wSus = Number(document.getElementById("w-sustainability").value);
    const panelId = panelSelect.value;
    const feedInOrePerKwh = Number(document.getElementById("feed-in-rate").value);

    // Validation
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) {
      announce("Please pick a location on the map first.");
      document.getElementById("map").scrollIntoView({ behavior: "smooth" });
      return;
    }
    if (!Number.isFinite(usage) || usage <= 0) {
      document.getElementById("usage").focus();
      return;
    }
    if (!Number.isFinite(roofAreaM2) || roofAreaM2 <= 0) {
      document.getElementById("roof-area").focus();
      return;
    }

    const rec = window.HappyDaysCalc.computeRecommendation({
      lat,
      lng,
      usage,
      budget,
      wCost,
      wIndependence: wInd,
      wSustainability: wSus,
      roofAreaM2,
      upkeepFraction,
      sellExcess: sellExcess.checked,
      feedInOrePerKwh,
      panelId,
      panels: window.PANEL_TYPES,
      stations: irradianceStations,
      zones: electricityZones,
      localPriceStats,
    });

    resultsLead.textContent = `Based on ${fmtSek.format(
      usage
    )} kWh/year at ${lat.toFixed(2)}, ${lng.toFixed(2)} — zone ${rec.zone}.`;

    renderSummary(rec);
    renderPayback(rec);
    renderMix(rec.mix);
    renderMetrics(rec.mix);
    renderTable(rec.projection.rows);

    results.hidden = false;
    results.setAttribute("tabindex", "-1");
    results.focus({ preventScroll: true });
    results.scrollIntoView({ behavior: "smooth", block: "start" });

    announce(
      rec.paybackYears != null
        ? `Results updated. Payback in ${rec.paybackYears} years.`
        : "Results updated. This configuration does not pay back within 25 years."
    );

    // Publish a compact snapshot for the AI adviser.
    const formSnapshot = {
      lat,
      lng,
      usage,
      roofAreaM2,
      budget: budget == null ? null : budget,
      upkeepPct: Number(upkeepInput.value),
      sellExcess: sellExcess.checked,
      feedInOrePerKwh,
      panelId,
      priorities: { cost: wCost, independence: wInd, sustainability: wSus },
    };
    const recSnapshot = {
      zone: rec.zone,
      gridRateSekPerKwh: rec.gridRateSekPerKwh,
      solarDisplacementRateSekPerKwh: rec.solarDisplacementRateSekPerKwh,
      gridRateSource: rec.gridRateSource,
      panel: rec.panel ? { id: rec.panel.id, label: rec.panel.label } : null,
      autoPicked: rec.autoPicked,
      installCost: rec.installCost,
      panelOutput: rec.panelOutput,
      irradiance: rec.irradiance,
      mix: {
        solar: Math.round(rec.mix.solar * 100),
        grid: Math.round(rec.mix.grid * 100),
        coverage: Math.round(rec.mix.coverage * 100),
        independence: rec.mix.independence,
      },
      paybackYears: rec.paybackYears,
      annualSavings: rec.projection ? rec.projection.annualSavings : null,
      selfConsumedKwh: rec.projection ? rec.projection.selfConsumedKwh : null,
      exportedKwh: rec.projection ? rec.projection.exportedKwh : null,
      annualSellRevenue: rec.projection ? rec.projection.annualSellRevenue : null,
    };
    if (window.HappyDaysAdviser) {
      window.HappyDaysAdviser.updateContext(recSnapshot, formSnapshot);
    }
  });

  // ---- Boot -------------------------------------------------------------

  function boot() {
    populatePanelSelect();
    populateCityFallback();
    bindLiveOutputs();

    // Set a sensible default panel-note (auto is selected on load).
    const def = (window.PANEL_TYPES || []).find((p) => p.id === "auto");
    if (def) panelNote.textContent = def.note;

    initMap();

    // Fire-and-forget: JSON fetch is best-effort.
    loadReferenceData();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();

/* ==========================================================================
 * AI adviser side panel (v3)
 *
 * Talks to POST /api/chat served by server/main.py. Parses SSE stream.
 * Keeps the latest recommendation + form snapshot in a module-level variable;
 * app.js above publishes to it via window.HappyDaysAdviser.updateContext.
 * ========================================================================== */
(function () {
  "use strict";

  const panel = document.getElementById("adviser-panel");
  const fab = document.getElementById("adviser-fab");
  const toggleBtn = document.getElementById("adviser-toggle");
  const toggleIcon = document.getElementById("adviser-toggle-icon");
  const form = document.getElementById("adviser-form");
  const textarea = document.getElementById("adviser-textarea");
  const sendBtn = document.getElementById("adviser-send");
  const messagesEl = document.getElementById("adviser-messages");
  const emptyEl = document.getElementById("adviser-empty");

  if (!panel || !form || !textarea) return;

  // Breakpoint shared with CSS.
  const DESKTOP_QUERY = window.matchMedia("(min-width: 900px)");

  // Chat state
  const history = [];      // [{role, content}]
  let latestRec = null;
  let latestForm = null;
  let streaming = false;

  // ---- Context API (called by the main form submit) -------------------
  window.HappyDaysAdviser = {
    updateContext(rec, formSnapshot) {
      latestRec = rec || null;
      latestForm = formSnapshot || null;
    },
  };

  // ---- Layout: desktop panel vs mobile FAB ----------------------------
  function syncLayout() {
    if (DESKTOP_QUERY.matches) {
      fab.hidden = true;
      panel.setAttribute("aria-hidden", "false");
      panel.style.display = "";
    } else {
      // On mobile: hide panel unless explicitly opened via FAB.
      if (!panel.dataset.mobileOpen) {
        panel.setAttribute("aria-hidden", "true");
      }
      fab.hidden = false;
    }
  }
  if (typeof DESKTOP_QUERY.addEventListener === "function") {
    DESKTOP_QUERY.addEventListener("change", syncLayout);
  } else if (typeof DESKTOP_QUERY.addListener === "function") {
    DESKTOP_QUERY.addListener(syncLayout);
  }

  // ---- Open/close on mobile -------------------------------------------
  fab.addEventListener("click", () => {
    panel.dataset.mobileOpen = "1";
    panel.setAttribute("aria-hidden", "false");
    // Add a close button to the header for mobile.
    ensureMobileCloseButton();
    setTimeout(() => textarea.focus(), 20);
  });

  function ensureMobileCloseButton() {
    if (toggleBtn.dataset.mobileClose === "1") return;
    toggleBtn.dataset.mobileClose = "1";
    toggleBtn.setAttribute("aria-label", "Close adviser panel");
    toggleIcon.innerHTML = "&times;";
    toggleBtn.addEventListener("click", mobileCloseHandler);
  }

  function mobileCloseHandler(e) {
    if (!DESKTOP_QUERY.matches) {
      e.preventDefault();
      e.stopImmediatePropagation();
      delete panel.dataset.mobileOpen;
      panel.setAttribute("aria-hidden", "true");
      fab.focus();
    }
  }

  // ---- Desktop collapse toggle ---------------------------------------
  toggleBtn.addEventListener("click", () => {
    if (!DESKTOP_QUERY.matches) return; // mobile handler covers this
    const collapsed = panel.classList.toggle("is-collapsed");
    document.body.classList.toggle("adviser-collapsed", collapsed);
    toggleBtn.setAttribute("aria-expanded", String(!collapsed));
    toggleBtn.setAttribute(
      "aria-label",
      collapsed ? "Expand adviser panel" : "Collapse adviser panel"
    );
    toggleIcon.innerHTML = collapsed ? "&#9664;" : "&#9654;";
    if (!collapsed) {
      setTimeout(() => textarea.focus(), 20);
    }
  });

  // ---- Textarea auto-grow and Enter-to-send --------------------------
  const LINE_HEIGHT = 22; // px, approximation for 6-row cap
  function autoGrow() {
    textarea.style.height = "auto";
    const max = LINE_HEIGHT * 6 + 20;
    textarea.style.height = Math.min(max, textarea.scrollHeight) + "px";
  }
  textarea.addEventListener("input", autoGrow);
  textarea.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      form.requestSubmit();
    }
  });

  // ---- Message rendering ---------------------------------------------
  function hideEmptyIfNeeded() {
    if (emptyEl && emptyEl.parentNode) {
      emptyEl.remove();
    }
  }

  function addMessage(role, text) {
    hideEmptyIfNeeded();
    const div = document.createElement("div");
    div.className = "adviser-msg " + (role === "user" ? "adviser-msg-user" : "adviser-msg-assistant");
    div.textContent = text;
    messagesEl.appendChild(div);
    scrollToBottom();
    return div;
  }

  function addTypingIndicator() {
    hideEmptyIfNeeded();
    const div = document.createElement("div");
    div.className = "adviser-msg-typing";
    div.textContent = "…";
    div.setAttribute("aria-label", "Adviser is typing");
    messagesEl.appendChild(div);
    scrollToBottom();
    return div;
  }

  function addErrorMessage(text, retryFn) {
    hideEmptyIfNeeded();
    const wrap = document.createElement("div");
    wrap.className = "adviser-msg-error";
    wrap.textContent = text;
    if (retryFn) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = "Försök igen";
      btn.addEventListener("click", () => {
        wrap.remove();
        retryFn();
      });
      wrap.appendChild(document.createElement("br"));
      wrap.appendChild(btn);
    }
    messagesEl.appendChild(wrap);
    scrollToBottom();
    return wrap;
  }

  function scrollToBottom() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  // ---- Streaming request ---------------------------------------------
  async function sendMessage(userText) {
    if (streaming || !userText) return;
    streaming = true;
    sendBtn.disabled = true;
    textarea.disabled = true;

    // Push user message to history + UI.
    history.push({ role: "user", content: userText });
    addMessage("user", userText);

    const typingEl = addTypingIndicator();
    let assistantEl = null;
    let assistantText = "";

    const payload = {
      messages: history.map((m) => ({ role: m.role, content: m.content })),
      context: {
        recommendation: latestRec,
        form: latestForm,
      },
    };

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      if (!res.ok) {
        let msg = `Server error (${res.status}).`;
        try {
          const data = await res.json();
          if (data && data.error) msg = data.error;
        } catch (_) {}
        typingEl.remove();
        addErrorMessage(msg, () => sendMessage(userText));
        // Roll back the user turn so retry still has the user message.
        return;
      }

      if (!res.body) {
        throw new Error("No response body");
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        let sep;
        while ((sep = buffer.indexOf("\n\n")) >= 0) {
          const rawEvent = buffer.slice(0, sep);
          buffer = buffer.slice(sep + 2);
          const line = rawEvent.split("\n").find((l) => l.startsWith("data:"));
          if (!line) continue;
          const payloadStr = line.slice(5).trim();
          if (payloadStr === "[DONE]") {
            continue;
          }
          let evt;
          try {
            evt = JSON.parse(payloadStr);
          } catch (_) {
            continue;
          }
          if (evt.error) {
            if (typingEl.parentNode) typingEl.remove();
            addErrorMessage(
              "Kunde inte svara — " + evt.error,
              () => sendMessage(userText)
            );
            streaming = false;
            sendBtn.disabled = false;
            textarea.disabled = false;
            textarea.focus();
            return;
          }
          if (typeof evt.t === "string") {
            if (!assistantEl) {
              if (typingEl.parentNode) typingEl.remove();
              assistantEl = addMessage("assistant", "");
            }
            assistantText += evt.t;
            assistantEl.textContent = assistantText;
            scrollToBottom();
          }
        }
      }

      if (typingEl.parentNode) typingEl.remove();
      if (assistantText) {
        history.push({ role: "assistant", content: assistantText });
      } else if (!assistantEl) {
        addErrorMessage(
          "Kunde inte svara — kontrollera att servern har ANTHROPIC_API_KEY satt",
          () => sendMessage(userText)
        );
      }
    } catch (err) {
      if (typingEl.parentNode) typingEl.remove();
      addErrorMessage(
        "Kunde inte svara — kontrollera att servern har ANTHROPIC_API_KEY satt",
        () => sendMessage(userText)
      );
    } finally {
      streaming = false;
      sendBtn.disabled = false;
      textarea.disabled = false;
      textarea.value = "";
      autoGrow();
      textarea.focus();
    }
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const txt = textarea.value.trim();
    if (!txt) return;
    sendMessage(txt);
  });

  // Initial layout sync.
  syncLayout();
})();

/* ==========================================================================
 * Live prices card — polls data/nordpool_live.json (written by
 * scraper/nordpool_live.py) and paints a traffic-light + 12 h line chart.
 * Self-hides when the file is missing so the UI stays clean without the
 * scraper running.
 * ========================================================================== */
(function () {
  "use strict";
  const FEED_URL = "data/nordpool_live.json";
  const REFRESH_MS = 60 * 1000;

  const section = document.getElementById("live-prices");
  if (!section) return;
  const zoneEl = document.getElementById("lp-zone");
  const dotEl = document.getElementById("lp-dot");
  const metaEl = document.getElementById("lp-meta");
  const priceEl = document.getElementById("lp-price");
  const hintEl = document.getElementById("lp-hint");
  const chartEl = document.getElementById("lp-chart");

  const SVGNS = "http://www.w3.org/2000/svg";
  const fmtOre = new Intl.NumberFormat("sv-SE", { maximumFractionDigits: 1 });

  function el(name, attrs, text) {
    const n = document.createElementNS(SVGNS, name);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    if (text != null) n.textContent = text;
    return n;
  }

  function renderChart(rows, stats, currentHourStart) {
    while (chartEl.firstChild) chartEl.removeChild(chartEl.firstChild);
    // Keep the <title> for a11y.
    chartEl.appendChild(el("title", { id: "lp-chart-title" }, "Next 12 hours of electricity prices"));
    if (!rows || rows.length === 0) return;

    const W = 640, H = 180;
    const padL = 40, padR = 12, padT = 14, padB = 28;
    const innerW = W - padL - padR;
    const innerH = H - padT - padB;
    const prices = rows.map((r) => r.price_ore);
    const minY = Math.min(...prices, stats && stats.min_ore != null ? stats.min_ore : prices[0]);
    const maxY = Math.max(...prices, stats && stats.max_ore != null ? stats.max_ore : prices[0]);
    const span = Math.max(1, maxY - minY);
    // Pad y-axis a touch so dots don't clip the top/bottom.
    const niceMin = Math.max(0, minY - span * 0.1);
    const niceMax = maxY + span * 0.1;

    const xAt = (i) => padL + (innerW * i) / Math.max(1, rows.length - 1);
    const yAt = (v) => padT + innerH - ((v - niceMin) / (niceMax - niceMin)) * innerH;

    // Horizontal gridlines at 0, 50, 100, 150, 200 öre if in range.
    const gridVals = [];
    const step = niceMax - niceMin > 200 ? 100 : 50;
    for (let v = Math.ceil(niceMin / step) * step; v <= niceMax; v += step) gridVals.push(v);
    for (const v of gridVals) {
      chartEl.appendChild(el("line", {
        class: "lp-grid",
        x1: padL, x2: W - padR, y1: yAt(v).toFixed(1), y2: yAt(v).toFixed(1),
      }));
      chartEl.appendChild(el("text", {
        x: padL - 6, y: (yAt(v) + 3.5).toFixed(1), "text-anchor": "end",
      }, String(Math.round(v))));
    }

    // Axes.
    chartEl.appendChild(el("line", { class: "lp-axis", x1: padL, x2: padL, y1: padT, y2: H - padB }));
    chartEl.appendChild(el("line", { class: "lp-axis", x1: padL, x2: W - padR, y1: H - padB, y2: H - padB }));

    // Area fill under the line.
    const areaPts = rows
      .map((r, i) => `${xAt(i).toFixed(1)},${yAt(r.price_ore).toFixed(1)}`)
      .join(" ");
    chartEl.appendChild(el("polygon", {
      class: "lp-fill",
      points: `${padL},${H - padB} ${areaPts} ${W - padR},${H - padB}`,
    }));

    // Line.
    const linePath = rows
      .map((r, i) => `${i === 0 ? "M" : "L"}${xAt(i).toFixed(1)},${yAt(r.price_ore).toFixed(1)}`)
      .join(" ");
    chartEl.appendChild(el("path", { class: "lp-line", d: linePath }));

    // Points (tier-colored), with current hour highlighted.
    for (let i = 0; i < rows.length; i++) {
      const r = rows[i];
      const isCurrent = currentHourStart && r.hour_start === currentHourStart;
      chartEl.appendChild(el("circle", {
        class: "lp-point" + (isCurrent ? " is-current" : ""),
        "data-tier": r.tier,
        cx: xAt(i).toFixed(1),
        cy: yAt(r.price_ore).toFixed(1),
        r: 4,
      }));
    }

    // X labels every other hour + last.
    for (let i = 0; i < rows.length; i++) {
      if (i % 2 !== 0 && i !== rows.length - 1) continue;
      const hh = new Date(rows[i].hour_start).getHours();
      chartEl.appendChild(el("text", {
        x: xAt(i).toFixed(1), y: H - padB + 14, "text-anchor": "middle",
      }, `${String(hh).padStart(2, "0")}`));
    }
  }

  function cheapestAhead(rows) {
    if (!rows || rows.length < 2) return null;
    // Skip the current hour; find the cheapest of the remaining.
    let best = null;
    for (let i = 1; i < rows.length; i++) {
      if (!best || rows[i].price_ore < best.price_ore) best = rows[i];
    }
    return best;
  }

  function render(data) {
    if (!data || !data.next_12h || data.next_12h.length === 0) {
      section.hidden = true;
      return;
    }
    section.hidden = false;
    zoneEl.textContent = data.zone || "SE?";
    const cur = data.current;
    const rows = data.next_12h;
    const curHour = cur ? cur.hour_start : null;

    section.classList.toggle("is-stale", Boolean(data.stale));
    dotEl.setAttribute("data-tier", cur ? cur.tier : "");

    if (cur) {
      priceEl.textContent = `${fmtOre.format(cur.price_ore)} öre/kWh`;
      const cheap = cheapestAhead(rows);
      if (cheap && cheap.hour_start !== curHour) {
        const hh = new Date(cheap.hour_start).getHours();
        hintEl.textContent =
          `Cheapest in next 12 h: ${String(hh).padStart(2, "0")}:00 ` +
          `at ~${fmtOre.format(cheap.price_ore)} öre/kWh`;
      } else {
        hintEl.textContent = "";
      }
    } else {
      priceEl.textContent = "—";
      hintEl.textContent = "";
    }

    const updated = data.generated_at ? new Date(data.generated_at) : null;
    const src = data.source || "";
    metaEl.textContent = updated
      ? `Updated ${updated.toLocaleTimeString("sv-SE", { hour: "2-digit", minute: "2-digit" })}` +
        (data.stale ? " · data may be stale" : "") +
        (src ? ` · ${src}` : "")
      : src;

    renderChart(rows, data.window_stats, curHour);
  }

  async function load() {
    try {
      const res = await fetch(FEED_URL, { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      render(data);
    } catch (e) {
      // Scraper isn't running or JSON missing — hide the card quietly.
      section.hidden = true;
    }
  }

  load();
  setInterval(load, REFRESH_MS);
})();
