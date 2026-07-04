# Skagit County Housing Dashboard

A local dashboard of active for-sale listings in Skagit County, WA, with
filtering, saved favorites, and price-drop tracking.

Data comes from Redfin's (unofficial) search API — the same JSON feed that
powers redfin.com's map view. Listing data ultimately originates from NWMLS.
This is for personal use; be a polite guest (the fetch runs once a day, not
continuously) and expect that the endpoint could change without notice.

## Setup

```
pip install -r requirements.txt
```

## Daily use

1. Fetch/refresh the data (run once a day):

   ```
   python fetch_listings.py
   ```

2. View the dashboard:

   ```
   python -m http.server 8742
   ```

   then open http://localhost:8742 in a browser.

## How price-drop tracking works

Redfin only reports each listing's *current* price. This project builds the
history itself: every run stores a snapshot in `data/listings.db` (SQLite).

- **First time a listing appears** → its price is recorded as `original_price`.
- **Price differs from last run** → a row is added to `price_history`.
- **Listing vanishes from the feed** → marked inactive (pending/sold/delisted).

The dashboard's "Price drops" tab and ▼ badges compare current price against
original price. The longer the fetch script runs daily, the richer the
history gets — day one has no drops by definition.

`data/snapshots/` keeps the raw gzipped API responses (one per day), so the
database can always be rebuilt or re-analyzed later.

## County assessor data

Each listing is also matched (by lat/lng, point-in-parcel) against Skagit
County's public tax-parcel GIS layer to pull the county-assessed value and
annual property taxes. The dashboard shows asking price as a percentage of
assessed value — listings at ≤95% of assessed get a "% of assessed" badge
as a potential-deal signal. Results are cached in SQLite, so only newly
listed homes trigger parcel queries on later runs. (Caveat: for condos the
parcel value may cover more than the unit.)

## Files

| File                 | Purpose                                          |
| -------------------- | ------------------------------------------------ |
| `fetch_listings.py`  | Pulls listings + assessor data, updates SQLite, exports JSON |
| `index.html`         | The dashboard (static, no build step)            |
| `daily_update.ps1`   | Fetch + git push; run daily by Windows Task Scheduler |
| `data/listings.db`   | SQLite: `listings` + `price_history` tables      |
| `data/listings.json` | Export read by the dashboard                     |
| `data/snapshots/`    | Raw daily API responses (gzipped, local only)    |

## How it's published

The live site is **https://connorfitz10.github.io/skagit-housing/** (GitHub
Pages, serving this repo's `main` branch).

Redfin blocks requests from GitHub-hosted runners (HTTP 405), so the daily
fetch cannot run in GitHub Actions. Instead, Windows Task Scheduler on
Connor's PC runs `daily_update.ps1` every morning at 7:30 (task name
"Skagit Housing Daily Fetch"): it fetches fresh data and pushes, which
republishes the site. If the PC is asleep at 7:30 the task runs when it
next wakes. The workflow in `.github/workflows/fetch.yml` is kept for
manually re-testing whether Redfin ever unblocks cloud runners.
