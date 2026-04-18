"""
nordpool_live.py
================

Long-running Nord Pool price scraper that produces a gateway-ready
JSON file describing the current hour's price and the next 12 hours'
prices for the chosen SE1-SE4 bidding zone.

Output: data/nordpool_live.json (atomically rewritten every 60 s).

The file is consumed by whatever downstream process speaks to the
Atech gateway — typically a small forwarder that reads the JSON and
POSTs or WebSockets it into gateway.atech.dev. Keeping the scraper
decoupled from Atech means the same JSON can also be:
  * polled by the HappyDays web UI at /data/nordpool_live.json
  * curl-tested, diffed, or manually forwarded
  * swapped for a mock file in integration tests

Data source: elprisetjustnu.se free public API (mirrors Nord Pool
day-ahead for SE1-SE4 as soon as they're published ~13:00 CET).
The API is 15-minute resolution in 2026; we aggregate to hourly means
before tiering, so the "next 12 hours" really is 12 hourly points.

Tier mapping is RELATIVE to the 12-hour window (cheapest tercile →
green, middle → yellow, most expensive → red). Useful framing for a
"when should I run appliances" traffic-light display.

Payload shape (v1):

    {
      "zone": "SE4",
      "generated_at": "2026-04-18T17:05:00+02:00",
      "stale": false,
      "source": "elprisetjustnu.se day-ahead, hourly means from 15-min slots",
      "current": {
        "hour_start": "2026-04-18T17:00:00+02:00",
        "price_ore": 75.3,
        "tier": "yellow",
        "color_hex": "#ffc800"
      },
      "next_12h": [
        {"hour_start": "...", "price_ore": ..., "tier": "...", "color_hex": "..."},
        ...
      ],
      "window_stats": {
        "min_ore": ..., "max_ore": ..., "mean_ore": ..., "n_hours": 12,
        "tier_low_cutoff_ore": ..., "tier_high_cutoff_ore": ...
      }
    }

Config via env:
  HAPPYDAYS_ZONE     default "SE4" — one of SE1|SE2|SE3|SE4
  POLL_SECONDS       default 60   — how often to rewrite the JSON file
  FETCH_SECONDS      default 900  — how often to re-hit elprisetjustnu
  DRY_RUN            if "1", log but don't write to disk
  OUTPUT_PATH        override output file location (absolute path)

Run:
  python3 scraper/nordpool_live.py
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ELPRIS_BASE = "https://www.elprisetjustnu.se/api/v1/prices"
DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "data" / "nordpool_live.json"

# Relative-tier color palette. Traffic-light framing: green = cheap / run
# appliances now, yellow = neutral, red = hold off if you can.
COLOR_GREEN = "#00c850"
COLOR_YELLOW = "#ffc800"
COLOR_RED = "#ff3c30"


# ---------------------------------------------------------------------------
# HTTP helper (stdlib-only, shared SSL fallback with the other scrapers)
# ---------------------------------------------------------------------------


def _urlopen(req: urllib.request.Request, timeout: int = 20):
    """
    Open a URL, retrying once with an unverified SSL context if the system
    CA bundle isn't wired up (common on stock macOS Python).
    """
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.URLError as e:
        if not isinstance(e.reason, ssl.SSLError):
            raise
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return urllib.request.urlopen(req, timeout=timeout, context=ctx)


def fetch_day_prices(zone: str, day: datetime) -> list[dict]:
    """
    Return the list of price rows for the given calendar day. Each row
    is a 15-min (or hourly) slot from elprisetjustnu.se. Raises HTTPError
    404 when the day isn't published yet (tomorrow before ~13:00 CET).
    """
    url = f"{ELPRIS_BASE}/{day.year:04d}/{day.month:02d}-{day.day:02d}_{zone}.json"
    req = urllib.request.Request(url, headers={"User-Agent": "HappyDays-live/1.0"})
    with _urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Price-state computation
# ---------------------------------------------------------------------------


def hour_floor(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def parse_row(row: dict) -> dict:
    """Normalise an elprisetjustnu row into {hour_start, hour_end, price_ore}."""
    return {
        "start": datetime.fromisoformat(row["time_start"]),
        "end": datetime.fromisoformat(row["time_end"]),
        "price_ore": float(row["SEK_per_kWh"]) * 100.0,  # SEK/kWh → öre/kWh
    }


def aggregate_to_hourly(rows: list[dict]) -> list[dict]:
    """
    Group sub-hourly rows by hour (flooring `start`) and compute the mean
    price for each hour. Preserves chronological order. If the source is
    already hourly, the aggregation is a no-op.
    """
    buckets: dict[datetime, list[float]] = {}
    order: list[datetime] = []
    for r in rows:
        hb = hour_floor(r["start"])
        if hb not in buckets:
            buckets[hb] = []
            order.append(hb)
        buckets[hb].append(r["price_ore"])
    hourly: list[dict] = []
    for hb in order:
        prices = buckets[hb]
        hourly.append({
            "hour_start": hb,
            "price_ore": round(sum(prices) / len(prices), 2),
        })
    return hourly


def compute_cutoffs(prices: list[float]) -> tuple[float, float]:
    """
    Split a price list into terciles by SORTED value. Everything <=
    low_cutoff is green, between the two is yellow, rest is red.
    """
    if not prices:
        return (0.0, 0.0)
    s = sorted(prices)
    n = len(s)
    lo_idx = max(0, (n // 3) - 1)
    hi_idx = max(lo_idx, (2 * n // 3) - 1)
    return (s[lo_idx], s[hi_idx])


def tier_for(price_ore: float, low_cutoff: float, high_cutoff: float) -> str:
    if price_ore <= low_cutoff:
        return "green"
    if price_ore <= high_cutoff:
        return "yellow"
    return "red"


def color_for(tier: str) -> str:
    return {"green": COLOR_GREEN, "yellow": COLOR_YELLOW, "red": COLOR_RED}[tier]


def build_payload(zone: str, now: datetime, hourly_window: list[dict], stale: bool) -> dict:
    """
    Build the gateway-ready payload. `hourly_window` is a list of
    hourly means (parse_row → aggregate_to_hourly) covering the current
    hour plus the next 11 hours (fewer if tomorrow isn't published yet).
    """
    prices = [r["price_ore"] for r in hourly_window]
    low_cut, high_cut = compute_cutoffs(prices)

    def decorate(row: dict) -> dict:
        t = tier_for(row["price_ore"], low_cut, high_cut)
        return {
            "hour_start": row["hour_start"].isoformat(),
            "price_ore": row["price_ore"],
            "tier": t,
            "color_hex": color_for(t),
        }

    decorated = [decorate(r) for r in hourly_window]
    current = decorated[0] if decorated else None

    stats = None
    if prices:
        stats = {
            "min_ore": round(min(prices), 2),
            "max_ore": round(max(prices), 2),
            "mean_ore": round(sum(prices) / len(prices), 2),
            "n_hours": len(prices),
            "tier_low_cutoff_ore": round(low_cut, 2),
            "tier_high_cutoff_ore": round(high_cut, 2),
        }

    return {
        "zone": zone,
        "generated_at": now.astimezone().isoformat(timespec="seconds"),
        "stale": stale,
        "source": "elprisetjustnu.se day-ahead, hourly means from 15-min slots",
        "current": current,
        "next_12h": decorated,
        "window_stats": stats,
    }


# ---------------------------------------------------------------------------
# Cache + main loop
# ---------------------------------------------------------------------------


class PriceCache:
    """
    In-memory cache of per-day parsed rows so we don't hammer
    elprisetjustnu every poll. `refetch_seconds` is the staleness window.
    """

    def __init__(self, zone: str) -> None:
        self.zone = zone
        self._rows: dict[str, list[dict]] = {}
        self._fetched_at: dict[str, float] = {}
        self._missing_until: dict[str, float] = {}

    def _day_key(self, day: datetime) -> str:
        return f"{day.year:04d}-{day.month:02d}-{day.day:02d}"

    def get_rows(self, day: datetime, *, refetch_seconds: int) -> list[dict]:
        key = self._day_key(day)
        now_mono = time.monotonic()
        if key in self._missing_until and self._missing_until[key] > now_mono:
            return []
        cached = self._rows.get(key)
        fetched_at = self._fetched_at.get(key, 0.0)
        if cached is not None and (now_mono - fetched_at) < refetch_seconds:
            return cached
        try:
            raw = fetch_day_prices(self.zone, day)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                self._missing_until[key] = now_mono + 30 * 60
                return []
            print(f"[warn] fetch {key} HTTP {e.code}: {e}", file=sys.stderr)
            return cached or []
        except urllib.error.URLError as e:
            print(f"[warn] fetch {key} network error: {e}", file=sys.stderr)
            return cached or []
        rows = [parse_row(r) for r in raw]
        self._rows[key] = rows
        self._fetched_at[key] = now_mono
        self._missing_until.pop(key, None)
        return rows


def assemble_hourly_window(cache: PriceCache, now: datetime, *, refetch_seconds: int) -> list[dict]:
    """
    Return up to 12 hourly-aggregated rows, starting from the current
    hour, pulling across today and (if needed) tomorrow.
    """
    hour = hour_floor(now)
    today = cache.get_rows(now, refetch_seconds=refetch_seconds)
    tomorrow = cache.get_rows(now + timedelta(days=1), refetch_seconds=refetch_seconds)
    hourly = aggregate_to_hourly([*today, *tomorrow])
    ahead = [r for r in hourly if r["hour_start"] >= hour]
    return ahead[:12]


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


def write_json_atomic(path: Path, payload: dict) -> None:
    """
    Serialize to sibling .tmp, fsync, then os.replace. If the process is
    killed mid-write we keep the previous good JSON instead of leaving a
    truncated file that a consumer might parse halfway.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_forever(
    *,
    zones: list[str],
    output_path: Path,
    poll_seconds: int,
    fetch_seconds: int,
    dry_run: bool,
) -> None:
    caches = {z: PriceCache(z) for z in zones}
    print(
        f"[nordpool_live] zones={','.join(zones)} out={output_path} "
        f"poll={poll_seconds}s fetch={fetch_seconds}s dry_run={dry_run}",
        flush=True,
    )
    while True:
        now = datetime.now(timezone.utc).astimezone()
        zone_blocks: dict[str, dict] = {}
        log_bits: list[str] = []
        for z in zones:
            window = assemble_hourly_window(
                caches[z], now, refetch_seconds=fetch_seconds
            )
            stale = not window or window[0]["hour_start"] != hour_floor(now)
            zone_blocks[z] = build_payload(z, now, window, stale)
            cur = zone_blocks[z]["current"]
            log_bits.append(
                f"{z}={cur['price_ore']:.1f}ö/{cur['tier']}" if cur else f"{z}=n/a"
            )

        combined = {
            "generated_at": now.astimezone().isoformat(timespec="seconds"),
            "source": "elprisetjustnu.se day-ahead, hourly means from 15-min slots",
            "zones": zone_blocks,
        }

        print(
            f"[{now.isoformat(timespec='seconds')}] " + " ".join(log_bits),
            flush=True,
        )

        if not dry_run:
            try:
                write_json_atomic(output_path, combined)
            except OSError as e:
                print(f"  [warn] write failed: {e}", file=sys.stderr)

        time.sleep(poll_seconds)


def main() -> int:
    # HAPPYDAYS_ZONES (plural) takes precedence; falls back to HAPPYDAYS_ZONE
    # (legacy single-zone) or all four zones by default.
    all_zones = ["SE1", "SE2", "SE3", "SE4"]
    raw_plural = os.environ.get("HAPPYDAYS_ZONES", "").strip()
    raw_single = os.environ.get("HAPPYDAYS_ZONE", "").strip()
    if raw_plural:
        zones = [z.strip().upper() for z in raw_plural.split(",") if z.strip()]
    elif raw_single:
        zones = [raw_single.upper()]
    else:
        zones = list(all_zones)

    poll = int(os.environ.get("POLL_SECONDS", "60"))
    fetch = int(os.environ.get("FETCH_SECONDS", "900"))
    dry_run = os.environ.get("DRY_RUN", "").strip() == "1"
    output_override = os.environ.get("OUTPUT_PATH", "").strip()
    output_path = Path(output_override) if output_override else DEFAULT_OUTPUT

    bad = [z for z in zones if z not in set(all_zones)]
    if bad or not zones:
        print(
            f"error: zones must be a subset of SE1-SE4, got {zones!r}",
            file=sys.stderr,
        )
        return 2

    try:
        run_forever(
            zones=zones,
            output_path=output_path,
            poll_seconds=poll,
            fetch_seconds=fetch,
            dry_run=dry_run,
        )
    except KeyboardInterrupt:
        print("\n[nordpool_live] stopped", flush=True)
        return 0
    return 0



if __name__ == "__main__":
    sys.exit(main())
