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

## Files

| File                 | Purpose                                          |
| -------------------- | ------------------------------------------------ |
| `fetch_listings.py`  | Pulls listings, updates SQLite, exports JSON     |
| `index.html`         | The dashboard (static, no build step)            |
| `data/listings.db`   | SQLite: `listings` + `price_history` tables      |
| `data/listings.json` | Export read by the dashboard                     |
| `data/snapshots/`    | Raw daily API responses (gzipped)                |

## Sharing / hosting options

The dashboard is a static page + one JSON file, so hosting is trivial:

1. **GitHub Pages + GitHub Actions (recommended, $0)** — a scheduled Action
   runs `fetch_listings.py` daily, commits the updated `data/`, and Pages
   serves this folder. Everyone gets a URL; favorites stay per-browser
   (localStorage).
2. **Any static host** (Netlify, Vercel, S3) — same idea.
3. **Just send the folder** — it runs anywhere Python exists.

Note: automating the fetch from a cloud runner may get blocked by Redfin's
bot protection more often than a home IP; if the Action proves flaky, run
the fetch locally on a schedule (Windows Task Scheduler) and only push the
data.
