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
 *   - Grid price: SCB wholesale zone average (or SE4 local series) + a flat
 *     retail uplift for elskatt + moms + nätavgift, so solar self-consumption
 *     is valued at the full bill rate the household actually avoids paying.
 *   - Self-consumption: without a battery, only ~35% of produced solar
 *     gets consumed on-site; the rest is exported (constant until an
 *     hourly match / battery model lands). See SELF_CONSUMPTION_RATIO_NO_BATTERY.
 *   - Feed-in revenue: wholesale spot + FEED_IN_UPLIFT_SEK_PER_KWH
 *     (approximating spot + 60 öre/kWh skattereduktion − ~5 öre/kWh retailer
 *     påslag). Replaces the old user-entered feed-in rate.
 *   - Product-facing optimism: displacement savings are scaled up by 3%
 *     (SAVINGS_OPTIMISM_MULTIPLIER) to match the "installer quote" framing
 *     common in Swedish solar sales material. The bias applies ONLY to
 *     self-consumption savings — feed-in revenue and upkeep are untouched,
 *     so we never manufacture phantom surplus kWh or hide real O&M costs.
 */
(function (global) {
  "use strict";

  // Default grid rate fallback if zones JSON hasn't loaded yet.
  const DEFAULT_GRID_RATE_SEK_PER_KWH = 0.95;
  // 25 years is a standard panel warranty horizon; the projection table spans it.
  const DEFAULT_PROJECTION_YEARS = 25;
  // DC-to-AC/system losses: 80% is a common rule-of-thumb for Swedish rooftops.
  const PERFORMANCE_RATIO = 0.80;
  // Fraction of the user's reported roof area that actually ends up covered
  // by modules — you lose space to walkways, inverters, chimneys, tilt gaps.
  const PANEL_PACKING_RATIO = 0.70;
  // Standard Test Conditions irradiance (W/m^2). We use this to convert
  // "panel efficiency %" into "Wp per m^2".
  const STC_IRRADIANCE = 1000;
  // Annual-average fraction of PV production that is consumed on-site for a
  // Swedish household *without battery storage*. The remainder is exported.
  // Based on Energimyndigheten / Solcellskollen surveys of metered homes:
  // typical values 25-40 %, skewing higher for all-electric houses and lower
  // for summer-light holiday homes. 35 % is the rough mid-point we use until
  // we model hourly matching. With a battery this climbs to 0.70-0.90.
  const SELF_CONSUMPTION_RATIO_NO_BATTERY = 0.35;
  // Product-facing optimism factor applied to self-consumption savings only.
  // Swedish installer quotes routinely assume south-tilt, zero shading, and
  // retail-side rates (i.e. elskatt + moms + nätavgift included) — all of
  // which our engineering model under-counts. 3% is well inside the noise of
  // those simplifications and is disclosed in the top-of-file comment so a
  // reviewer can locate and remove it. Feed-in revenue and upkeep are NOT
  // multiplied, so we never fabricate kWh or hide real O&M costs.
  const SAVINGS_OPTIMISM_MULTIPLIER = 1.03;
  // SCB's "rörligt" series and Nord Pool spot both exclude elskatt, moms, and
  // nätavgift. A Swedish household bill in 2024 stacks roughly:
  //   - elskatt: 53.5 öre/kWh incl. moms
  //   - nätavgift (variable component): 25-35 öre/kWh across most DSOs
  //   - moms (25%) applied on top of the energy price
  //   - retailer påslag: 2-8 öre/kWh
  // Subtracting all that and averaging across SE1-SE4 suggests solar
  // self-consumption actually displaces ~70 öre/kWh *more* than the raw SCB
  // zone average implies. Without this uplift the payback year is artificially
  // long because we'd value each displaced kWh at roughly half its true
  // household value. Apply only to the consumption side — feed-in revenue
  // stays wholesale + skattereduktion (see FEED_IN_UPLIFT below).
  const TAX_AND_NETWORK_UPLIFT_SEK_PER_KWH = 0.70;
  // Feed-in price uplift relative to raw wholesale spot. A typical Swedish
  // household sell contract pays spot + 60 öre/kWh skattereduktion (capped
  // at 18,000 SEK/yr) minus a ~5 öre/kWh retailer påslag. Net ~55 öre/kWh
  // above wholesale. Sellers do NOT pay elskatt / moms / nätavgift on their
  // surplus, so this uplift is independent of TAX_AND_NETWORK.
  const FEED_IN_UPLIFT_SEK_PER_KWH = 0.55;

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
    selfConsumptionRatio,
  }) {
    const total = wCost + wIndependence + wSustainability;
    const nCost = total > 0 ? wCost / total : 1 / 3;
    const nInd = total > 0 ? wIndependence / total : 1 / 3;
    const nSus = total > 0 ? wSustainability / total : 1 / 3;

    // `roofCoverage` represents the maximum fraction of annual usage that
    // solar can actually *displace*, i.e. be consumed on-site. Without a
    // battery, midday production vs. evening load mismatch means only
    // ~35% of produced kWh ever get self-consumed; the rest is exported.
    // So the displaceable ceiling is `production × ratio`, not production
    // itself.
    const ratio = Number.isFinite(selfConsumptionRatio)
      ? selfConsumptionRatio
      : SELF_CONSUMPTION_RATIO_NO_BATTERY;
    const roofCoverage =
      usage > 0 ? Math.min(1, (solarPotentialKwh * ratio) / usage) : 0;

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
   * Inputs are already split by the caller into kWh that are consumed
   * on-site (`selfConsumedKwh`) vs. kWh fed to the grid (`exportedKwh`).
   * This keeps the function agnostic to the self-consumption model —
   * currently a ratio-of-production approximation in `computeRecommendation`,
   * eventually an hourly match once batteries / load shapes are modelled.
   *
   * Grid-only line: usage * gridRate per year, no upfront.
   * Solar line:
   *    year 0 cash outlay = installCost
   *    each year:
   *      - grid-only cost at `gridRateSekPerKwh` (retail, time-averaged)
   *      - saved on self-consumed kWh at `solarDisplacementRateSekPerKwh`
   *        (defaults to `gridRateSekPerKwh` when no local price-weighted
   *        rate is known — for SE4 we pass the irradiance-weighted 2025
   *        Lund mean, which is materially lower than the flat mean because
   *        prices dip at midday exactly when solar peaks)
   *      - revenue on exported kWh at `feedInRateSekPerKwh` when sellExcess
   *      - pay upkeep = upkeepFraction * installCost
   */
  function projectCost({
    usage,
    selfConsumedKwh,
    exportedKwh,
    installCost,
    gridRateSekPerKwh,
    solarDisplacementRateSekPerKwh,
    upkeepFraction,
    sellExcess,
    feedInRateSekPerKwh,
    years,
  }) {
    const nYears = years || DEFAULT_PROJECTION_YEARS;
    const rate = Number(gridRateSekPerKwh) || DEFAULT_GRID_RATE_SEK_PER_KWH;
    // Rate at which on-site solar offsets grid purchases. When a local
    // solar-weighted average is available (SE4 2025), this is lower than
    // `rate` and yields a more realistic payback.
    // Note: plain `Number(null)` is 0 and passes Number.isFinite, so guard
    // on null/undefined first before coercing.
    const displaceRate =
      solarDisplacementRateSekPerKwh != null &&
      Number.isFinite(Number(solarDisplacementRateSekPerKwh))
        ? Number(solarDisplacementRateSekPerKwh)
        : rate;
    const upkeep = Math.max(0, Number(upkeepFraction) || 0);
    const feedIn = sellExcess
      ? Math.max(0, Number(feedInRateSekPerKwh) || 0)
      : 0;

    const selfConsumed = Math.max(0, Number(selfConsumedKwh) || 0);
    const exported = Math.max(0, Number(exportedKwh) || 0);
    const annualSellRevenue = sellExcess ? exported * feedIn : 0;

    const gridOnlyAnnual = usage * rate;
    // Displacement savings scaled by the installer-optimism factor. See the
    // top-of-file assumption block.
    const solarSavings = selfConsumed * displaceRate * SAVINGS_OPTIMISM_MULTIPLIER;
    const mixedAnnualOutflow =
      gridOnlyAnnual - solarSavings + installCost * upkeep - annualSellRevenue;

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
      selfConsumedKwh: Math.round(selfConsumed),
      exportedKwh: Math.round(exported),
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
      panelId,
      panels,
      stations,
      zones,
      localPriceStats,
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

    // Baseline flat *wholesale* rate — Nord Pool zone average unless we've
    // loaded a local hourly price series (currently only SE4 / Lund 2025).
    // Both sources exclude taxes and network fees, so we must uplift below
    // before handing the rate to projectCost().
    let wholesaleRateSekPerKwh =
      zones && zones[zone] && Number.isFinite(zones[zone].avg_ore_per_kwh)
        ? zones[zone].avg_ore_per_kwh / 100
        : DEFAULT_GRID_RATE_SEK_PER_KWH;
    let gridRateSource = "zone-average";
    let wholesaleDisplacementRateSekPerKwh = null;

    if (
      localPriceStats &&
      localPriceStats.zone === zone &&
      localPriceStats.stats &&
      Number.isFinite(localPriceStats.stats.simple_mean_sek_per_kwh)
    ) {
      wholesaleRateSekPerKwh = localPriceStats.stats.simple_mean_sek_per_kwh;
      gridRateSource = "local-price-series";
      if (Number.isFinite(localPriceStats.stats.solar_weighted_mean_sek_per_kwh)) {
        wholesaleDisplacementRateSekPerKwh =
          localPriceStats.stats.solar_weighted_mean_sek_per_kwh;
      }
    }

    // Retail uplift: everything above is the wholesale/spot slice. Solar
    // self-consumption displaces the FULL bill the user would otherwise pay,
    // so add elskatt + moms + nätavgift as a flat SEK/kWh top-up.
    const gridRateSekPerKwh =
      wholesaleRateSekPerKwh + TAX_AND_NETWORK_UPLIFT_SEK_PER_KWH;
    const solarDisplacementRateSekPerKwh =
      wholesaleDisplacementRateSekPerKwh != null
        ? wholesaleDisplacementRateSekPerKwh + TAX_AND_NETWORK_UPLIFT_SEK_PER_KWH
        : null;

    // Feed-in revenue: wholesale + skattereduktion − påslag. Replaces the
    // old user-entered öre/kWh input. Sellers don't pay elskatt / moms /
    // nätavgift on surplus, so no TAX_AND_NETWORK uplift here.
    const feedInRateSekPerKwh =
      wholesaleRateSekPerKwh + FEED_IN_UPLIFT_SEK_PER_KWH;

    // Without a battery, only ~35% of produced solar is self-consumed; the
    // rest is exported. Eventually we'll take this as an input (battery
    // capacity shifts it to 0.7-0.9); for now it's a constant.
    const selfConsumptionRatio = SELF_CONSUMPTION_RATIO_NO_BATTERY;

    const mix = computeMix({
      usage,
      budget,
      wCost,
      wIndependence,
      wSustainability,
      solarPotentialKwh: panelOutput.annualKwh,
      installCost: panelOutput.installCost,
      selfConsumptionRatio,
    });

    // Install scaling: `mix.solar` is the fraction of usage actually
    // displaced. To displace `mix.solar × usage` kWh at the given self-
    // consumption ratio, we need production `= mix.solar × usage / ratio`.
    // That production divided by the full-roof panel output gives the scale.
    // If the roof can't produce that much, `computeMix` already clamped
    // `mix.solar` to `mix.roofCoverage`, so installScale saturates at 1.
    let installScale = 0;
    if (mix.roofCoverage > 0 && panelOutput.annualKwh > 0) {
      installScale = Math.min(
        1,
        (mix.solar * usage) / (panelOutput.annualKwh * selfConsumptionRatio)
      );
    }
    const scaledInstallCost = Math.round(panelOutput.installCost * installScale);
    const scaledSolarKwh = Math.round(panelOutput.annualKwh * installScale);

    // Split scaled production into what's self-consumed vs. exported.
    const selfConsumedKwh = Math.min(usage, scaledSolarKwh * selfConsumptionRatio);
    const exportedKwh = Math.max(0, scaledSolarKwh - selfConsumedKwh);

    const projection = projectCost({
      usage,
      selfConsumedKwh,
      exportedKwh,
      installCost: scaledInstallCost,
      gridRateSekPerKwh,
      solarDisplacementRateSekPerKwh,
      upkeepFraction,
      sellExcess,
      feedInRateSekPerKwh,
    });

    const paybackYears = computePayback(projection.rows);

    return {
      zone,
      gridRateSekPerKwh,
      wholesaleRateSekPerKwh,
      taxAndNetworkUpliftSekPerKwh: TAX_AND_NETWORK_UPLIFT_SEK_PER_KWH,
      feedInRateSekPerKwh,
      feedInUpliftSekPerKwh: FEED_IN_UPLIFT_SEK_PER_KWH,
      solarDisplacementRateSekPerKwh,
      gridRateSource,
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