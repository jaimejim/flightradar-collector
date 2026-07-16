#!/usr/bin/env python3
"""Forward ADS-B collector for flightradar.jaime.win.

Fetches the current aircraft snapshot from a free community ADS-B API and POSTs
it to the Worker's /api/ingest endpoint, which stores it in Cloudflare D1.

Runs on GitHub Actions (clean runner IP — the community feeds throttle
Cloudflare's shared Worker IPs, but not GitHub's). No laptop involved.

Env vars:
  INGEST_URL    (default https://flightradar.jaime.win/api/ingest)
  INGEST_TOKEN  (required — set as a GitHub Actions secret)
"""
import json
import os
import subprocess
import sys
import urllib.request

INGEST_URL = os.environ.get("INGEST_URL", "https://flightradar.jaime.win/api/ingest")

# Centre of the collection area (Soukanlahti <-> EFHK) + radius in nautical mi.
LAT, LON, DIST = 60.225, 24.85, 45

# Free, no-key community ADS-B sources (readsb schema). Tried in order.
SOURCES = [
    f"https://api.airplanes.live/v2/point/{LAT}/{LON}/{DIST}",
    f"https://opendata.adsb.fi/api/v2/lat/{LAT}/lon/{LON}/dist/{DIST}",
    f"https://api.adsb.lol/v2/point/{LAT}/{LON}/{DIST}",
]
UA = {"User-Agent": "flightradar-collector/1.0 (github actions; personal)"}


def fetch_snapshot():
    """Return (aircraft_list, now_ms) from the first source that answers."""
    last = "no sources"
    for url in SOURCES:
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.load(r)
            ac = d.get("aircraft") or d.get("ac") or []
            print(f"fetched {len(ac)} aircraft from {url.split('/')[2]}")
            return ac, d.get("now")
        except Exception as e:
            last = f"{url.split('/')[2]}: {e}"
            print(f"  source failed: {last}")
    raise RuntimeError(f"all sources failed ({last})")


def push(ac, now, token):
    # Push via curl, not urllib: Cloudflare's bot protection returns 403 to
    # Python-urllib's request fingerprint, but lets curl through.
    body = json.dumps({"aircraft": ac, "now": now})
    proc = subprocess.run(
        ["curl", "-sS", "-X", "POST", INGEST_URL,
         "-H", "Content-Type: application/json",
         "-H", f"Authorization: Bearer {token}",
         "--data-binary", "@-"],
        input=body, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"curl failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def main():
    token = os.environ.get("INGEST_TOKEN", "").strip()
    if not token:
        print("ERROR: INGEST_TOKEN env var not set", file=sys.stderr)
        sys.exit(1)
    ac, now = fetch_snapshot()
    res = push(ac, now, token)
    print(f"ingested: {res.get('ingested', res)}")


if __name__ == "__main__":
    main()
