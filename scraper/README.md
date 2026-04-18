# SMHI irradiance scraper

Fetches global irradiance (parameter 11) from the SMHI Meteorological
Observations open-data API, averages the last three years of hourly readings
into annual kWh/m² per station, and writes `data/smhi_irradiance.json`.

## How to re-run

From the repo root:

```
python3 scraper/smhi_scraper.py
```

Requires Python 3 only (standard library). No pip install, no venv.

The script:

1. Calls `https://opendata-download-metobs.smhi.se/api/version/1.0/parameter/11.json`
   to list stations that observe global irradiance.
2. For each candidate station (hardcoded list of ~14 geographically spread
   stations), downloads the `corrected-archive` CSV of hourly observations.
3. Sums hourly W/m² readings into annual kWh/m², picks the most recent three
   years that have ≥ 4000 hours of readings, and averages them.
4. Writes `data/smhi_irradiance.json` with `generated_at`, a `source` note
   (either "SMHI Open Data — parameter 11…" or a fallback label), and a
   `stations` array.

## Fallback behaviour

If the live SMHI API is unreachable or returns unexpected data, the script
writes the file anyway using latitude-zoned approximations for 12 major
Swedish cities. The `source` field makes it clear which path ran.

## SSL note

On stock macOS Python installs the system CA bundle may not be wired up and
you'll see `CERTIFICATE_VERIFY_FAILED`. The scraper detects this and
transparently retries with an unverified TLS context — we're reading public
meteorological data, so the risk is low. To fix it properly, run
`/Applications/Python\ 3.x/Install\ Certificates.command`.
