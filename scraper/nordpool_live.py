"""
nordpool_live.py
================

Long-running Nord Pool price scraper that produces a gateway-ready
JSON file describing the current hour's price and the next 12 hours'
prices for the chosen SE1-SE4 bidding zone.

Outputs (both atomically rewritten every POLL_SECONDS):

  data/nordpool_live.json     — rich payload for the HappyDays web UI
                                (current + next 12 h, tiers, colors)

  data/gateway_actions.json   — Atech-firmware-shaped actions:
                                {generated_at, zone, stale, actions: [
                                  {"action":"set_prices",       "value":"12.1,14.3,…"},
                                  {"action":"set_current_price","value":"0.23"},
                                  {"action":"set_hour",         "value":"14"}
                                ]}

The files are consumed by whatever downstream process speaks to the
Atech gateway — typically a small forwarder that reads one of the JSON
files and POSTs (or WebSocket-sends) its contents into
gateway.atech.dev. Keeping the scraper decoupled from Atech means the
same JSON can also be polled by the web UI, curl-tested, diffed, or
swapped for a mock file in integration tests.

Units for gateway_actions.json / gateway_commands.txt:
  set_current_price : öre/kWh, 1 decimal (e.g. "32.7")
                      — matches what Atech's firmware expects per its own
                      confirmation ("current hour price in ore/kWh").
  set_prices        : öre/kWh, 1 decimal, comma-separated with NO spaces,
                      always exactly 24 values (00:00–23:00 Europe/Stockholm).
                      Extra whitespace inside `value` has been reported to
                      confuse the firmware, so don't add any.
  set_hour          : integer hour 0–23 in Europe/Stockholm local time.

Two output shapes are produced side by side:
  data/gateway_actions.json   — structured wrapper with metadata (zone,
                                generated_at, stale, source, actions[]).
                                Use this if you're writing a forwarder
                                that consumes the whole snapshot.
  data/gateway_commands.txt   — three raw single-line JSON objects,
                                one per line, no wrapper. Paste any one
                                line directly into the Atech dashboard
                                input field.

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
  HAPPYDAYS_ZONE     default "SE4"    — one of SE1|SE2|SE3|SE4. Lund is SE4.
  POLL_SECONDS       default 60       — how often to rewrite the JSON files.
                                        (Recomputes set_hour from cached prices.)
  FETCH_SECONDS      default 21600    — how often to re-hit elprisetjustnu.se.
                                        6 h is plenty: day-ahead is published
                                        once at ~13:00 CET and is static after.
  DRY_RUN            if "1", log but don't write to disk
  OUTPUT_DIR         override output directory (default: data/ in repo root)

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
from zoneinfo import ZoneInfo

ELPRIS_BASE = "https://www.elprisetjustnu.se/api/v1/prices"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
NORDPOOL_LIVE_FILENAME = "nordpool_live.json"
GATEWAY_ACTIONS_FILENAME = "gateway_actions.json"
GATEWAY_COMMANDS_FILENAME = "gateway_commands.txt"
# Nord Pool SE1-SE4 bidding zones are aligned to Swedish local time; the
# elprisetjustnu.se URL uses Swedish-local calendar dates. We pin explicitly
# so the script produces correct output from any host timezone.
STOCKHOLM = ZoneInfo("Europe/Stockholm")

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


def assemble_today_hourly(cache: PriceCache, now_sthlm: datetime, *, refetch_seconds: int) -> list[dict]:
    """
    Return today's 24 hourly-aggregated rows (00:00–23:00 local Swedish
    time). May be fewer than 24 if the API call failed; caller decides
    how to react.
    """
    today_rows = cache.get_rows(now_sthlm, refetch_seconds=refetch_seconds)
    hourly = aggregate_to_hourly(today_rows)
    # Filter to hours whose start date equals today's Swedish date (belt +
    # braces — elprisetjustnu returns only that day anyway).
    today_date = now_sthlm.date()
    return [r for r in hourly if r["hour_start"].astimezone(STOCKHOLM).date() == today_date]


def find_current_row(rows: list[dict], now_aware: datetime) -> dict | None:
    """Row whose [hour_start, hour_start+1h) contains `now_aware`."""
    for r in rows:
        start = r["hour_start"]
        end = start + timedelta(hours=1)
        if start <= now_aware < end:
            return r
    return None


# ---------------------------------------------------------------------------
# Gateway-actions payload (matches Atech firmware's expected JSON shape)
# ---------------------------------------------------------------------------


def build_gateway_actions(
    zone: str,
    now_sthlm: datetime,
    today_hourly: list[dict],
    current_row: dict | None,
) -> dict:
    """
    Emit the firmware-shaped payload. Only includes set_prices when we
    have all 24 hours — a partial array would confuse the chart on the
    device. set_current_price and set_hour are always included when we
    can compute them from the available rows.
    """
    actions: list[dict] = []

    if len(today_hourly) == 24:
        # öre/kWh, 1 decimal, comma-separated, exactly 24 values. No spaces
        # in the `value` string — Atech's firmware parses a plain list.
        prices_str = ",".join(f"{r['price_ore']:.1f}" for r in today_hourly)
        actions.append({"action": "set_prices", "value": prices_str})

    if current_row is not None:
        # öre/kWh, 1 decimal — same units as the array, per Atech's
        # confirmation ("current hour price in ore/kWh"). An earlier
        # draft emitted SEK/kWh here; that shape was inconsistent with
        # set_prices and didn't match the firmware's parser.
        actions.append({
            "action": "set_current_price",
            "value": f"{current_row['price_ore']:.1f}",
        })
        actions.append({"action": "set_hour", "value": str(now_sthlm.hour)})

    # Rich stats for forwarders / debugging — the firmware ignores everything
    # outside `actions`, but a human diffing the file can see what's happening.
    return {
        "zone": zone,
        "generated_at": now_sthlm.isoformat(timespec="seconds"),
        "stale": len(today_hourly) < 24 or current_row is None,
        "source": "elprisetjustnu.se day-ahead, hourly means from 15-min slots",
        "actions": actions,
    }


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


def _write_atomic(path: Path, data: bytes) -> None:
    """tmp + fsync + os.replace. Consumers never see a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def write_json_atomic(path: Path, payload: dict) -> None:
    _write_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))


def write_commands_txt(path: Path, actions: list[dict]) -> None:
    """
    Emit one JSON object per line, no wrapper. Ready to paste directly
    into the Atech gateway input field — grab any single line, send it.
    """
    lines = [json.dumps(a, ensure_ascii=False, separators=(", ", ": ")) for a in actions]
    body = ("\n".join(lines) + "\n").encode("utf-8")
    _write_atomic(path, body)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_forever(
    *,
    zone: str,
    output_dir: Path,
    poll_seconds: int,
    fetch_seconds: int,
    dry_run: bool,
) -> None:
    cache = PriceCache(zone)
    live_path = output_dir / NORDPOOL_LIVE_FILENAME
    actions_path = output_dir / GATEWAY_ACTIONS_FILENAME
    commands_path = output_dir / GATEWAY_COMMANDS_FILENAME
    print(
        f"[nordpool_live] zone={zone} dir={output_dir} poll={poll_seconds}s "
        f"fetch={fetch_seconds}s dry_run={dry_run}",
        flush=True,
    )
    while True:
        now_sthlm = datetime.now(tz=STOCKHOLM)

        # Website payload: current hour + next 12 h, tiered green/yellow/red.
        window = assemble_hourly_window(cache, now_sthlm, refetch_seconds=fetch_seconds)
        live_stale = not window or window[0]["hour_start"] != hour_floor(now_sthlm)
        live_payload = build_payload(zone, now_sthlm, window, live_stale)

        # Gateway payload: today's 24 hourly öre values + current hour + current SEK price.
        today_hourly = assemble_today_hourly(cache, now_sthlm, refetch_seconds=fetch_seconds)
        current_row = find_current_row(today_hourly, now_sthlm)
        actions_payload = build_gateway_actions(zone, now_sthlm, today_hourly, current_row)

        cur = live_payload["current"]
        cur_str = f"{cur['price_ore']:.1f} öre ({cur['tier']})" if cur else "n/a"
        print(
            f"[{now_sthlm.isoformat(timespec='seconds')}] current={cur_str} "
            f"window={len(window)}h today={len(today_hourly)}h "
            f"actions={len(actions_payload['actions'])}",
            flush=True,
        )

        if not dry_run:
            try:
                write_json_atomic(live_path, live_payload)
                write_json_atomic(actions_path, actions_payload)
                write_commands_txt(commands_path, actions_payload["actions"])
            except OSError as e:
                print(f"  [warn] write failed: {e}", file=sys.stderr)

        time.sleep(poll_seconds)


def main() -> int:
    zone = os.environ.get("HAPPYDAYS_ZONE", "SE4").strip().upper()
    poll = int(os.environ.get("POLL_SECONDS", "60"))
    fetch = int(os.environ.get("FETCH_SECONDS", "21600"))  # 6 h
    dry_run = os.environ.get("DRY_RUN", "").strip() == "1"
    output_override = os.environ.get("OUTPUT_DIR", "").strip()
    output_dir = Path(output_override) if output_override else DATA_DIR

    if zone not in {"SE1", "SE2", "SE3", "SE4"}:
        print(f"error: HAPPYDAYS_ZONE must be SE1-SE4, got {zone!r}", file=sys.stderr)
        return 2

    try:
        run_forever(
            zone=zone,
            output_dir=output_dir,
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
