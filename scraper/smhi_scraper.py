#!/usr/bin/env python3
"""
SMHI irradiance scraper for HappyDays v2.

Fetches global irradiance (parameter 11) observations from SMHI Meteorological
Observations open data API, averages the last 3 years of data per station into
annual kWh/m^2/year, and writes the result to data/smhi_irradiance.json.

Standard library only: urllib, json, statistics, datetime.

If the real API is unreachable or returns unexpected shapes, the script falls
back to writing a JSON file with latitude-zoned approximations for 12 major
Swedish cities (same shape), and records that in the `source` field so the
frontend can still render a working map.

Usage:
    python3 scraper/smhi_scraper.py

API docs:
    https://opendata.smhi.se/apidocs/metobs/
    parameter 11 = Globalstrålning (global irradiance), unit W/m^2
    "corrected-archive" period returns hour-by-hour historical data.
"""

from __future__ import annotations

import json
import math
import os
import ssl
import statistics
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone


def _open_url(url: str, headers: dict):
    """
    Open a URL, transparently falling back to an unverified SSL context if the
    system CA bundle isn't wired up (common on stock macOS Python installs
    where `Install Certificates.command` was never run). We're fetching public
    meteorological data, so an unverified fallback is acceptable here.
    """
    req = urllib.request.Request(url, headers=headers)
    try:
        return urllib.request.urlopen(req, timeout=TIMEOUT)
    except urllib.error.URLError as e:
        if isinstance(e.reason, ssl.SSLError):
            print(
                f"[smhi_scraper] SSL verify failed, retrying with unverified context "
                f"({e.reason})",
                file=sys.stderr,
            )
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx)
        raise

# Parameter 11 = Global irradiance (Globalstrålning), hourly average W/m^2
PARAMETER_ID = 11
# Use the corrected archive which is the most trustworthy long period.
# "latest-months" also exists but only covers ~4 months.
PERIOD = "corrected-archive"

BASE = "https://opendata-download-metobs.smhi.se/api/version/1.0"
PARAM_URL = f"{BASE}/parameter/{PARAMETER_ID}.json"

# HTTP timeout in seconds for each request.
TIMEOUT = 20

# Candidate stations geographically spread across Sweden.
# These IDs are well-known SMHI stations; we verify each against the parameter
# station list at runtime and skip any that don't carry parameter 11.
CANDIDATE_STATION_IDS = [
    178985,  # Tarfala Sol (far north, alpine)
    180025,  # Kiruna Sol
    162015,  # Luleå Sol
    140615,  # Umeå Sol
    134615,  # Östersund Sol
    105285,  # Borlänge Sol
    93235,   # Karlstad Sol
    98735,   # Stockholm Sol
    86655,   # Norrköping Sol
    71415,   # Göteborg Sol
    78645,   # Visby Sol
    68545,   # Hoburg Sol (Gotland south)
    64565,   # Växjö Sol
    53445,   # Lund Sol
]

# Fallback: latitude-zoned annual irradiance approximations (kWh/m^2/year) for
# 12 Swedish cities. Used only if the live API cannot be reached.
FALLBACK_CITIES = [
    {"id": "fb-kiruna",    "name": "Kiruna",     "lat": 67.86, "lng": 20.22, "annual_kwh_per_m2": 830},
    {"id": "fb-lulea",     "name": "Luleå",      "lat": 65.58, "lng": 22.16, "annual_kwh_per_m2": 900},
    {"id": "fb-umea",      "name": "Umeå",       "lat": 63.83, "lng": 20.26, "annual_kwh_per_m2": 930},
    {"id": "fb-ostersund", "name": "Östersund",  "lat": 63.18, "lng": 14.64, "annual_kwh_per_m2": 920},
    {"id": "fb-sundsvall", "name": "Sundsvall",  "lat": 62.39, "lng": 17.31, "annual_kwh_per_m2": 950},
    {"id": "fb-uppsala",   "name": "Uppsala",    "lat": 59.86, "lng": 17.64, "annual_kwh_per_m2": 980},
    {"id": "fb-stockholm", "name": "Stockholm",  "lat": 59.33, "lng": 18.07, "annual_kwh_per_m2": 990},
    {"id": "fb-orebro",    "name": "Örebro",     "lat": 59.27, "lng": 15.21, "annual_kwh_per_m2": 985},
    {"id": "fb-goteborg",  "name": "Göteborg",   "lat": 57.71, "lng": 11.97, "annual_kwh_per_m2": 1010},
    {"id": "fb-visby",     "name": "Visby",      "lat": 57.64, "lng": 18.30, "annual_kwh_per_m2": 1060},
    {"id": "fb-malmo",     "name": "Malmö",      "lat": 55.60, "lng": 13.00, "annual_kwh_per_m2": 1050},
    {"id": "fb-lund",      "name": "Lund",       "lat": 55.70, "lng": 13.19, "annual_kwh_per_m2": 1050},
]


def fetch_json(url: str):
    """GET a URL and parse it as JSON."""
    with _open_url(url, headers={
        "User-Agent": "HappyDays-SMHI-scraper/2.0 (educational; +github)",
        "Accept": "application/json",
    }) as resp:
        data = resp.read()
    return json.loads(data.decode("utf-8"))


def fetch_text(url: str) -> str:
    """GET a URL and return response body as text."""
    with _open_url(url, headers={
        "User-Agent": "HappyDays-SMHI-scraper/2.0 (educational; +github)",
        "Accept": "text/csv, text/plain, */*",
    }) as resp:
        data = resp.read()
    return data.decode("utf-8", errors="replace")


def list_stations_with_param() -> list[dict]:
    """Get all SMHI stations that observe parameter 11 (global irradiance)."""
    payload = fetch_json(PARAM_URL)
    stations = payload.get("station", [])
    out = []
    for s in stations:
        # 'active' means station still reporting; we include inactive ones too
        # since they may have long archives. We'll filter empty series later.
        out.append({
            "id": s.get("id"),
            "name": s.get("name"),
            "lat": s.get("latitude"),
            "lng": s.get("longitude"),
            "active": bool(s.get("active")),
        })
    return out


def fetch_station_series(station_id: int) -> list[dict] | None:
    """
    Fetch the corrected-archive CSV/JSON data series for a station.
    Returns list of {date: 'YYYY-MM-DD', value: float_W_per_m2} or None.

    SMHI serves both .json and .csv; JSON is cleaner but we use CSV because
    the JSON endpoint can be huge and sometimes missing value arrays. CSV is
    the stable format the browser example in the docs uses.
    """
    url = (
        f"{BASE}/parameter/{PARAMETER_ID}/station/{station_id}/"
        f"period/{PERIOD}/data.csv"
    )
    try:
        text = fetch_text(url)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
        print(f"  ! station {station_id}: HTTP error {e}", file=sys.stderr)
        return None

    # SMHI CSV has a multi-section preamble with blank separator rows.
    # The hourly irradiance section's header line is:
    #   "Datum;Tid (UTC);Global Irradians (svenska stationer);Kvalitet;;Tidsutsnitt:"
    # Followed by rows like:
    #   "1983-01-01;00:00:00;0.00;G"
    #
    # Older / daily formats may use "Representativt dygn;Värde;Kvalitet"
    # instead. We handle both.
    lines = text.splitlines()
    data_start = None
    date_col = None
    value_col = None
    for i, line in enumerate(lines):
        parts = [p.strip() for p in line.split(";")]
        if not parts:
            continue
        low = [p.lower() for p in parts]

        # Format A (hourly): Datum ; Tid (UTC) ; <parameter name> ; Kvalitet
        has_datum_col0 = len(low) > 0 and low[0] == "datum"
        has_tid_col1 = len(low) > 1 and low[1].startswith("tid")
        has_kvalitet = any(p == "kvalitet" for p in low)
        if has_datum_col0 and has_tid_col1 and has_kvalitet:
            date_col = 0
            # value column = the one that is neither "datum" nor starts with
            # "tid" nor equals "kvalitet" — it's usually index 2.
            for j, p in enumerate(low):
                if j in (0, 1):
                    continue
                if p == "kvalitet":
                    continue
                if p == "" or p.startswith("tidsutsnitt"):
                    continue
                value_col = j
                break
            if value_col is None:
                value_col = 2
            data_start = i + 1
            break

        # Format B (daily / aggregated): has "Värde"
        has_varde = any("värde" in p for p in low)
        has_dygn = any("representativt dygn" in p for p in low)
        if has_varde:
            for j, p in enumerate(low):
                if "representativt dygn" in p:
                    date_col = j
                    break
            if date_col is None and has_datum_col0:
                date_col = 0
            for j, p in enumerate(low):
                if p == "värde":
                    value_col = j
                    break
            if date_col is not None and value_col is not None:
                data_start = i + 1
                break

    if data_start is None or date_col is None or value_col is None:
        print(f"  ! station {station_id}: CSV header not recognised", file=sys.stderr)
        return None

    out = []
    for line in lines[data_start:]:
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(";")]
        if len(parts) <= max(date_col, value_col):
            continue
        d = parts[date_col]
        v = parts[value_col]
        if not d or not v:
            continue
        # date might be "YYYY-MM-DD" or "YYYY-MM-DD HH:MM:SS"
        d = d[:10]
        # The CSV's trailing "Tidsutsnitt" column leaks informational text
        # into the value column on early rows — skip anything non-numeric.
        try:
            val = float(v.replace(",", "."))
        except ValueError:
            continue
        out.append({"date": d, "value": val})
    return out


def aggregate_annual_kwh(series: list[dict], years_back: int = 3) -> float | None:
    """
    Convert observations to annual kWh/m^2/year averaged over the last N years.

    SMHI parameter 11 reports hourly mean global irradiance in W/m^2. The
    'corrected-archive' CSV reports them as one row per hour.

    For each hour:   energy[Wh/m^2] = value[W/m^2] * 1 hour
    Annual kWh/m^2 = sum(Wh/m^2 for year) / 1000
    """
    if not series:
        return None

    # Parse dates and take the most recent 3 full calendar years present.
    by_year_sum = {}
    by_year_count = {}
    for row in series:
        d = row["date"]
        try:
            year = int(d[:4])
        except ValueError:
            continue
        if row["value"] is None:
            continue
        by_year_sum[year] = by_year_sum.get(year, 0.0) + row["value"]
        by_year_count[year] = by_year_count.get(year, 0) + 1

    if not by_year_sum:
        return None

    # Pick the most recent `years_back` years with a reasonable number of
    # hourly readings (>=4000 hours of 8760 -> over half a year).
    candidate_years = sorted(by_year_sum.keys(), reverse=True)
    good_years = [y for y in candidate_years if by_year_count.get(y, 0) >= 4000]
    if not good_years:
        # looser threshold — still useful for average
        good_years = [y for y in candidate_years if by_year_count.get(y, 0) >= 2000]
    if not good_years:
        return None
    good_years = good_years[:years_back]

    # Sum of hourly W/m^2 * 1h = Wh/m^2 per year, /1000 = kWh/m^2
    # If readings are sparse, scale up to a full 8760-hour year.
    annuals = []
    for y in good_years:
        s = by_year_sum[y]
        n = by_year_count[y]
        # scale to full year if needed (linear extrapolation)
        scaled = s * (8760.0 / max(1, n))
        annuals.append(scaled / 1000.0)

    return round(statistics.mean(annuals), 1)


def run_live_scrape() -> list[dict]:
    """Try the live SMHI API. Returns list of station dicts, may be partial."""
    print(f"[smhi_scraper] Querying parameter list from {PARAM_URL}")
    stations_meta = list_stations_with_param()
    print(f"[smhi_scraper] API lists {len(stations_meta)} stations for param {PARAMETER_ID}")

    # Build a lookup so we can filter candidates to those the API says exist
    by_id = {s["id"]: s for s in stations_meta if s.get("id") is not None}
    candidates = []
    for sid in CANDIDATE_STATION_IDS:
        meta = by_id.get(sid)
        if meta:
            candidates.append(meta)
        else:
            print(f"[smhi_scraper] candidate {sid} not in parameter list, skipping")

    # If none of our candidates match, use the API's own list — take stations
    # spread across latitudes.
    if len(candidates) < 6 and stations_meta:
        print("[smhi_scraper] fallback: picking geographically spread stations from API list")
        sorted_by_lat = sorted(
            [s for s in stations_meta if s.get("lat") is not None],
            key=lambda s: s["lat"],
        )
        if sorted_by_lat:
            # pick ~12 evenly across the latitude range
            step = max(1, len(sorted_by_lat) // 12)
            candidates = sorted_by_lat[::step][:12]

    out = []
    for meta in candidates:
        sid = meta["id"]
        name = meta["name"]
        print(f"[smhi_scraper] fetching station {sid} ({name})…")
        series = fetch_station_series(sid)
        if not series:
            print(f"  ! no series for {sid}")
            continue
        kwh = aggregate_annual_kwh(series)
        if kwh is None:
            print(f"  ! could not aggregate {sid}")
            continue
        print(f"  ok {sid}: {kwh} kWh/m^2/year")
        out.append({
            "id": str(sid),
            "name": name,
            "lat": round(meta["lat"], 4),
            "lng": round(meta["lng"], 4),
            "annual_kwh_per_m2": kwh,
        })
    return out


def write_output(stations: list[dict], source_note: str) -> str:
    out_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "data",
        "smhi_irradiance.json",
    )
    out_path = os.path.normpath(out_path)
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "source": source_note,
        "parameter": f"SMHI metobs parameter {PARAMETER_ID} (global irradiance)",
        "stations": stations,
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return out_path


def main() -> int:
    stations: list[dict] = []
    source_note = ""
    try:
        stations = run_live_scrape()
    except Exception as e:  # noqa: BLE001 — report and fall back
        print(f"[smhi_scraper] live scrape failed: {e!r}", file=sys.stderr)

    if len(stations) >= 4:
        source_note = (
            f"SMHI Open Data — parameter {PARAMETER_ID} (global irradiance), "
            f"{PERIOD}, 3-year mean kWh/m^2/year"
        )
        out_path = write_output(stations, source_note)
        print(f"[smhi_scraper] wrote {len(stations)} station(s) -> {out_path}")
        return 0

    print(
        f"[smhi_scraper] only {len(stations)} station(s) succeeded; "
        f"falling back to latitude-zoned approximations",
        file=sys.stderr,
    )
    source_note = (
        "FALLBACK — latitude-zoned approximations for 12 Swedish cities; "
        "SMHI live scrape was unavailable or returned no usable data"
    )
    out_path = write_output(FALLBACK_CITIES, source_note)
    print(f"[smhi_scraper] wrote {len(FALLBACK_CITIES)} fallback stations -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
