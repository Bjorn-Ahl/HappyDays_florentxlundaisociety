/*
 * HappyDays — v1 advisor logic (frontend-only)
 *
 * NOTE: All calculations in this file are PLACEHOLDERS. They are
 * deliberately simple heuristics that react plausibly to input changes.
 * They are intended to be replaced once the SMHI irradiance/wind data
 * scraping pipeline is wired in, and once proper cost/subsidy tables
 * (e.g. grid tariffs per elområde, green deduction) are sourced.
 *
 * Assumptions baked in (TODO: replace):
 *   - Flat Swedish grid electricity price ~2.20 SEK/kWh
 *   - Flat on-site generation cost ~0.40 SEK/kWh (amortised)
 *   - One-off install cost ~18 SEK per annual kWh offset by solar
 *   - No inflation, no subsidies, no resale of surplus, no battery
 *   - Rooftop wind capped low — uncommon in SE residential contexts
 */

(function () {
  "use strict";

  // --- Constants (placeholders) ---
  const GRID_RATE_SEK_PER_KWH = 2.2;
  const ONSITE_RATE_SEK_PER_KWH = 0.4;
  const INSTALL_COST_PER_ANNUAL_KWH = 18;
  const PROJECTION_YEARS = 10;

  // --- DOM refs ---
  const form = document.getElementById("advisor-form");
  const results = document.getElementById("results");
  const resultsLead = document.getElementById("results-lead");

  const segSolar = document.getElementById("seg-solar");
  const segWind = document.getElementById("seg-wind");
  const segGrid = document.getElementById("seg-grid");
  const pctSolar = document.getElementById("pct-solar");
  const pctWind = document.getElementById("pct-wind");
  const pctGrid = document.getElementById("pct-grid");

  const coverageValue = document.getElementById("coverage-value");
  const independenceValue = document.getElementById("independence-value");
  const independenceLabel = document.getElementById("independence-label");

  const costChart = document.getElementById("cost-chart");
  const costTableBody = document.querySelector("#cost-table tbody");

  // --- Live slider output binding ---
  ["w-cost", "w-independence", "w-sustainability"].forEach((id) => {
    const input = document.getElementById(id);

    const out = document.getElementById(`${id}-out`);
    if (!input || !out) return;
    input.addEventListener("input", () => {
      out.textContent = input.value;
    });
  });

  // --- Main calculator ---
  function computeRecommendation({
    usage,
    budget,
    wCost,
    wIndependence,
    wSustainability,
  }) {
    // Normalise priority weights
    const total = wCost + wIndependence + wSustainability;
    const nCost = wCost / total;
    const nInd = wIndependence / total;
    const nSus = wSustainability / total;

    // Solar share: boosted by independence + sustainability,
    // dampened by cost priority (especially when budget is tight/absent).
    let solarShare = 0.55 * nSus + 0.45 * nInd - 0.35 * nCost;
    // Base floor so solar always shows up as *some* option
    solarShare = Math.max(0.1, Math.min(0.85, solarShare + 0.3));

    // Budget dampener: if budget is provided and small relative to
    // the implied install cost, reduce solar share.
    if (typeof budget === "number" && budget > 0) {
      // Implied install cost if we were 100% solar-offset:
      const maxInstallCost = usage * INSTALL_COST_PER_ANNUAL_KWH;
      const affordRatio = budget / maxInstallCost; // 1.0 = can fully afford
      if (affordRatio < 1) {
        // scale solar down, but never below 10%
        solarShare = Math.max(0.1, solarShare * Math.max(0.4, affordRatio));
      }
    }

    // Wind: small by default. Slight boost from sustainability + independence.
    let windShare = 0.03 + 0.07 * (nSus + nInd) * 0.5;
    windShare = Math.max(0.02, Math.min(0.12, windShare));

    // Cap solar+wind so grid is at least 5%
    if (solarShare + windShare > 0.95) {
      const scale = 0.95 / (solarShare + windShare);
      solarShare *= scale;
      windShare *= scale;
    }

    const gridShare = Math.max(0.05, 1 - solarShare - windShare);

    // Normalise tiny rounding drift to sum to 1
    const sum = solarShare + windShare + gridShare;
    const solar = solarShare / sum;
    const wind = windShare / sum;
    const grid = gridShare / sum;

    // Coverage = on-site generation share of usage
    const coverage = solar + wind;

    // Independence score: on-site minus grid, mapped to 0-100
    // (solar+wind) ranges roughly 0.12 -> 0.95; centre around 55.
    const rawIndependence = (solar + wind) * 100 - grid * 20;
    const independence = Math.max(0, Math.min(100, Math.round(rawIndependence)));

    return { solar, wind, grid, coverage, independence };
  }

  function independenceTier(score) {
    if (score < 34) return "Low independence — mostly grid-reliant.";
    if (score < 67) return "Moderate independence — meaningful self-supply.";
    return "High independence — largely self-sufficient on paper.";
  }

  // --- Cost projection ---
  function projectCost({ usage, solar, wind }) {
    const onsiteShare = solar + wind;
    const gridShare = 1 - onsiteShare;

    // Upfront install cost scales with how much annual kWh we're offsetting on-site
    const installCost = usage * onsiteShare * INSTALL_COST_PER_ANNUAL_KWH;

    const gridOnlyAnnual = usage * GRID_RATE_SEK_PER_KWH;
    const mixedAnnual =
      usage * gridShare * GRID_RATE_SEK_PER_KWH +
      usage * onsiteShare * ONSITE_RATE_SEK_PER_KWH;

    const rows = [];
    let gridOnlyCum = 0;
    let mixedCum = installCost;

    for (let year = 1; year <= PROJECTION_YEARS; year++) {
      gridOnlyCum += gridOnlyAnnual;
      mixedCum += mixedAnnual;
      rows.push({
        year,
        gridOnly: Math.round(gridOnlyCum),
        mixed: Math.round(mixedCum),
      });
    }
    return rows;
  }

  // --- Rendering ---
  function renderMix({ solar, wind, grid }) {
    const s = Math.round(solar * 100);
    const w = Math.round(wind * 100);
    // Absorb rounding error in grid so percentages sum to 100
    const g = Math.max(0, 100 - s - w);

    segSolar.style.width = `${s}%`;
    segWind.style.width = `${w}%`;
    segGrid.style.width = `${g}%`;

    pctSolar.textContent = `${s}%`;
    pctWind.textContent = `${w}%`;
    pctGrid.textContent = `${g}%`;

    const bar = document.getElementById("mix-bar");
    bar.setAttribute(
      "aria-label",
      `Recommended energy mix: ${s}% solar, ${w}% wind, ${g}% grid`
    );
  }

  function renderMetrics({ coverage, independence }) {
    coverageValue.textContent = `${Math.round(coverage * 100)}%`;
    independenceValue.textContent = independence;
    independenceLabel.textContent = independenceTier(independence);
  }

  function renderTable(rows) {
    const fmt = new Intl.NumberFormat("sv-SE");
    costTableBody.innerHTML = rows
      .map(
        (r) =>
          `<tr><td>${r.year}</td><td>${fmt.format(r.gridOnly)}</td><td>${fmt.format(
            r.mixed
          )}</td></tr>`
      )
      .join("");
  }

  function renderChart(rows) {
    // Inline SVG line chart, no dependencies.
    const W = 600;
    const H = 260;
    const padL = 56;
    const padR = 16;
    const padT = 16;
    const padB = 36;
    const innerW = W - padL - padR;
    const innerH = H - padT - padB;

    const maxY = Math.max(
      ...rows.map((r) => Math.max(r.gridOnly, r.mixed)),
      1
    );
    // Round max up to a nice number for axis readability
    const niceMax = niceCeil(maxY);

    const xAt = (i) =>
      padL + (innerW * i) / Math.max(1, rows.length - 1);
    const yAt = (v) => padT + innerH - (v / niceMax) * innerH;

    const toPath = (key) =>
      rows
        .map((r, i) => `${i === 0 ? "M" : "L"}${xAt(i).toFixed(1)},${yAt(r[key]).toFixed(1)}`)
        .join(" ");

    const gridPath = toPath("gridOnly");
    const mixedPath = toPath("mixed");

    const fmt = new Intl.NumberFormat("sv-SE", { notation: "compact" });

    // Y-axis ticks: 0, 1/2, max
    const ticks = [0, niceMax / 2, niceMax];
    const tickLines = ticks
      .map(
        (t) =>
          `<line x1="${padL}" y1="${yAt(t).toFixed(
            1
          )}" x2="${W - padR}" y2="${yAt(t).toFixed(
            1
          )}" stroke="#e3ebe8" stroke-width="1" />`
      )
      .join("");
    const tickLabels = ticks
      .map(
        (t) =>
          `<text x="${padL - 8}" y="${(yAt(t) + 4).toFixed(
            1
          )}" text-anchor="end" font-size="11" fill="#5a6b70">${fmt.format(
            Math.round(t)
          )}</text>`
      )
      .join("");

    // X-axis year labels (every 2 years + last)
    const xLabels = rows
      .map((r, i) => {
        if (i % 2 !== 0 && i !== rows.length - 1) return "";
        return `<text x="${xAt(i).toFixed(
          1
        )}" y="${H - padB + 18}" text-anchor="middle" font-size="11" fill="#5a6b70">Y${r.year}</text>`;
      })
      .join("");

    // Legend (simple, inside SVG)
    const legend = `
      <g transform="translate(${padL + 8}, ${padT + 8})">
        <rect width="170" height="40" fill="#ffffff" fill-opacity="0.85" rx="6" />
        <line x1="10" y1="14" x2="34" y2="14" stroke="#9aa6a9" stroke-width="3" />
        <text x="42" y="18" font-size="12" fill="#1b2a2e">Grid only</text>
        <line x1="10" y1="30" x2="34" y2="30" stroke="#2f6b53" stroke-width="3" />
        <text x="42" y="34" font-size="12" fill="#1b2a2e">Recommended</text>
      </g>`;

    costChart.innerHTML = `
      ${tickLines}
      ${tickLabels}
      ${xLabels}
      <line x1="${padL}" y1="${padT}" x2="${padL}" y2="${H - padB}" stroke="#c9d3d0" stroke-width="1" />
      <line x1="${padL}" y1="${H - padB}" x2="${W - padR}" y2="${H - padB}" stroke="#c9d3d0" stroke-width="1" />
      <path d="${gridPath}" fill="none" stroke="#9aa6a9" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round" />
      <path d="${mixedPath}" fill="none" stroke="#2f6b53" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round" />
      ${legend}
    `;
  }

  function niceCeil(n) {
    if (n <= 0) return 1;
    const pow = Math.pow(10, Math.floor(Math.log10(n)));
    const base = n / pow;
    let nice;
    if (base <= 1) nice = 1;
    else if (base <= 2) nice = 2;
    else if (base <= 2.5) nice = 2.5;
    else if (base <= 5) nice = 5;
    else nice = 10;
    return nice * pow;
  }

  // --- Submit handler ---
  form.addEventListener("submit", (e) => {
    e.preventDefault();

    const location = (document.getElementById("location").value || "").trim();
    const usageRaw = document.getElementById("usage").value;
    const budgetRaw = document.getElementById("budget").value;

    const usage = Number(usageRaw);
    const budget = budgetRaw === "" ? null : Number(budgetRaw);

    if (!location) {
      document.getElementById("location").focus();
      return;
    }
    if (!Number.isFinite(usage) || usage <= 0) {
      document.getElementById("usage").focus();
      return;
    }

    const wCost = Number(document.getElementById("w-cost").value);
    const wInd = Number(document.getElementById("w-independence").value);
    const wSus = Number(document.getElementById("w-sustainability").value);

    const rec = computeRecommendation({
      usage,
      budget: budget ?? undefined,
      wCost,
      wIndependence: wInd,
      wSustainability: wSus,
    });

    const rows = projectCost({ usage, solar: rec.solar, wind: rec.wind });

    resultsLead.textContent = `Based on ${new Intl.NumberFormat("sv-SE").format(
      usage
    )} kWh/year in ${location}, here is a plausible starting point.`;

    renderMix(rec);
    renderMetrics(rec);
    renderTable(rows);
    renderChart(rows);

    results.hidden = false;
    // Move focus to results for screen-reader users, and scroll into view.
    results.setAttribute("tabindex", "-1");
    results.focus({ preventScroll: true });
    results.scrollIntoView({ behavior: "smooth", block: "start" });
  });
})();
