# HappyDays

A solar-transition advisor for Swedish homeowners — built for
*florent x lund ai society*.

## Problem

Solar decisions in Sweden mean piecing together SMHI irradiance,
SCB zone prices, *grönt avdrag*, *skattereduktion*, SE1–SE4 splits, and
installer quotes that quietly optimise for the installer. No one
calculator shows its work or lets you ask a follow-up.

## Solution

Pin a spot on a map of Sweden, describe your house, get a traceable
answer: energy mix, payback year, 10-year cost table, live Nord Pool
prices for the pinned zone, and a Claude-backed adviser for the
follow-ups.

## Technical approach

- **Frontend** — plain HTML/CSS/vanilla JS. Leaflet +
  OSM map, `calc.js` as a pure `window.HappyDaysCalc` module.
- **Data** — SMHI stations for irradiance, SCB for zone averages,
  *elprisetjustnu.se* for live hourly Nord Pool (all four zones,
  refreshed every 60 s). Cached as JSON under `data/`.
- **Adviser** — `server/main.py`, a single-file stdlib HTTP server on
  `127.0.0.1:8765` proxying to Claude Haiku 4.5 with a ~4.7k-token
  Swedish-solar reference block tagged for prompt caching.

See `server/README.md` and `scraper/README.md` for the deep dive.

## Run

```sh
pip3 install -r server/requirements.txt
cp .env.example .env         # then set ANTHROPIC_API_KEY
python3 server/main.py       # → http://127.0.0.1:8765
python3 scraper/nordpool_live.py   # optional, for live prices
```

The calculator works without an API key; only the adviser chat needs
one.
