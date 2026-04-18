# HappyDays scrapers

Three scrapers live here — one for weather, two for prices.

| file | cadence | writes | purpose |
|---|---|---|---|
| `smhi_scraper.py` | one-shot | `data/smhi_irradiance.json` | 3-year annual kWh/m² means per SMHI station |
| `nordpool_scraper.py` | one-shot | `data/electricity_zones.json` | 2022–2024 monthly-mean rörligt prices per SE1–SE4 |
| `nordpool_live.py` | long-running | `data/nordpool_live.json` + `data/gateway_actions.json` + `data/gateway_commands.txt` | rich web-UI payload, structured Atech actions with metadata, AND a paste-ready 3-line plain-text dump of the three `set_*` commands |

All three are stdlib-only Python 3. No `pip install`, no venv.

---

## `smhi_scraper.py` — SMHI irradiance

From the repo root:

```
python3 scraper/smhi_scraper.py
```

1. Calls `https://opendata-download-metobs.smhi.se/api/version/1.0/parameter/11.json`
   to list stations that observe global irradiance (parameter 11).
2. For each candidate station (hardcoded list of ~14 geographically spread
   stations), downloads the `corrected-archive` CSV of hourly observations.
3. Sums hourly W/m² into annual kWh/m², picks the most recent three years
   that have ≥ 4000 hours, and averages them.
4. Writes `data/smhi_irradiance.json` with a `generated_at` stamp, a
   `source` note, and a `stations` array.

If the SMHI API is unreachable, the script falls back to latitude-zoned
approximations for 12 major Swedish cities — the `source` field flags
which path ran.

---

## `nordpool_scraper.py` — SCB retail zone averages

From the repo root:

```
python3 scraper/nordpool_scraper.py
```

Hits SCB Elprisstatistik pxweb table `SSDManadElhandelpris` for months
`2022M01`–`2024M12` × bidding zones SE1–SE4 × contract type `rörligt` ×
customer category 3 (one/two-dwelling house with electric heating).
Averages the 36 monthly observations per zone and writes
`data/electricity_zones.json` atomically (`.tmp` + `os.replace` — an
interrupted write cannot leave a truncated file that falls through to
the `DEFAULT_GRID_RATE_SEK_PER_KWH` fallback).

Prices are öre/kWh **excluding elskatt, moms and nätavgift**. The full
household bill is typically 60–80 öre/kWh higher once those are added
back; the source note in the JSON says so.

---

## `nordpool_live.py` — live gateway feed

Long-running scraper that writes two JSON files every `POLL_SECONDS`
(default 60 s), one for the website and one for the Atech gateway.
Data source is `elprisetjustnu.se` (mirrors Nord Pool day-ahead as
soon as it's published ~13:00 CET). 15-minute slots are aggregated to
hourly means. The API is only re-hit every `FETCH_SECONDS` (default
**6 h** — day-ahead prices are static once published, so there's no
point hammering the API).

**`data/nordpool_live.json`** — rich payload consumed by the HappyDays
web UI. Current hour + next 12 hours passed through a
relative-tercile traffic-light tier (cheapest 1/3 → green, middle →
yellow, most expensive → red). Served at
`http://localhost:8765/data/nordpool_live.json`.

**`data/gateway_actions.json`** — Atech-firmware-shaped payload:

```json
{
  "zone": "SE4",
  "generated_at": "...",
  "stale": false,
  "actions": [
    {"action": "set_prices",        "value": "32.7,35.1,…"},
    {"action": "set_current_price", "value": "0.33"},
    {"action": "set_hour",          "value": "14"}
  ]
}
```

Units: `set_prices` is 24 comma-separated öre/kWh values for today
(00:00–23:00 Europe/Stockholm), 1 decimal, no spaces; `set_current_price`
is öre/kWh, 1 decimal (matches Atech's confirmed "current hour price in
ore/kWh" wording); `set_hour` is integer 0–23.

**`data/gateway_commands.txt`** — same three actions as
`gateway_actions.json` but stripped to three raw one-line JSON objects
separated by newlines, no wrapper. Designed for manual copy-paste into
the Atech dashboard input field: grab one line, send it, done. Served
as `text/plain`.

```
{"action": "set_prices", "value": "73.8,73.5,..."}
{"action": "set_current_price", "value": "33.9"}
{"action": "set_hour", "value": "15"}
```

The scraper does **not** talk to `gateway.atech.dev` directly — a tiny
forwarder (not in this repo) reads `gateway_actions.json` and POSTs
each entry of `actions` to `https://gateway.atech.dev/send/{project_id}`
or sends it via the live WebSocket. Keeping the JSON-file seam means
the same data can also be polled by the web UI, curl-tested, or mocked.

### Run it in the foreground

```
HAPPYDAYS_ZONE=SE4 python3 scraper/nordpool_live.py
```

Env knobs:

| var | default | meaning |
|---|---|---|
| `HAPPYDAYS_ZONE` | `SE4` | one of `SE1` / `SE2` / `SE3` / `SE4` (Lund is SE4) |
| `POLL_SECONDS` | `60` | how often to rewrite both JSON files (recomputes `set_hour` from cached prices) |
| `FETCH_SECONDS` | `21600` | how often to re-hit `elprisetjustnu.se` (6 h — day-ahead is static after release) |
| `DRY_RUN` | unset | if `1`, log but don't write the files |
| `OUTPUT_DIR` | `data/` | override output directory |

Ctrl-C to stop.

### Run it as a LaunchAgent (macOS, auto-restart on crash, starts at login)

From the repo root:

```
sed "s#__REPO_ROOT__#$PWD#g" \
    scraper/com.happydays.nordpool-live.plist \
  > ~/Library/LaunchAgents/com.happydays.nordpool-live.plist

launchctl bootstrap gui/$(id -u) \
    ~/Library/LaunchAgents/com.happydays.nordpool-live.plist
```

Tail logs:

```
tail -f .happydays-nordpool.out.log .happydays-nordpool.err.log
```

Uninstall:

```
launchctl bootout gui/$(id -u)/com.happydays.nordpool-live
rm ~/Library/LaunchAgents/com.happydays.nordpool-live.plist
```

`KeepAlive=true` + `ThrottleInterval=10` mean the scraper auto-restarts
on crash but won't hot-loop if it fails within 10 s of spawning.

---

## SSL note (all three scrapers)

On stock macOS Python installs the system CA bundle may not be wired up
and you'll see `CERTIFICATE_VERIFY_FAILED`. Each scraper detects this
and transparently retries with an unverified TLS context — we're reading
public data, so the risk is low. To fix it properly, run
`/Applications/Python\ 3.x/Install\ Certificates.command`.
