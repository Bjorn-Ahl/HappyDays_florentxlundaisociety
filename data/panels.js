/*
 * HappyDays — panel catalogue.
 *
 * Starter specs for 5 common PV panel chemistries, plus the "auto" sentinel
 * which tells the calculator to pick the best panel inside a budget.
 *
 * Prices are 2024 ballpark Swedish installed-module costs per Wp (SEK/W),
 * taken from consumer-grade surveys (Solcellskollen, Energimyndigheten
 * indicative price ranges). They are *starter* values only — a real
 * deployment would want a quote-driven data source.
 *
 * `efficiencyPct` is the mid-range module efficiency percentage
 * (STC conditions). Values at the conservative end of typical retail specs.
 *
 * Loaded as a plain <script>; exposes window.PANEL_TYPES.
 */
(function (global) {
  "use strict";

  const PANEL_TYPES = [
    {
      id: "mono",
      label: "Monocrystalline",
      efficiencyPct: 18.5,
      sekPerW: 10,
      note: "Most common residential pick. Good efficiency, mature tech.",
    },
    {
      id: "poly",
      label: "Polycrystalline",
      efficiencyPct: 15.0,
      sekPerW: 8,
      note: "Cheaper per watt but larger area for the same output.",
    },
    {
      id: "thinfilm",
      label: "Thin-Film",
      efficiencyPct: 11.0,
      sekPerW: 7,
      note: "Lightweight, OK in shade, but needs lots of roof area.",
    },
    {
      id: "bifacial",
      label: "Bifacial (PERC)",
      efficiencyPct: 20.0,
      sekPerW: 12,
      note: "Premium output — gathers reflected light from behind.",
    },
    {
      id: "cdte",
      label: "CdTe",
      efficiencyPct: 10.0,
      sekPerW: 6,
      note: "Budget thin-film chemistry. Modest efficiency.",
    },
    {
      id: "auto",
      label: "Auto (maximise output within budget)",
      efficiencyPct: null,
      sekPerW: null,
      note: "Calculator picks the panel with the highest annual kWh inside your budget.",
    },
  ];

  global.PANEL_TYPES = PANEL_TYPES;
})(window);
