#!/usr/bin/env python3
"""
HappyDays v3 — single-file development server.

Serves the static SPA from the repo root on port 8765 and exposes a streaming
`POST /api/chat` endpoint backed by Anthropic's Claude Haiku 4.5. The cacheable
system prefix (persona + reference tables) is loaded on start and marked with
`cache_control: ephemeral` so repeat calls hit Anthropic's prompt cache.

Run:

    export ANTHROPIC_API_KEY=sk-ant-...
    python3 server/main.py

The server still serves static files if ANTHROPIC_API_KEY is missing — the
/api/chat endpoint simply returns 503 in that case.
"""

from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

# --------------------------------------------------------------------------
# Paths & config
# --------------------------------------------------------------------------

SERVER_DIR = Path(__file__).resolve().parent
ROOT_DIR = SERVER_DIR.parent  # repo root (index.html lives here)
PORT = 8765
HOST = "127.0.0.1"

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".webp": "image/webp",
    ".txt": "text/plain; charset=utf-8",
    ".map": "application/json; charset=utf-8",
}


# --------------------------------------------------------------------------
# Rate limiting (per-IP, in-memory, resets on restart)
# --------------------------------------------------------------------------

RATE_LIMIT_WINDOW_S = 60
RATE_LIMIT_MAX = 20
_rate_lock = threading.Lock()
_rate_hits: dict[str, deque] = {}



def rate_limited(ip: str) -> bool:
    now = time.monotonic()
    with _rate_lock:
        dq = _rate_hits.setdefault(ip, deque())
        while dq and (now - dq[0]) > RATE_LIMIT_WINDOW_S:
            dq.popleft()
        if len(dq) >= RATE_LIMIT_MAX:
            return True
        dq.append(now)
        return False


# --------------------------------------------------------------------------
# Reference block (cached system prefix)
# --------------------------------------------------------------------------

SYSTEM_PERSONA = """
You are a knowledgeable, friendly Swedish solar-transition adviser for the HappyDays website.
Style: concise, warm, practical. Default to Swedish unless the user writes in another language.
Scope: solar PV for Swedish homeowners — installation decisions, panel types, SE1-SE4 price
zones, permits (bygglov), subsidies (grönt avdrag), feed-in tariffs, battery storage, payback.
Do NOT give binding legal/tax advice; recommend verifying with Skatteverket / bygglovshandläggare.
When the user has a current recommendation, reference its values (payback years, mix %, SEK
savings, self-consumed vs. exported kWh) directly; otherwise explain generally.
Keep answers under 200 words unless asked for detail.
""".strip()


def _read_json(relpath: str) -> dict:
    p = ROOT_DIR / relpath
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] could not read {relpath}: {exc}", file=sys.stderr)
        return {}


def _extract_panel_types() -> list[dict]:
    """
    Pull the PANEL_TYPES literal out of data/panels.js without evaluating JS.
    We look for the array body between `const PANEL_TYPES = [` and the matching
    `];` and parse each object by regex — good enough for this static file.
    """
    p = ROOT_DIR / "data" / "panels.js"
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return []
    m = re.search(r"PANEL_TYPES\s*=\s*\[(.*?)\];", text, re.DOTALL)
    if not m:
        return []
    body = m.group(1)
    panels = []
    for obj in re.finditer(r"\{(.*?)\}", body, re.DOTALL):
        fields = {}
        for km in re.finditer(r"(\w+)\s*:\s*(\"[^\"]*\"|'[^']*'|[\d.]+|null)", obj.group(1)):
            key = km.group(1)
            raw = km.group(2)
            if raw.startswith(('"', "'")):
                val = raw[1:-1]
            elif raw == "null":
                val = None
            else:
                try:
                    val = float(raw) if "." in raw else int(raw)
                except ValueError:
                    val = raw
            fields[key] = val
        if fields:
            panels.append(fields)
    return panels


def build_reference_block() -> str:
    """Assemble a static reference the model can use. Padded so the prefix
    comfortably exceeds Haiku 4.5's 4096-token minimum cacheable length."""

    irr = _read_json("data/smhi_irradiance.json")
    zones_doc = _read_json("data/electricity_zones.json")
    panels = _extract_panel_types()

    parts: list[str] = []

    parts.append("# HappyDays reference tables\n")
    parts.append(
        "These tables are the same data the web calculator uses. Prefer them "
        "over making up values. When the user has a current recommendation "
        "(see dynamic context), cross-reference it for consistency.\n"
    )

    # --- Panel catalogue -------------------------------------------------
    parts.append("\n## Panel catalogue (data/panels.js)\n")
    parts.append(
        "Typical Swedish retail installed-module costs per Wp (SEK/W) and "
        "mid-range module efficiency at STC. Prices are 2024 ballpark figures; "
        "a production build would drive these from installer quotes.\n"
    )
    parts.append("| id | label | efficiency_pct | sek_per_w | note |\n")
    parts.append("|---|---|---|---|---|\n")
    for p in panels:
        eff = p.get("efficiencyPct")
        sek = p.get("sekPerW")
        parts.append(
            f"| {p.get('id','?')} | {p.get('label','?')} | "
            f"{'' if eff in (None, 'null') else eff} | "
            f"{'' if sek in (None, 'null') else sek} | "
            f"{p.get('note','')} |\n"
        )

    # --- Electricity zones ------------------------------------------------
    parts.append("\n## Swedish electricity bidding zones (data/electricity_zones.json)\n")
    parts.append(
        zones_doc.get("source_note", "")
        + "\n"
        + zones_doc.get("zone_boundary_simplification", "")
        + "\n"
    )
    parts.append("| zone | name | avg_ore_per_kwh | notes |\n")
    parts.append("|---|---|---|---|\n")
    for zid, z in (zones_doc.get("zones") or {}).items():
        parts.append(
            f"| {zid} | {z.get('name','')} | {z.get('avg_ore_per_kwh','')} | "
            f"{z.get('source','')} |\n"
        )

    # --- SMHI stations ----------------------------------------------------
    parts.append("\n## SMHI irradiance stations (data/smhi_irradiance.json)\n")
    parts.append(
        f"Source: {irr.get('source','')}. Parameter: {irr.get('parameter','')}.\n"
    )
    parts.append(
        "Annual kWh/m^2/year per station (3-year mean). The calculator uses "
        "nearest-station lookup by Haversine distance from the pinned map "
        "location; latitude-zoned fallback (900/950/1000/1050) applies only "
        "when no station is within range.\n"
    )
    parts.append("| id | name | lat | lng | annual_kwh_per_m2 |\n")
    parts.append("|---|---|---|---|---|\n")
    for s in (irr.get("stations") or []):
        parts.append(
            f"| {s.get('id','')} | {s.get('name','')} | {s.get('lat','')} | "
            f"{s.get('lng','')} | {s.get('annual_kwh_per_m2','')} |\n"
        )

    # --- Primer: short solar-knowledge background ------------------------
    parts.append(
        """

## Solar-knowledge primer (background for the adviser)

### Permit & regulatory landscape
- **Bygglov**: Roof-parallel PV on most Swedish one- and two-family houses does
  NOT require bygglov since the 2017 amendment to plan- och bygglagen, as long
  as panels follow the roof's colour and shape. Cultural-heritage zones
  (kulturmiljöområden) and certain municipalities still require permit review.
  Always advise the user to confirm locally.
- **Anmälan**: Even when bygglov is waived, some municipalities want a written
  notice (anmälan). Check the kommun's bygg-site.
- **Grid connection**: The installer typically files the föranmälan to the DSO
  (Ellevio, Vattenfall Eldistribution, E.ON Energidistribution, etc.). The DSO
  must install a new production-capable meter at no cost for installations up
  to 43.5 kVA under the Elförordning rules.

### Tax & subsidy landscape
- **Grönt avdrag** (green tax deduction): From 2021, homeowners get a 20%
  deduction off installed PV cost (materials + labour), applied at invoice by
  the installer. Capped at 50,000 SEK per person per year. Stackable with the
  skattereduktion for micro-production below.
- **Skattereduktion för mikroproduktion**: Fed-in surplus qualifies for a
  60 öre/kWh tax reduction (up to 18,000 SEK/year), provided the property has
  a single connection point and feed-in <= 100 amps. This is separate from the
  price the electricity retailer pays for the surplus (spot-linked).
- **Moms (VAT)**: Small-scale private micro-producers normally stay below the
  80,000 SEK/year VAT registration threshold and do not need to register.

### Economics rules of thumb
- Installed cost 2024: 12,000-20,000 SEK/kWp turnkey including VAT, mono or
  PERC bifacial panels, string inverter, no storage. Lower (~10-14,000) for a
  6-10 kWp job in the competitive south; higher on a complicated roof.
- Annual yield: 850-1,100 kWh per installed kWp per year depending on latitude,
  orientation, and shading. The HappyDays model uses an 80% performance ratio
  and 70% packing ratio by default.
- Payback: 8-14 years is the typical window for a south-facing roof in SE3/SE4
  with grönt avdrag applied. SE1/SE2 payback is longer because grid prices
  are lower (even though generation is only ~15-20% weaker).

### Panels & tech
- **Monocrystalline** (most common): 18-22% efficiency, 25-year performance
  warranty at 80-85% of Wp, degradation ~0.4-0.5%/yr.
- **Bifacial PERC**: 20-22% front-side efficiency plus 5-15% rear-side gain on
  light roofs or ground mounts with high albedo (snow, white gravel).
- **TOPCon / HJT**: Newer cell technologies edging into residential, 22-23%
  efficiency, better low-light and temperature coefficient.
- **Thin-film (CIGS / CdTe)**: Cheaper per Wp historically, better shade
  tolerance, but needs 30-50% more roof area per kWp and is now rarely used
  residential in Sweden (supply chain mostly commercial/utility).

### Batteries
- Typical home battery 5-15 kWh (LFP chemistry). Installed cost 2024 around
  8,000-13,000 SEK/kWh usable. Makes the most sense with high self-consumption
  goals and time-varying hourly tariffs (timavtal). Pure payback is usually
  still longer than the warranty (10 yr) unless the battery is bundled into a
  demand-response programme.
- Battery installs ALSO qualify for grönt avdrag (50% deduction, capped at
  50,000 SEK/person/year) — advise the user to confirm current Skatteverket
  terms since the rate has been revised multiple times.

### Orientation & shading
- South-facing at 30-45 degrees is the textbook optimum in Sweden, but
  east-west split installations often only lose 10-15% annual yield and spread
  production more evenly through the day (better self-consumption).
- Even small shadows over a string can crush output because of how bypass
  diodes fail open. Panel-level optimisers or micro-inverters mitigate this.

### Winter & snow
- Snow coverage can zero a panel out for days or weeks mid-winter. Factor 0-5%
  winter-month generation into any planning, but remember Nov-Jan is also
  typically only ~3-5% of annual yield in the south and almost nil in the
  north, so the impact is smaller than people expect.
- Tilt >= 30 degrees helps snow slide off; dark frames melt snow edges sooner.

### Home-load & self-consumption strategy
- Without storage, residential self-consumption is typically 25-40% of
  production. With heat pump + EV charging shifted to daytime, it rises to
  50-60%. A battery can push it to 70-80%.
- Time-of-use tariffs (timavtal) in SE3/SE4 now routinely show 200-500 öre/kWh
  peaks in winter mornings and evenings. Smart-charging an EV off-peak can be
  a bigger economic lever than adding more panels.

### Data-freshness caveats
- Electricity prices in electricity_zones.json are now fetched live from SCB
  Elprisstatistik table SSDManadElhandelpris (rörligt contract, kundkategori
  3) by scraper/nordpool_scraper.py. Three-year means 2022-2024 per elområde.
  Values exclude elskatt, moms and nätavgift — the full household bill is
  typically 60-80 öre/kWh higher. The 2022 crisis is baked into the average
  (SE3/SE4 are pulled up ~15-25 öre by late-2022 spikes). Refresh by
  re-running the scraper.
- SE4 specifically also has data/lund_price_stats.json: 2025 hourly SE4 spot
  prices intersected with local SMHI irradiance. It ships both a flat
  (24/7) mean and an irradiance-weighted mean, and calc.js uses BOTH — the
  flat for the grid-only baseline and the weighted for solar displacement.
- SMHI irradiance figures are 3-year means from the global-irradiance
  parameter (parameter 11) on corrected-archive stations. Year-to-year can
  vary by +/-5-8% due to cloud cover.
- Self-consumption without a battery is modelled as a fixed 35% of annual
  production (SELF_CONSUMPTION_RATIO_NO_BATTERY in calc.js). Batteries push
  this to 70-90% but are not yet wired into the model.
- Displacement savings carry a built-in +3% product-facing optimism
  multiplier (SAVINGS_OPTIMISM_MULTIPLIER in calc.js). If a user asks
  "where does this number come from", acknowledge it — do not pretend the
  model is purely engineering. Feed-in revenue and upkeep are NOT biased.
- Always remind the user that these are illustrative starter figures; for a
  binding quote they should use an installer's site survey and a current
  retail contract.

### Step-by-step: from idea to working install (the usual sequence)

1. **Roof survey**: confirm available south/east/west surface area, pitch
   (ideally 25-45 degrees), roof age (panels last 25+ years — you don't want
   to re-roof in 8 years and pay to dismount/remount), and shading from trees,
   chimneys, dormers, or neighbouring buildings. Use Google Earth tilt or a
   free site survey from an installer.
2. **Load analysis**: pull last 12 months of electricity invoices from the
   retailer (or Ellevio/Vattenfall "Mitt Ellevio" / "Mina Sidor") and note
   total kWh/yr plus the monthly curve. Householders with a heat pump, EV, or
   sauna will shift the load profile substantially — flag this to installers.
3. **Contract review**: check current retail contract. Are you on fixed-price
   (fastprisavtal), variable-price (rörligt), or timprisavtal (hourly spot)?
   Solar self-consumption benefits most when your marginal grid kWh is
   expensive, so hourly/daily users see the biggest bill impact.
4. **Installer quotes**: get 3 quotes. Compare on: panel brand + model number,
   inverter brand + model, warranty length (panel 25 yr, inverter 10-12 yr),
   SEK/kWp all-in, install timeline, grönt avdrag handling, and whether the
   quote includes the DSO föranmälan paperwork.
5. **Municipality check**: even if bygglov isn't required, some municipalities
   want a written anmälan for installations over 15 kW or on listed buildings.
   Check the kommun's bygg-site or call the bygglovshandläggare.
6. **DSO föranmälan**: installer submits. DSO has 30 days to respond. Most
   single-family installations sail through.
7. **Install**: typical turnkey install takes 1-3 days on site. Scaffolding
   rental (byggställning) is usually included for multi-day jobs.
8. **Inspection & commissioning**: installer hands over the färdiganmälan to
   the DSO; DSO swaps the meter for a bidirectional model (no cost under the
   current rules, up to 43.5 kVA).
9. **Retail-side sell contract**: pick a retailer that buys your surplus at a
   reasonable rate. Good options in 2024-2025 have been Tibber, GodEl,
   Bixia, and Telge Energi — compare on buy-back price (öre/kWh) and monthly
   fee. Many retailers now offer "spot + påslag" for surplus, typically
   Nordpool spot minus 5-10 öre.
10. **Ongoing**: monitor via the inverter app (SolarEdge, Fronius, Huawei,
    etc.). Watch for sudden drops that might indicate a dead bypass diode,
    string fault, or snow-load. Book a professional inspection every 5 years
    or after major storms.

### Common misconceptions to gently correct

- "Solar doesn't work in Sweden." It does. Lund and Göteborg get more annual
  kWh/m^2 than Hamburg; even Luleå (65 deg N) gets ~930 kWh/m^2. The lower
  winter sun is partially offset by cooler panels running at higher
  efficiency, and summer days of 18+ daylight hours produce huge yields.
- "Panels are pointless without a battery." False. Self-consumption at 25-40%
  without a battery still returns positive economics in SE3/SE4 thanks to
  grönt avdrag and grid-sell revenue. Batteries are an optimisation, not a
  prerequisite.
- "East-west is useless." West- and east-facing roofs lose only 10-20% versus
  south-facing and actually produce more balanced output through the day.
- "Panels require constant cleaning." Rain handles most of it. Annual
  inspection + occasional hose-down is typically enough. Clean after major
  pollen or Saharan-dust events.
- "Bygglov is always needed." Not for roof-parallel PV on most single-family
  houses since 2017. Colour-matched, roof-parallel panels outside heritage
  zones are exempt.
- "Feed-in rates are trivial." They're not huge, but spot + skattereduktion +
  möjligt producentavtal stacks to roughly 60-120 öre/kWh net depending on
  zone and time — enough to turn a marginal project positive.

### Frequently asked user questions and sketched answers

- *"Hur stor anläggning passar mig?"* — Rule of thumb: 1 kWp per 5-6 m^2 of
  usable roof, generating roughly 900-1,000 kWh/kWp/year in the south and
  800-900 in the north. Size so that summer peak production is close to your
  average daytime load plus whatever surplus you're willing to feed in.
- *"Lönar det sig att lägga till en batteri?"* — Depends on your tariff.
  Under a flat rörligt contract: rarely. Under a timpris contract with big
  spot-price peaks: often, once prices spread > 150 öre/kWh between trough
  and peak. Break-even is typically 10-14 years, close to warranty.
- *"Vad händer om jag flyttar?"* — Panels and inverter stay with the house.
  They add typically 5-15% to valuation in Swedish real-estate surveys,
  especially for recent installs with remaining warranty. Check the
  mäklare's comparables for your area.
- *"Vilken inverter är bäst?"* — There's no single answer; SolarEdge,
  Fronius, Huawei SUN2000, Enphase micro-inverters, and Solis all have
  strong installers in Sweden. Prioritise local service network + 10-year
  warranty + monitoring app quality over raw efficiency (all modern units
  hit 97-98% peak).

### Glossary (Swedish solar terms)

- **Bygglov**: Building permit.
- **Anmälan**: Notification to the municipality (lighter than bygglov).
- **Föranmälan / Färdiganmälan**: Pre- and post-install filings to the DSO.
- **DSO (Distribution System Operator)**: Elnätsbolag, the local grid owner
  (Ellevio, Vattenfall Eldistribution, E.ON Energidistribution, and ~170
  small regional co-ops).
- **Grönt avdrag**: The 2021+ green tax deduction for renewable installs.
- **Mikroproduktion**: Grid-connected production <= 43.5 kVA and <= 100 A.
- **Skattereduktion**: Tax credit on feed-in, separate from the retailer's
  price for the surplus.
- **Nätavgift**: The fixed and variable fees charged by the DSO, independent
  of which retailer sells you electricity.
- **Elpris / Spotpris**: Nord Pool day-ahead hourly clearing price.
- **Solcellskollen / Energimyndigheten**: Independent consumer information
  sources — useful to cite when the user wants a neutral second opinion.

### Example response tone (for reference only)

A good answer in Swedish looks like:

> Utifrån din nuvarande rekommendation (ca 60% sol, 40% nät, återbetalning på
> 9 år) skulle jag säga att... [konkret tips]. Kom ihåg att du alltid kan
> verifiera exakta avdragsregler hos Skatteverket och bygglovskraven hos din
> kommun.

A good answer in English looks like:

> Based on your current recommendation (about 60% solar, 40% grid, 9-year
> payback), I'd suggest... [concrete tip]. As always, verify the exact grönt
> avdrag rules with Skatteverket and the permit status with your kommun's
> bygglovshandläggare.

Be warm, be specific, and refer to the user's numbers when they are
available.

### Additional Swedish-solar context the adviser may draw on

- **Climate zone variation**: SMHI divides Sweden into four major climate
  zones for building-code purposes. Solar yield drops roughly 15-20% from
  Skåne (zone IV) to Norrbotten (zone I), but the decline is not linear —
  coastal and island locations (Gotland, Öland, Bohuslän) get 10-15% more
  direct sun than inland locations at the same latitude due to lower cloud
  cover. This is why Visby (1,132 kWh/m^2) and Hoburg (1,166 kWh/m^2) beat
  Stockholm (1,011 kWh/m^2) despite being at similar latitude.
- **Seasonal distribution**: roughly 75-80% of annual PV yield in Sweden
  comes between April and September. January typically produces 1-3% of
  annual yield in the south, often well under 1% in the north. Sizing for
  annual self-consumption therefore trades off summer surplus (which must
  be fed in or stored) against winter grid draw (which must be bought at
  prevailing rates). A household that only heats with a heat pump and uses
  the EV during work hours will usually get a better summer-consumption
  match by sizing for about 80% of annual usage rather than 100%.
- **Temperature coefficients**: PV efficiency drops roughly 0.3-0.4%/°C
  above STC (25°C). Sweden's cool summers are a small advantage here — a
  Swedish rooftop at 40°C will produce a percentage point or two more per
  kWp than the same panel in southern Europe at 55°C. Dark-framed panels
  do run hotter in full sun; clear-anodised frames shave a few degrees.
- **Wiring and breakers**: most Swedish single-family homes have a
  16-25 A main fuse (huvudsäkring). An installation up to around 9 kWp
  three-phase slots into a 16 A main without upgrading; above that the
  installer may recommend a fuse upgrade (usually 1,500-5,000 SEK flat
  fee to the DSO). For single-phase installations the ceiling is lower
  (~4.6 kW per phase on a 16 A fuse).
- **Fire and insurance**: Svensk Försäkring generally covers PV damage
  under standard villa-insurance, but require a fackmannamässigt
  monterad certificate from the installer. If the panels come loose in a
  storm or a bird strike cracks a module, the claim process is the same
  as for any other roof damage. Ask the insurer for a written confirmation
  before installing; a handful of insurers charge a small premium uplift
  for PV (typically 100-400 SEK/yr).
- **End-of-life**: PV modules are covered under the WEEE directive (EU
  battery / electronics recycling). The installer is legally responsible
  for free take-back at the end of life, though most homeowners keep the
  panels running well past the 25-year warranty. Inverters typically need
  replacement at years 10-15.
"""
    )

    return "".join(parts)


REFERENCE_BLOCK = build_reference_block()
CACHED_SYSTEM_TEXT = SYSTEM_PERSONA + "\n\n---\n\n" + REFERENCE_BLOCK
APPROX_TOKENS = len(CACHED_SYSTEM_TEXT) // 4

# --------------------------------------------------------------------------
# Anthropic client (lazy)
# --------------------------------------------------------------------------

_anthropic_client = None
_anthropic_import_error: str | None = None

try:
    import anthropic  # type: ignore

    _anthropic_available = True
except Exception as exc:  # noqa: BLE001
    anthropic = None  # type: ignore
    _anthropic_available = False
    _anthropic_import_error = str(exc)


def get_anthropic_client():
    global _anthropic_client
    if not _anthropic_available:
        return None
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    return _anthropic_client


# --------------------------------------------------------------------------
# Request handler
# --------------------------------------------------------------------------


class HappyDaysHandler(BaseHTTPRequestHandler):
    # Keep the stdlib default server_version but tidy the access logs.
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A003, D401
        # We emit our own one-line access log from _respond_static / chat.
        pass

    # ---- helpers ------------------------------------------------------
    def _client_ip(self) -> str:
        # Honour X-Forwarded-For in case a reverse proxy is in front.
        xff = self.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
        return self.client_address[0]

    def _log_access(self, status: int, started_at: float) -> None:
        dur_ms = int((time.monotonic() - started_at) * 1000)
        print(
            f"{self.command} {self.path} -> {status} ({dur_ms} ms) "
            f"from {self._client_ip()}",
            flush=True,
        )

    def _send_json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---- routing ------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        started = time.monotonic()
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path.startswith("/api/"):
            self._send_json(404, {"error": "unknown endpoint"})
            self._log_access(404, started)
            return

        status = self._serve_static(path, head_only=False)
        self._log_access(status, started)

    def do_HEAD(self) -> None:  # noqa: N802
        started = time.monotonic()
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path.startswith("/api/"):
            self._send_json(404, {"error": "unknown endpoint"})
            self._log_access(404, started)
            return
        status = self._serve_static(path, head_only=True)
        self._log_access(status, started)

    def do_POST(self) -> None:  # noqa: N802
        started = time.monotonic()
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path == "/api/chat":
            status = self._handle_chat()
            self._log_access(status, started)
            return

        self._send_json(404, {"error": "unknown endpoint"})
        self._log_access(404, started)

    # ---- static ------------------------------------------------------
    def _serve_static(self, url_path: str, *, head_only: bool = False) -> int:
        # Resolve inside ROOT_DIR, preventing path traversal.
        if url_path in ("", "/"):
            url_path = "/index.html"
        rel = url_path.lstrip("/")
        target = (ROOT_DIR / rel).resolve()
        try:
            target.relative_to(ROOT_DIR.resolve())
        except ValueError:
            self._send_json(403, {"error": "forbidden"})
            return 403
        if not target.exists() or not target.is_file():
            self._send_json(404, {"error": "not found"})
            return 404

        ctype = MIME_TYPES.get(target.suffix.lower(), "application/octet-stream")
        try:
            data = target.read_bytes()
        except OSError:
            self._send_json(500, {"error": "read failed"})
            return 500

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if head_only:
            return 200
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass
        return 200

    # ---- chat --------------------------------------------------------
    def _handle_chat(self) -> int:
        ip = self._client_ip()

        # Rate limit
        if rate_limited(ip):
            self._send_json(
                429,
                {"error": "Too many requests — 20 per minute. Please wait."},
            )
            return 429

        # Parse body
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            self._send_json(400, {"error": "empty body"})
            return 400
        try:
            raw = self.rfile.read(length)
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send_json(400, {"error": "invalid JSON"})
            return 400

        messages = body.get("messages") or []
        context = body.get("context") or {}
        if not isinstance(messages, list) or not messages:
            self._send_json(400, {"error": "missing messages"})
            return 400

        # Sanitize messages: only user/assistant roles, string content.
        clean: list[dict] = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            content = m.get("content")
            if role in ("user", "assistant") and isinstance(content, str) and content:
                clean.append({"role": role, "content": content})
        if not clean:
            self._send_json(400, {"error": "no usable messages"})
            return 400

        # Key check
        client = get_anthropic_client()
        if client is None:
            if not _anthropic_available:
                self._send_json(
                    503,
                    {
                        "error": (
                            "anthropic SDK not installed — run "
                            "`pip install -r server/requirements.txt`."
                        )
                    },
                )
                return 503
            self._send_json(
                503,
                {
                    "error": (
                        "ANTHROPIC_API_KEY not set — the server is running "
                        "but the adviser is disabled."
                    )
                },
            )
            return 503

        # Stream SSE
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(obj) -> bool:
            try:
                self.wfile.write(
                    f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")
                )
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError):
                return False

        def emit_done() -> None:
            try:
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        try:
            dynamic = (
                "Current user context (may be empty):\n"
                + json.dumps(context, ensure_ascii=False, sort_keys=True)
            )
            with client.messages.stream(
                model="claude-haiku-4-5",
                max_tokens=1024,
                system=[
                    {
                        "type": "text",
                        "text": CACHED_SYSTEM_TEXT,
                        "cache_control": {"type": "ephemeral"},
                    },
                    {"type": "text", "text": dynamic},
                ],
                messages=clean,
            ) as stream:
                for text in stream.text_stream:
                    if not text:
                        continue
                    if not emit({"t": text}):
                        return 200
                try:
                    final = stream.get_final_message()
                    usage = getattr(final, "usage", None)
                    if usage is not None:
                        print(f"[chat] usage: {usage}", flush=True)
                except Exception:
                    pass
        except Exception as exc:  # noqa: BLE001
            print(f"[chat] stream error: {exc}", file=sys.stderr, flush=True)
            emit({"error": str(exc)})

        emit_done()
        return 200


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main() -> int:
    key_set = bool(os.environ.get("ANTHROPIC_API_KEY"))
    print(f"HappyDays server starting on http://{HOST}:{PORT}", flush=True)
    print(f"  root:        {ROOT_DIR}", flush=True)
    print(
        f"  cached prefix chars: {len(CACHED_SYSTEM_TEXT)} "
        f"(~{APPROX_TOKENS} tokens, min cacheable = 4096)",
        flush=True,
    )
    if APPROX_TOKENS < 4096:
        print(
            "  [warn] cached prefix is below Haiku 4.5's 4096-token minimum — "
            "cache will silently miss. Expand build_reference_block().",
            flush=True,
        )
    print(
        f"  anthropic sdk: {'ok' if _anthropic_available else 'missing (' + (_anthropic_import_error or '?') + ')'}",
        flush=True,
    )
    print(f"  api key:     {'set' if key_set else 'NOT SET (chat disabled)'}", flush=True)

    try:
        server = ThreadingHTTPServer((HOST, PORT), HappyDaysHandler)
    except OSError as exc:
        print(f"Could not bind {HOST}:{PORT}: {exc}", file=sys.stderr)
        print("  Hint: lsof -ti :8765 | xargs kill", file=sys.stderr)
        return 1

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
