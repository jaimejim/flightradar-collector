# flightradar-collector

Forward ADS-B collector for [flightradar.jaime.win](https://flightradar.jaime.win) —
an aggregated flight-density map of the approach corridor over Soukanlahti (Espoo)
into Helsinki-Vantaa (EFHK).

## What it does

Every 10 minutes, a GitHub Actions job fetches the current aircraft snapshot from
free community ADS-B networks (airplanes.live / adsb.fi / adsb.lol) and POSTs it to
the site's Cloudflare Worker, which stores it in D1. Over time this builds the
history the map aggregates.

## Why GitHub Actions?

The community ADS-B APIs rate-limit by IP and throttle Cloudflare's shared Worker
egress IPs (HTTP 429/522), so the Worker can't collect directly. GitHub's runners
have clean IPs, so collection works from here — and nothing runs on a laptop.

## Setup

1. This repo must be **public** (free unlimited Actions minutes; private repos
   only get 2,000 min/month, not enough for a 10-min cadence).
2. Add a repository secret **`INGEST_TOKEN`** (Settings → Secrets and variables →
   Actions) matching the Worker's `INGEST_TOKEN` secret.
3. The workflow runs on schedule automatically; trigger a test run from the
   Actions tab (**collect → Run workflow**).

The token is only ever a GitHub secret — never committed. `collect.py` reads it
from the `INGEST_TOKEN` env var.

## Notes

- **Push uses `curl`, not urllib.** Cloudflare's bot protection returns HTTP 403
  to Python-urllib's request fingerprint; `curl` passes. `curl` is preinstalled
  on GitHub runners.
- **First-workflow registration:** GitHub only rescans workflows on a real file
  content change — empty commits won't register a brand-new workflow. If it
  shows 0 workflows after first push, edit the YAML (any change) and push again.
- A benign warning about "Node.js 20 deprecated" refers to the checkout/setup
  actions' runtime, not this code; safe to ignore.

Data: community ADS-B feeds. GPS-interference overlay data: gpsjam.org (CC-BY).
