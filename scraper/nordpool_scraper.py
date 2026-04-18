"""
nordpool_scraper.py
===================

Refreshes data/electricity_zones.json from SCB's official Elprisstatistik
table SSDManadElhandelpris (Elhandelspriser på elenergi exkl. elskatt,
moms och nätavgift, per avtalstyp, elområde och kundkategori, månad).

Endpoint: the pxweb 1.x API under EN0301A. Public, no auth.

We query the 36 months 2022M01 .. 2024M12 for all four bidding zones
(SE1-SE4), contract type "rörligt" (variable/spot-tracking) and
customer category 3 (one- or two-dwelling house with electric heating —
the typical solar candidate). We then average each zone over those 36
months to get a three-year mean in öre/kWh.

Stdlib only so it mirrors scraper/smhi_scraper.py and runs without
`pip install`. Run from the repo root:

    python3 scraper/nordpool_scraper.py
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

API_URL = (
    "https://api.scb.se/OV0104/v1/doris/en/ssd/START/EN/"
    "EN0301/EN0301A/SSDManadElhandelpris"
)

ZONES = ["SE1", "SE2", "SE3", "SE4"]
ZONE_NAMES = {
    "SE1": "Luleå (Norra Sverige)",
    "SE2": "Sundsvall (Norra Mellansverige)",
    "SE3": "Stockholm (Södra Mellansverige)",
    "SE4": "Malmö (Södra Sverige)",
}

START_YEAR = 2022
END_YEAR = 2024
CONTRACT_TYPE = "rorligt"        # spot-tracking variable-price contract
CUSTOMER_CATEGORY = "3"          # one/two-dwelling house with electric heating


def build_months(start_year: int, end_year: int) -> list[str]:
    out: list[str] = []
    for y in range(start_year, end_year + 1):
        for m in range(1, 13):
            out.append(f"{y}M{m:02d}")
    return out


def fetch_prices() -> dict[str, list[float]]:
    """
    Returns { zone: [price_per_month_öre, ...] } sorted chronologically.
    """
    months = build_months(START_YEAR, END_YEAR)
    query = {
        "query": [
            {"code": "Avtalstyp", "selection": {"filter": "item", "values": [CONTRACT_TYPE]}},
            {"code": "Elomrade", "selection": {"filter": "item", "values": ZONES}},
            {"code": "Kundkategori", "selection": {"filter": "item", "values": [CUSTOMER_CATEGORY]}},
            {"code": "Tid", "selection": {"filter": "item", "values": months}},
        ],
        "response": {"format": "json"},
    }
    body = json.dumps(query).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "HappyDays-scraper/1.0",
        },
    )
    # Stock macOS Python often ships without a wired-up CA bundle; if verify
    # fails, fall back to unverified context (SCB data is public).
    try:
        resp = urllib.request.urlopen(req, timeout=30)
    except urllib.error.URLError as e:
        if not isinstance(e.reason, ssl.SSLError):
            raise
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        resp = urllib.request.urlopen(req, timeout=30, context=ctx)
    with resp:
        payload = json.loads(resp.read().decode("utf-8"))

    # Flatten `data` list into { zone: { month: price } } first so we can
    # surface any month gaps explicitly instead of silently drifting.
    by_zone: dict[str, dict[str, float]] = {z: {} for z in ZONES}
    for row in payload.get("data", []):
        # key order matches the `query` order: [Avtalstyp, Elomrade, Kundkategori, Tid]
        _avtal, zone, _kund, month = row["key"]
        vals = row.get("values", [])
        if not vals:
            continue
        raw = vals[0]
        # SCB uses "." for missing observations; skip them.
        if raw in (".", "", None):
            continue
        try:
            by_zone[zone][month] = float(raw)
        except ValueError:
            continue

    # Now emit in chronological order per zone, dropping gaps quietly.
    ordered: dict[str, list[float]] = {}
    for z in ZONES:
        ordered[z] = [by_zone[z][m] for m in months if m in by_zone[z]]
    return ordered


def compute_zone_means(by_zone: dict[str, list[float]]) -> dict[str, dict]:
    zones_out: dict[str, dict] = {}
    for z in ZONES:
        series = by_zone.get(z, [])
        if not series:
            print(f"  [{z}] no data", file=sys.stderr)
            continue
        mean = sum(series) / len(series)
        zones_out[z] = {
            "name": ZONE_NAMES[z],
            "avg_ore_per_kwh": round(mean, 1),
            "source": (
                "SCB Elprisstatistik table SSDManadElhandelpris, avtalstyp "
                "'rörligt', kundkategori 3 (enbostadshus med elvärme), "
                f"{START_YEAR}M01–{END_YEAR}M12 ({len(series)}-month mean). "
                "Excludes elskatt, moms and nätavgift. Tracks Nord Pool "
                "day-ahead prices closely (rörligt contracts settle on spot)."
            ),
            "n_months": len(series),
            "min_ore_per_kwh": round(min(series), 1),
            "max_ore_per_kwh": round(max(series), 1),
        }
        print(f"  [{z}] mean={mean:5.1f} öre/kWh  n={len(series)}  "
              f"range={min(series):.1f}..{max(series):.1f}")
    return zones_out


def write_zones_json(zones: dict[str, dict], path: Path) -> None:
    doc = {
        "generated_at": date.today().isoformat(),
        "source_note": (
            f"Three-year means ({START_YEAR}–{END_YEAR}) of monthly "
            "variable-rate household electricity prices per elområde, "
            "from SCB Elprisstatistik table SSDManadElhandelpris. "
            "Prices exclude elskatt, moms and nätavgift, so they are "
            "comparable to Nord Pool spot + retailer margin; the full "
            "household bill is typically 60–80 öre/kWh higher once "
            "taxes and network fees are added. "
            "Refresh by re-running scraper/nordpool_scraper.py from the "
            "repo root."
        ),
        "zone_boundary_simplification": (
            "Zone assignment from latitude is a rough north-to-south slice: "
            "SE1 lat ≥ 65, SE2 61 ≤ lat < 65, SE3 57 ≤ lat < 61, "
            "SE4 lat < 57. Real Svenska kraftnät boundaries follow grid "
            "topology, not parallels, so coastal towns near a boundary may "
            "be misclassified."
        ),
        "zones": zones,
    }
    # Atomic write: serialize to a sibling tmp file, fsync, then rename. If
    # the process is killed mid-write we keep the previous good JSON instead
    # of leaving a truncated file that falls through to the default grid rate.
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8")
    with open(tmp_path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)
    print(f"\nWrote {path} ({sum(z['n_months'] for z in zones.values())} monthly obs across "
          f"{len(zones)} zones)")


def main() -> int:
    print(f"Fetching SCB rörligt-contract prices for {START_YEAR}-{END_YEAR}, SE1-SE4 ...")
    try:
        by_zone = fetch_prices()
    except urllib.error.URLError as e:
        print(f"  ERROR: could not reach SCB API: {e}", file=sys.stderr)
        return 1
    zones = compute_zone_means(by_zone)
    if len(zones) != 4:
        print("  ERROR: did not receive all four zones; aborting write.", file=sys.stderr)
        return 2
    out_path = Path(__file__).resolve().parent.parent / "data" / "electricity_zones.json"
    write_zones_json(zones, out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
