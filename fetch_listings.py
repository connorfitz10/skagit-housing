"""Fetch active for-sale listings for Skagit County, WA from Redfin's
unofficial search API and maintain a local price-history database.

Run daily (manually or via scheduler):
    python fetch_listings.py

Outputs:
    data/listings.db      - SQLite: listings + price_history tables
    data/listings.json    - export consumed by the dashboard (index.html)
    data/snapshots/       - raw gzipped API responses, one per day
"""

import gzip
import json
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from curl_cffi import requests

REGION_ID = 3098        # Skagit County, WA
REGION_TYPE = 5         # county
COUNTY_URL = "https://www.redfin.com/county/3098/WA/Skagit-County"
API_URL = "https://www.redfin.com/stingray/api/gis"
# The API ignores page_number but honors large num_homes values, so we
# grab everything in one request. County has ~750-800 active listings.
NUM_HOMES = 2000

# Skagit County's public tax-parcel GIS layer (official open data).
# Each listing's lat/lng is matched to its parcel for assessed value + taxes.
ASSESSOR_URL = ("https://geo.skagitcountywa.gov/server/rest/services/"
                "PortalServiceLayers/Tax_Parcels/MapServer/0/query")
ASSESSOR_REFRESH_DAYS = 120   # assessed values only change ~annually
ASSESSOR_DELAY_S = 0.15      # politeness delay between parcel queries

DATA_DIR = Path(__file__).parent / "data"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
DB_PATH = DATA_DIR / "listings.db"
JSON_PATH = DATA_DIR / "listings.json"


def val(obj, *keys, default=None):
    """Unwrap Redfin's {"value": x, "level": n} field wrappers."""
    for key in keys:
        if not isinstance(obj, dict) or key not in obj:
            return default
        obj = obj[key]
    if isinstance(obj, dict):
        obj = obj.get("value")
    return obj if obj is not None else default


def fetch_all_homes(session):
    resp = session.get(
        API_URL,
        params={
            "al": "1",
            "region_id": str(REGION_ID),
            "region_type": str(REGION_TYPE),
            "status": "9",                  # active + coming soon
            "uipt": "1,2,3,4,5,6,7,8",      # all property types
            "sf": "1,2,3,5,6,7",            # all sale types
            "num_homes": str(NUM_HOMES),
            "ord": "days-on-redfin-asc",
            "v": "8",
        },
        headers={"Referer": COUNTY_URL},
    )
    resp.raise_for_status()
    text = resp.text
    if text.startswith("{}&&"):
        text = text[4:]
    homes = json.loads(text).get("payload", {}).get("homes", [])
    print(f"  fetched {len(homes)} homes")
    if len(homes) >= NUM_HOMES:
        print(f"  WARNING: hit the {NUM_HOMES} request cap; raise NUM_HOMES.")
    return homes


def normalize(home):
    lat_long = val(home, "latLong", default={}) or {}
    if isinstance(lat_long, dict):
        lat = lat_long.get("latitude")
        lng = lat_long.get("longitude")
    else:
        lat = lng = None
    time_on_redfin_ms = val(home, "timeOnRedfin", default=0) or 0
    return {
        "property_id": home.get("propertyId"),
        "listing_id": home.get("listingId"),
        "mls_id": val(home, "mlsId"),
        "url": "https://www.redfin.com" + home.get("url", "") if home.get("url") else None,
        "address": val(home, "streetLine"),
        "city": home.get("city"),
        "zip": home.get("zip"),
        "price": val(home, "price"),
        "hoa_month": val(home, "hoa"),
        "beds": home.get("beds"),
        "baths": home.get("baths"),
        "sqft": val(home, "sqFt"),
        "price_per_sqft": val(home, "pricePerSqFt"),
        "lot_size": val(home, "lotSize"),
        "year_built": val(home, "yearBuilt"),
        "property_type": home.get("uiPropertyType"),
        "mls_status": home.get("mlsStatus"),
        "dom": val(home, "dom", default=round(time_on_redfin_ms / 86_400_000) or None),
        "lat": lat,
        "lng": lng,
    }


PROPERTY_TYPES = {1: "House", 2: "Condo", 3: "Townhouse", 4: "Multi-Family",
                  5: "Land", 6: "Other", 7: "Other", 8: "Mobile", 13: "Co-op"}


def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS listings (
            property_id   INTEGER PRIMARY KEY,
            listing_id    INTEGER,
            mls_id        TEXT,
            url           TEXT,
            address       TEXT,
            city          TEXT,
            zip           TEXT,
            price         INTEGER,
            original_price INTEGER,
            hoa_month     INTEGER,
            beds          REAL,
            baths         REAL,
            sqft          INTEGER,
            price_per_sqft INTEGER,
            lot_size      INTEGER,
            year_built    INTEGER,
            property_type INTEGER,
            mls_status    TEXT,
            dom           INTEGER,
            lat           REAL,
            lng           REAL,
            first_seen    TEXT,
            last_seen     TEXT,
            active        INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS price_history (
            property_id INTEGER,
            date        TEXT,
            price       INTEGER,
            PRIMARY KEY (property_id, date)
        );
    """)
    # Assessor columns added after initial schema; migrate older databases.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(listings)")}
    for name, sqltype in [("parcel_id", "TEXT"), ("assessed_value", "INTEGER"),
                          ("annual_taxes", "REAL"), ("assessor_checked", "TEXT")]:
        if name not in cols:
            conn.execute(f"ALTER TABLE listings ADD COLUMN {name} {sqltype}")


def upsert(conn, rows, today):
    new_count = drop_count = 0
    for r in rows:
        if r["property_id"] is None:
            continue
        existing = conn.execute(
            "SELECT price, original_price FROM listings WHERE property_id = ?",
            (r["property_id"],),
        ).fetchone()
        if existing is None:
            conn.execute(
                """INSERT INTO listings (property_id, listing_id, mls_id, url, address,
                    city, zip, price, original_price, hoa_month, beds, baths, sqft,
                    price_per_sqft, lot_size, year_built, property_type, mls_status,
                    dom, lat, lng, first_seen, last_seen, active)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                (r["property_id"], r["listing_id"], r["mls_id"], r["url"], r["address"],
                 r["city"], r["zip"], r["price"], r["price"], r["hoa_month"], r["beds"],
                 r["baths"], r["sqft"], r["price_per_sqft"], r["lot_size"],
                 r["year_built"], r["property_type"], r["mls_status"], r["dom"],
                 r["lat"], r["lng"], today, today),
            )
            new_count += 1
            if r["price"]:
                conn.execute(
                    "INSERT OR REPLACE INTO price_history VALUES (?,?,?)",
                    (r["property_id"], today, r["price"]),
                )
        else:
            old_price = existing[0]
            if r["price"] and old_price and r["price"] != old_price:
                conn.execute(
                    "INSERT OR REPLACE INTO price_history VALUES (?,?,?)",
                    (r["property_id"], today, r["price"]),
                )
                if r["price"] < old_price:
                    drop_count += 1
            conn.execute(
                """UPDATE listings SET listing_id=?, mls_id=?, url=?, address=?, city=?,
                    zip=?, price=?, hoa_month=?, beds=?, baths=?, sqft=?, price_per_sqft=?,
                    lot_size=?, year_built=?, property_type=?, mls_status=?, dom=?,
                    lat=?, lng=?, last_seen=?, active=1
                   WHERE property_id=?""",
                (r["listing_id"], r["mls_id"], r["url"], r["address"], r["city"],
                 r["zip"], r["price"], r["hoa_month"], r["beds"], r["baths"], r["sqft"],
                 r["price_per_sqft"], r["lot_size"], r["year_built"], r["property_type"],
                 r["mls_status"], r["dom"], r["lat"], r["lng"], today, r["property_id"]),
            )
    return new_count, drop_count


def _num(s):
    """Assessor attributes arrive as strings like '2065400' or '18669.82'."""
    try:
        f = float(s)
        return f if f else None
    except (TypeError, ValueError):
        return None


def query_parcel(lat, lng):
    params = urllib.parse.urlencode({
        "f": "json",
        "geometry": json.dumps({"x": lng, "y": lat,
                                "spatialReference": {"wkid": 4326}}),
        "geometryType": "esriGeometryPoint",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "PARCELID,AssessedValue,TotalTaxes",
        "returnGeometry": "false",
    })
    with urllib.request.urlopen(f"{ASSESSOR_URL}?{params}", timeout=30) as r:
        feats = json.loads(r.read()).get("features", [])
    if not feats:
        return None
    return feats[0]["attributes"]


def enrich_assessor(conn, today):
    """Attach county assessed value + annual taxes to listings that lack
    them (or whose data is stale). Cached in SQLite, so after the first
    run only newly listed homes trigger parcel queries."""
    stale = (date.fromisoformat(today) - timedelta(days=ASSESSOR_REFRESH_DAYS)).isoformat()
    todo = conn.execute(
        """SELECT property_id, lat, lng FROM listings
           WHERE active = 1 AND lat IS NOT NULL
             AND (assessor_checked IS NULL OR assessor_checked < ?)""",
        (stale,),
    ).fetchall()
    if not todo:
        return 0, 0
    print(f"Querying Skagit County assessor for {len(todo)} listings...")
    ok = failed = 0
    for i, row in enumerate(todo, 1):
        try:
            attrs = query_parcel(row["lat"], row["lng"])
        except Exception:
            failed += 1
            continue  # leave unchecked; retried next run
        if attrs:
            conn.execute(
                """UPDATE listings SET parcel_id=?, assessed_value=?,
                   annual_taxes=?, assessor_checked=? WHERE property_id=?""",
                (attrs.get("PARCELID"), _num(attrs.get("AssessedValue")),
                 _num(attrs.get("TotalTaxes")), today, row["property_id"]),
            )
            ok += 1
        else:
            # no parcel at that point (bad geocode); don't retry daily
            conn.execute(
                "UPDATE listings SET assessor_checked=? WHERE property_id=?",
                (today, row["property_id"]),
            )
        if i % 100 == 0:
            print(f"  {i}/{len(todo)} done")
            conn.commit()
        time.sleep(ASSESSOR_DELAY_S)
    conn.commit()
    if failed:
        print(f"  {failed} parcel queries failed (will retry next run)")
    return ok, failed


UNIT_ADDRESS_RE = re.compile(r"#|\bunit\b|\bspc\b|\bspace\b|\btrlr\b|\bapt\b", re.I)


def export_json(conn, today):
    listings = []
    for row in conn.execute("SELECT * FROM listings WHERE active = 1"):
        d = dict(row)
        history = [
            {"date": h["date"], "price": h["price"]}
            for h in conn.execute(
                "SELECT date, price FROM price_history WHERE property_id = ? ORDER BY date",
                (d["property_id"],),
            )
        ]
        d["price_history"] = history
        d["property_type_label"] = PROPERTY_TYPES.get(d["property_type"], "Other")
        if d["original_price"] and d["price"] and d["price"] < d["original_price"]:
            d["price_drop"] = d["original_price"] - d["price"]
            d["price_drop_pct"] = round(100 * d["price_drop"] / d["original_price"], 1)
        else:
            d["price_drop"] = 0
            d["price_drop_pct"] = 0
        # Unit-numbered listings (mobile-home parks, some condos) geocode
        # onto a shared parent parcel whose assessed value covers the whole
        # complex — comparing against it is meaningless, so drop it. The
        # <20% ratio check catches shared parcels with unmarked addresses.
        suspect = bool(d["address"] and UNIT_ADDRESS_RE.search(d["address"]))
        if not suspect and d["price"] and d["assessed_value"]:
            if 100 * d["price"] / d["assessed_value"] < 20:
                suspect = True
        if suspect:
            d["assessed_value"] = None
            d["annual_taxes"] = None
        if d["price"] and d["assessed_value"]:
            d["ask_vs_assessed_pct"] = round(100 * d["price"] / d["assessed_value"])
        else:
            d["ask_vs_assessed_pct"] = None
        listings.append(d)

    out = {
        "updated": today,
        "county": "Skagit County, WA",
        "listings": listings,
    }
    JSON_PATH.write_text(json.dumps(out), encoding="utf-8")
    return len(listings)


def main():
    today = date.today().isoformat()
    DATA_DIR.mkdir(exist_ok=True)
    SNAPSHOT_DIR.mkdir(exist_ok=True)

    print(f"Fetching Skagit County listings from Redfin ({today})...")
    session = requests.Session(impersonate="chrome")
    warm = session.get(COUNTY_URL)
    warm.raise_for_status()

    homes = fetch_all_homes(session)
    if not homes:
        print("ERROR: no homes returned; API may have changed or blocked us.")
        sys.exit(1)

    snapshot_path = SNAPSHOT_DIR / f"{today}.json.gz"
    with gzip.open(snapshot_path, "wt", encoding="utf-8") as f:
        json.dump(homes, f)

    rows = [normalize(h) for h in homes]
    # de-dupe on property_id (a home can appear on page boundaries twice)
    seen, unique = set(), []
    for r in rows:
        if r["property_id"] not in seen:
            seen.add(r["property_id"])
            unique.append(r)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    new_count, drop_count = upsert(conn, unique, today)

    # Listings we've tracked as active but which vanished from today's fetch
    # have gone pending/sold/delisted. Only trust this on a plausibly full
    # fetch, so a partial API failure can't mass-deactivate the database.
    delisted = 0
    if len(unique) > 100:
        cur = conn.execute(
            "UPDATE listings SET active = 0 WHERE active = 1 AND last_seen < ?", (today,)
        )
        delisted = cur.rowcount
    conn.commit()

    enrich_assessor(conn, today)

    exported = export_json(conn, today)
    conn.close()

    print(f"Done. {len(unique)} active listings ({new_count} new today, "
          f"{drop_count} price drops today, {delisted} left the market).")
    print(f"Exported {exported} listings to {JSON_PATH}")


if __name__ == "__main__":
    main()
