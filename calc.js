/*
 * HappyDays — pure calculation module.
 *
 * Pure functions only: no DOM, no globals, no side effects beyond reading
 * the injected `irradianceStations` argument. Input = plain object, output
 * = plain object.
 *
 * Exposes window.HappyDaysCalc with:
 *   - priceZoneFor(lat)
 *   - irradianceFor(lat, lng, stations?)
 *   - pickBestPanelForBudget(opts)
 *   - computePayback({ installCost, annualSavingsSek })
 *   - projectCost(opts)
 *   - computeRecommendation(opts)
 *
 * Assumptions (see README for rationale):
 *   - Panel DC:AC + soiling + orientation losses rolled into PERFORMANCE_RATIO
 *   - Roof covered by panels at 70% packing ratio (inverters, gaps, edges)
 *   - Upkeep is a % of install cost per year, applied from year 1
 *   - Grid price is a flat zone average (no ToU, no transmission fees)
 *   - Feed-in sale price is user-specified in öre/kWh
 */
(function (global) {
  "use strict";

  // Default grid rate fallback if zones JSON hasn't loaded yet.
  const DEFAULT_GRID_RATE_SEK_PER_KWH = 0.95;
  // 25 years is a standard panel warranty horizon; the break-even chart spans it.
  const DEFAULT_PROJECTION_YEARS = 25;
  // DC-to-AC/system losses: 80% is a common rule-of-thumb for Swedish rooftops.
  const PERFORMANCE_RATIO = 0.80;
  // Fraction of the user's reported roof area that actually ends up covered
  // by modules — you lose space to walkways, inverters, chimneys, tilt gaps.
  const PANEL_PACKING_RATIO = 0.70;
  // Standard Test Conditions irradiance (W/m^2). We use this to convert
  // "panel efficiency %" into "Wp per m^2".
  const STC_IRRADIANCE = 1000;

  // --- Geography helpers ---------------------------------------------------

  /**
   * Haversine distance in km between two lat/lng pairs.
   */
  function haversineKm(lat1, lng1, lat2, lng2) {
    const R = 6371;
    const toRad = (d) => (d * Math.PI) / 180;
    const dLat = toRad(lat2 - lat1);
    const dLng = toRad(lng2 - lng1);
    const a =
      Math.sin(dLat / 2) ** 2 +
      Math.cos(toRad(lat1)) *
        Math.cos(toRad(lat2)) *
        Math.sin(dLng / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(a));
  }

  /**
   * Assign a Swedish electricity bidding zone from latitude alone.
   * Document note: real boundaries follow grid topology, not parallels.
   */
  function priceZoneFor(lat) {
    if (!Number.isFinite(lat)) return "SE3";
    if (lat >= 65) return "SE1";
    if (lat >= 61) return "SE2";
    if (lat >= 57) return "SE3";
    return "SE4";
  }

  /**
   * Latitude-zoned fallback for annual kWh/m^2/year when no station data
   * has been loaded.
   */
  function latitudeFallbackIrradiance(lat) {
    if (lat >= 65) return 900;
    if (lat >= 60) return 950;
    if (lat >= 58) return 1000;
    return 1050;
  }

  /**
   * Nearest-station lookup. Returns { stationId, stationName, annualKwhPerM2, distanceKm, source }.
   */
  function irradianceFor(lat, lng, stations) {
    if (!Array.isArray(stations) || stations.length === 0) {
      return {
        stationId: null,
        stationName: null,
        annualKwhPerM2: latitudeFallbackIrradiance(lat),
        distanceKm: null,
        source: "latitude-zoned fallback",
      };
    }
    let best = null;
    let bestDist = Infinity;
    for (const s of stations) {
      if (!Number.isFinite(s.lat) || !Number.isFinite(s.lng)) continue;
      const d = haversineKm(lat, lng, s.lat, s.lng);
      if (d < bestDist) {
        bestDist = d;
        best = s;
      }
    }
    if (!best) {
      return {
        stationId: null,
        stationName: null,
        annualKwhPerM2: latitudeFallbackIrradiance(lat),
        distanceKm: null,
        source: "latitude-zoned fallback",
      };
    }
    return {
      stationId: best.id,
      stationName: best.name,
      annualKwhPerM2: Number(best.annual_kwh_per_m2),
      distanceKm: Math.round(bestDist * 10) / 10,
      source: "SMHI nearest station",
    };
  }

  // --- Panel sizing --------------------------------------------------------

  /**
   * For a single panel chemistry: how many annual kWh can we get out of
   * `roofAreaM2` at this site's `irradianceKwhM2`, given the chemistry's
   * efficiency and de-rating factors?
   *
   *   installedWp  = activeArea[m^2] * efficiencyFraction * STC[W/m^2]
   *   annualKwh    = installedWp/1000 * irradianceKwhM2 * performanceRatio
   *   installCost  = installedWp * sekPerW
   */
  function estimatePanelOutput(panel, roofAreaM2, irradianceKwhM2) {
    const activeArea = Math.max(0, roofAreaM2) * PANEL_PACKING_RATIO;
    const efficiency = (panel.efficiencyPct || 0) / 100;
    const installedWp = activeArea * efficiency * STC_IRRADIANCE;
    const installedKwp = installedWp / 1000;
    const annualKwh = installedKwp * irradianceKwhM2 * PERFORMANCE_RATIO;
    const installCost = installedWp * (panel.sekPerW || 0);
    return {
      installedWp: Math.round(installedWp),
      installedKwp: Math.round(installedKwp * 10) / 10,
      annualKwh: Math.round(annualKwh),
      installCost: Math.round(installCost),
    };
  }

  /**
   * Auto mode: choose the panel (excluding "auto") that maximises annual
   * kWh *within budget*. If no panel fits the budget, pick the cheapest.
   * If no budget is provided, pick the highest-output panel outright.
   */
  function pickBestPanelForBudget({ panels, roofAreaM2, irradianceKwhM2, budget }) {
    const real = (panels || []).filter((p) => p.id !== "auto");
    if (real.length === 0) return null;

    const scored = real.map((p) => ({
      panel: p,
      ...estimatePanelOutput(p, roofAreaM2, irradianceKwhM2),
    }));

    if (typeof budget === "number" && budget > 0) {
      const affordable = scored.filter((s) => s.installCost <= budget);
      if (affordable.length > 0) {
        affordable.sort((a, b) => b.annualKwh - a.annualKwh);
        return affordable[0];
      }
      // No panel fits — recommend the cheapest (and let the UI note it's over budget).
      scored.sort((a, b) => a.installCost - b.installCost);
      return scored[0];
    }

    scored.sort((a, b) => b.annualKwh - a.annualKwh);
    return scored[0];
  }

  // --- Priorities & mix ----------------------------------------------------

  /**
   * Compute the recommended solar-vs-grid split. On-site coverage is solar
   * only (wind removed in v3). Grid share is the residual with a minimum
   * floor of 5% — we always assume at least some grid tie-in.
   *
   * Inputs:
   *   usage, budget?, wCost, wIndependence, wSustainability,
   *   solarPotentialKwh  — how much solar the roof+panel combo can actually yield
   *   installCost        — upfront SEK for that solar solution
   *
   * Output shape: { solar, grid, coverage, independence, roofCoverage }
   *   solar + grid === 1 (within rounding); coverage === solar.
   */
  function computeMix({
    usage,
    budget,
    wCost,
    wIndependence,
    wSustainability,
    solarPotentialKwh,
    installCost,
  }) {
    const total = wCost + wIndependence + wSustainability;
    const nCost = total > 0 ? wCost / total : 1 / 3;
    const nInd = total > 0 ? wIndependence / total : 1 / 3;
    const nSus = total > 0 ? wSustainability / total : 1 / 3;

    // Fraction of annual usage the roof could physically cover with solar
    // (cannot exceed 1 — any surplus is exported).
    const roofCoverage = usage > 0 ? Math.min(1, solarPotentialKwh / usage) : 0;

    // Desired solar share driven by priorities, capped by roofCoverage.
    // Sustainability + independence push solar up; cost pulls it down.
    let desiredSolar = 0.30 + 0.55 * nSus + 0.45 * nInd - 0.35 * nCost;
    desiredSolar = Math.max(0.05, Math.min(0.85, desiredSolar));

    let solarShare = Math.min(desiredSolar, roofCoverage);

    // Budget dampener — scale solar if install cost exceeds the given budget.
    if (typeof budget === "number" && budget > 0 && installCost > budget && installCost > 0) {
      const affordRatio = budget / installCost;
      solarShare = solarShare * Math.max(0.3, affordRatio);
    }

    // --- Grid floor: force grid >= 5%. Solar is the residual. ---------
    const MIN_GRID_SHARE = 0.05;
    const MAX_SOLAR_SHARE = 1 - MIN_GRID_SHARE;
    if (solarShare > MAX_SOLAR_SHARE) {
      solarShare = MAX_SOLAR_SHARE;
    }
    const gridShare = Math.max(MIN_GRID_SHARE, 1 - solarShare);

    // Coverage = on-site generation share of usage (solar only in v3).
    const coverage = solarShare;

    // Independence score: on-site heavy, grid reliant penalised.
    const rawIndependence = coverage * 100 - gridShare * 20;
    const independence = Math.max(
      0,
      Math.min(100, Math.round(rawIndependence))
    );

    return {
      solar: solarShare,
      grid: gridShare,
      coverage,
      independence,
      roofCoverage,
    };
  }

  // --- Cost projection -----------------------------------------------------

  /**
   * Year-by-year cumulative cost projection (SEK).
   *
   * Grid-only line: usage * gridRate per year, no upfront.
   * Solar line:
   *    year 0 cash outlay = installCost
   *    each year:
   *      - buy `gridShare * usage` from the grid at gridRate
   *      - use `onsiteShare * usage` from solar (on-site rate ~ 0)
   *      - pay upkeep = upkeepFraction * installCost
   *      - sell surplus (produced − self-consumed) at feedInOrePerKwh if enabled
   */
  function projectCost({
    usage,
    solar,
    installCost,
    solarPotentialKwh,
    gridRateSekPerKwh,
    upkeepFraction,
    sellExcess,
    feedInOrePerKwh,
    years,
  }) {
    const nYears = years || DEFAULT_PROJECTION_YEARS;
    const onsiteShare = solar;
    const gridShare = Math.max(0, 1 - onsiteShare);
    const rate = Number(gridRateSekPerKwh) || DEFAULT_GRID_RATE_SEK_PER_KWH;
    const upkeep = Math.max(0, Number(upkeepFraction) || 0);
    const feedIn = sellExcess
      ? (Number(feedInOrePerKwh) || 0) / 100 // öre/kWh -> SEK/kWh
      : 0;

    // On-site kWh actually consumed per year.
    const selfConsumedKwh = usage * onsiteShare;
    // Surplus kWh available for export (solar production minus self-consumption).
    const surplusKwh = Math.max(0, solarPotentialKwh - usage * solar);
    const annualSellRevenue = sellExcess ? surplusKwh * feedIn : 0;

    const gridOnlyAnnual = usage * rate;
    const mixedAnnualOutflow =
      usage * gridShare * rate + installCost * upkeep - annualSellRevenue;

    const rows = [];
    let gridOnlyCum = 0;
    let mixedCum = installCost; // year 0 outlay
    rows.push({ year: 0, gridOnly: 0, mixed: Math.round(installCost) });
    for (let year = 1; year <= nYears; year++) {
      gridOnlyCum += gridOnlyAnnual;
      mixedCum += mixedAnnualOutflow;
      rows.push({
        year,
        gridOnly: Math.round(gridOnlyCum),
        mixed: Math.round(mixedCum),
      });
    }

    return {
      rows,
      annualGridOnlyCost: Math.round(gridOnlyAnnual),
      annualMixedOutflow: Math.round(mixedAnnualOutflow),
      annualSavings: Math.round(gridOnlyAnnual - mixedAnnualOutflow),
      selfConsumedKwh: Math.round(selfConsumedKwh),
      surplusKwh: Math.round(surplusKwh),
      annualSellRevenue: Math.round(annualSellRevenue),
    };
  }

  /**
   * Given cumulative-cost rows, find the crossover year (where mixed becomes
   * cheaper than grid-only). Linearly interpolate between the two bracketing
   * years for a nicer "7.4 years" number.
   */
  function computePayback(rows) {
    if (!Array.isArray(rows) || rows.length < 2) return null;
    for (let i = 1; i < rows.length; i++) {
      const prev = rows[i - 1];
      const cur = rows[i];
      const prevDelta = prev.gridOnly - prev.mixed; // >0 when solar ahead
      const curDelta = cur.gridOnly - cur.mixed;
      if (prevDelta < 0 && curDelta >= 0) {
        // Linear crossover between (prev.year, prevDelta) and (cur.year, curDelta)
        const t = -prevDelta / (curDelta - prevDelta);
        const paybackYear = prev.year + t * (cur.year - prev.year);
        return Math.round(paybackYear * 10) / 10;
      }
    }
    return null; // Never pays back inside the projection window.
  }

  // --- Top-level recommendation -------------------------------------------

  /**
   * One-shot recommendation. Ties everything together so the UI can call a
   * single function with a plain input object.
   */
  function computeRecommendation(input) {
    const {
      lat,
      lng,
      usage,
      budget,
      wCost,
      wIndependence,
      wSustainability,
      roofAreaM2,
      upkeepFraction,
      sellExcess,
      feedInOrePerKwh,
      panelId,
      panels,
      stations,
      zones,
    } = input;

    const zone = priceZoneFor(lat);
    const irradiance = irradianceFor(lat, lng, stations);
    const irradianceKwhM2 = irradiance.annualKwhPerM2;

    // Pick the panel (auto or explicit).
    let chosenPanel;
    let autoPicked = false;
    if (panelId === "auto" || !panelId) {
      const best = pickBestPanelForBudget({
        panels,
        roofAreaM2,
        irradianceKwhM2,
        budget,
      });
      chosenPanel = best ? best.panel : null;
      autoPicked = true;
    } else {
      chosenPanel = (panels || []).find((p) => p.id === panelId) || null;
    }
    if (!chosenPanel) {
      // Safety net: bail to monocrystalline.
      chosenPanel = (panels || []).find((p) => p.id === "mono") || null;
    }

    const panelOutput = chosenPanel
      ? estimatePanelOutput(chosenPanel, roofAreaM2, irradianceKwhM2)
      : { installedWp: 0, installedKwp: 0, annualKwh: 0, installCost: 0 };

    const gridRateSekPerKwh =
      zones && zones[zone] && Number.isFinite(zones[zone].avg_ore_per_kwh)
        ? zones[zone].avg_ore_per_kwh / 100
        : DEFAULT_GRID_RATE_SEK_PER_KWH;

    const mix = computeMix({
      usage,
      budget,
      wCost,
      wIndependence,
      wSustainability,
      solarPotentialKwh: panelOutput.annualKwh,
      installCost: panelOutput.installCost,
    });

    // Actual install cost scales with how much panel capacity we need.
    // If the roof could produce more than the mix asks for (roofCoverage >
    // mix.solar), we only buy a partial install. If the mix wants more solar
    // than the roof can deliver, the mix is clamped to roofCoverage and we
    // buy the full install.
    //
    //   roofCoverage = panelOutput.annualKwh / usage (capped at 1)
    //   mix.solar    = fraction of usage actually met by solar (<= roofCoverage)
    //   installScale = mix.solar / roofCoverage   (0..1)
    let installScale = 0;
    if (mix.roofCoverage > 0) {
      installScale = Math.min(1, mix.solar / mix.roofCoverage);
    }
    const scaledInstallCost = Math.round(panelOutput.installCost * installScale);
    const scaledSolarKwh = Math.round(panelOutput.annualKwh * installScale);

    const projection = projectCost({
      usage,
      solar: mix.solar,
      installCost: scaledInstallCost,
      solarPotentialKwh: scaledSolarKwh,
      gridRateSekPerKwh,
      upkeepFraction,
      sellExcess,
      feedInOrePerKwh,
    });

    const paybackYears = computePayback(projection.rows);

    return {
      zone,
      gridRateSekPerKwh,
      irradiance,
      panel: chosenPanel,
      autoPicked,
      panelOutput,
      installCost: scaledInstallCost,
      mix,
      projection,
      paybackYears,
    };
  }

  global.HappyDaysCalc = {
    priceZoneFor,
    irradianceFor,
    pickBestPanelForBudget,
    estimatePanelOutput,
    computePayback,
    projectCost,
    computeRecommendation,
    haversineKm,
  };
})(window);
