#!/usr/bin/env python3
"""Forward ADS-B collector for flightradar.jaime.win.

Fetches the current aircraft snapshot from free community ADS-B APIs (and,
optionally, OpenSky as a complementary source) and POSTs it to the Worker's
/api/ingest endpoint, which stores it in Cloudflare D1.

Runs on GitHub Actions (clean runner IP -- the community feeds and OpenSky
throttle Cloudflare's shared Worker IPs, but not GitHub's). No laptop involved.

To get near-real-time density without hammering any single source, each run
*loops*: it polls every INTERVAL_SECONDS for LOOP_SECONDS total, pushing each
snapshot. The Worker dedups on (icao24, time_pos), so re-pushing an unchanged
position is a no-op. Set LOOP_SECONDS=0 for a single snapshot (legacy mode).

Env vars:
  INGEST_URL            (default https://flightradar.jaime.win/api/ingest)
  INGEST_TOKEN          (required -- set as a GitHub Actions secret)
  LOOP_SECONDS          (default 270 -- total wall time to keep polling)
  INTERVAL_SECONDS      (default 25  -- gap between polls; OpenSky-safe)
  OPENSKY_CLIENT_ID     (optional -- enables OpenSky as a complementary source)
  OPENSKY_CLIENT_SECRET (optional -- OAuth2 client secret)
"""
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

INGEST_URL = os.environ.get("INGEST_URL", "https://flightradar.jaime.win/api/ingest")
LOOP_SECONDS = int(os.environ.get("LOOP_SECONDS", "270"))
INTERVAL_SECONDS = int(os.environ.get("INTERVAL_SECONDS", "25"))

# Centre of the collection area (Soukanlahti <-> EFHK) + radius in nautical mi.
LAT, LON, DIST = 60.225, 24.85, 45

# OpenSky wants a lat/lon bounding box (mirror the Worker's region + margin).
LAMIN, LAMAX, LOMIN, LOMAX = 59.90, 60.55, 24.30, 25.40

# Free, no-key community ADS-B sources (readsb schema). Tried in order.
SOURCES = [
    f"https://api.airplanes.live/v2/point/{LAT}/{LON}/{DIST}",
    f"https://opendata.adsb.fi/api/v2/lat/{LAT}/lon/{LON}/dist/{DIST}",
    f"https://api.adsb.lol/v2/point/{LAT}/{LON}/{DIST}",
]
UA = {"User-Agent": "flightradar-collector/2.0 (github actions; personal)"}

# --- Unit conversions: the Worker's ingest expects readsb units (feet, knots,
# --- ft/min); OpenSky reports metric (metres, m/s), so convert on the way in.
M_TO_FT = 3.28084
MS_TO_KT = 1.0 / 0.514444
MS_TO_FTMIN = 1.0 / 0.00508

OPENSKY_STATES_URL = "https://opensky-network.org/api/states/all"
OPENSKY_TOKEN_URL = ("https://auth.opensky-network.org/auth/realms/"
                     "opensky-network/protocol/openid-connect/token")
# OpenSky /states/all row indices (see their REST docs).
S_ICAO24, S_CALLSIGN, S_TIMEPOS = 0, 1, 3
S_LON, S_LAT, S_BARO_ALT, S_ONGROUND, S_VELOCITY = 5, 6, 7, 8, 9
S_TRACK, S_VRATE, S_GEO_ALT = 10, 11, 13


def fetch_community():
    """Return (aircraft_list, now_s) from the first community source that answers."""
    last = "no sources"
    for url in SOURCES:
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.load(r)
            ac = d.get("aircraft") or d.get("ac") or []
            now = d.get("now")
            # adsb.lol reports `now` in ms, adsb.fi in seconds -- normalise.
            if now and now > 1e12:
                now = now / 1000.0
            print(f"  community: {len(ac)} aircraft from {url.split('/')[2]}")
            return ac, now
        except Exception as e:
            last = f"{url.split('/')[2]}: {e}"
            print(f"  community source failed: {last}")
    print(f"  all community sources failed ({last})")
    return [], None


def opensky_token():
    """Exchange client credentials for a bearer token, or None if not configured."""
    cid = os.environ.get("OPENSKY_CLIENT_ID", "").strip()
    secret = os.environ.get("OPENSKY_CLIENT_SECRET", "").strip()
    if not cid or not secret:
        return None
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": cid,
        "client_secret": secret,
    }).encode()
    try:
        req = urllib.request.Request(OPENSKY_TOKEN_URL, data=data)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)["access_token"]
    except Exception as e:
        print(f"  opensky token failed: {e}")
        return None


def fetch_opensky(token):
    """Fetch OpenSky states in-region and map them to readsb-format dicts."""
    params = urllib.parse.urlencode({
        "lamin": LAMIN, "lamax": LAMAX, "lomin": LOMIN, "lomax": LOMAX,
    })
    req = urllib.request.Request(OPENSKY_STATES_URL + "?" + params, headers={
        **UA, "Authorization": f"Bearer {token}",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
    except Exception as e:
        print(f"  opensky fetch failed: {e}")
        return [], None
    now = d.get("time")
    out = []
    for s in d.get("states") or []:
        lat, lon = s[S_LAT], s[S_LON]
        if lat is None or lon is None:
            continue
        on_ground = bool(s[S_ONGROUND])
        baro_m, geo_m = s[S_BARO_ALT], s[S_GEO_ALT]
        vel, vrate = s[S_VELOCITY], s[S_VRATE]
        tpos = s[S_TIMEPOS]
        out.append({
            "hex": (s[S_ICAO24] or "").strip(),
            "flight": (s[S_CALLSIGN] or "").strip(),
            "lat": lat, "lon": lon,
            "alt_baro": "ground" if on_ground
                        else (baro_m * M_TO_FT if baro_m is not None else None),
            "alt_geom": geo_m * M_TO_FT if geo_m is not None else None,
            "gs": vel * MS_TO_KT if vel is not None else None,
            "track": s[S_TRACK],
            "baro_rate": vrate * MS_TO_FTMIN if vrate is not None else None,
            # seconds since this position was seen (Worker subtracts from `now`).
            "seen_pos": max(0, (now - tpos)) if (now and tpos) else 0,
        })
    print(f"  opensky: {len(out)} aircraft")
    return out, now


def merge(community, opensky):
    """Union by hex; community feed wins (fresher, local receivers)."""
    by_hex = {}
    for a in opensky:                       # lay OpenSky down first...
        h = (a.get("hex") or "").strip().lower()
        if h:
            by_hex[h] = a
    for a in community:                     # ...then let community overwrite.
        h = (a.get("hex") or "").strip().lower()
        if h:
            by_hex[h] = a
    return list(by_hex.values())


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


def collect_once(ingest_token, os_token):
    community, cnow = fetch_community()
    opensky = []
    onow = None
    if os_token:
        opensky, onow = fetch_opensky(os_token)
    ac = merge(community, opensky)
    if not ac:
        print("  nothing to push")
        return 0
    now = cnow or onow or time.time()
    res = push(ac, now, ingest_token)
    print(f"  ingested: {res.get('ingested', res)} (merged {len(ac)})")
    return res.get("ingested", 0) if isinstance(res, dict) else 0


def main():
    ingest_token = os.environ.get("INGEST_TOKEN", "").strip()
    if not ingest_token:
        print("ERROR: INGEST_TOKEN env var not set", file=sys.stderr)
        sys.exit(1)

    os_token = opensky_token()
    print("OpenSky: " + ("enabled (authenticated)" if os_token
                         else "disabled (no credentials)"))

    if LOOP_SECONDS <= 0:                   # legacy single-snapshot mode
        collect_once(ingest_token, os_token)
        return

    deadline = time.monotonic() + LOOP_SECONDS
    poll = 0
    while True:
        poll += 1
        started = time.monotonic()
        print(f"[poll {poll}] t+{int(started - (deadline - LOOP_SECONDS))}s")
        try:
            collect_once(ingest_token, os_token)
        except Exception as e:              # keep the loop alive on transient errors
            print(f"  poll error: {e}")
        # Stop if the next poll wouldn't finish inside the window.
        if time.monotonic() + INTERVAL_SECONDS >= deadline:
            break
        # Sleep the remainder of the interval (account for time already spent).
        time.sleep(max(0, INTERVAL_SECONDS - (time.monotonic() - started)))
    print(f"done: {poll} polls over ~{LOOP_SECONDS}s")


if __name__ == "__main__":
    main()
