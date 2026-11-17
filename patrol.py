"""Paper Plane patrol: fetch the cheapest cached fares from Cape Town and keep a daily price history.

Run it by hand with `python patrol.py`, or let the GitHub Actions workflow run it every morning.

Writes:
  data/fares.csv   one row per route per day (the cheapest fare seen that day)
  data/deals.json  the latest fare per route, its 30-day usual price and whether it is a deal
"""

import csv
import gzip
import json
import os
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FARES_CSV = ROOT / "data" / "fares.csv"
DEALS_JSON = ROOT / "data" / "deals.json"
API_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"

SAST = timezone(timedelta(hours=2))  # South Africa has no daylight saving
HISTORY_DAYS = 30                    # window for the "usual" price
MIN_HISTORY_DAYS = 7                 # don't flag deals before a route has this many days
DEAL_DROP = 0.25                     # a deal is 25%+ below the usual price

FIELDS = ["fetched_day", "origin", "destination", "price", "currency",
          "airline", "transfers", "depart_date", "return_date", "link"]


def load_env():
    """Read KEY=value lines from .env when running locally. Real env vars win."""
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def cheapest_fare(token, origin, destination, currency):
    """Return the cheapest cached return fare for a route, or None if the cache has nothing."""
    params = {
        "origin": origin, "destination": destination, "currency": currency,
        "one_way": "false", "sorting": "price", "limit": 1,
    }
    req = urllib.request.Request(
        API_URL + "?" + urllib.parse.urlencode(params),
        headers={"X-Access-Token": token, "Accept-Encoding": "gzip"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
    payload = json.loads(body)
    if not payload.get("success"):
        raise RuntimeError(payload.get("error") or "API returned success=false")
    rows = payload.get("data") or []
    return rows[0] if rows else None


def read_fares():
    if not FARES_CSV.exists():
        return []
    with FARES_CSV.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_fares(rows):
    FARES_CSV.parent.mkdir(parents=True, exist_ok=True)
    rows.sort(key=lambda r: (r["fetched_day"], r["destination"]))
    with FARES_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def upsert(rows, new):
    """Keep only the cheapest fare per route per day."""
    for i, row in enumerate(rows):
        if (row["fetched_day"], row["origin"], row["destination"]) == (
                new["fetched_day"], new["origin"], new["destination"]):
            if int(new["price"]) < int(row["price"]):
                rows[i] = new
            return
    rows.append(new)


def build_deals(rows, today):
    """Latest fare per route, compared with the median of the last 30 days."""
    cutoff = (datetime.fromisoformat(today) - timedelta(days=HISTORY_DAYS)).date().isoformat()
    by_route = {}
    for row in rows:
        by_route.setdefault((row["origin"], row["destination"]), []).append(row)

    deals = []
    for (origin, destination), history in by_route.items():
        history.sort(key=lambda r: r["fetched_day"])
        latest = history[-1]
        window = [int(r["price"]) for r in history if r["fetched_day"] >= cutoff]
        usual = statistics.median(window)
        days = len(window)
        price = int(latest["price"])
        drop = 1 - price / usual if usual else 0
        deals.append({
            **latest,
            "price": price,
            "transfers": int(latest["transfers"]) if latest["transfers"] != "" else None,
            "usual_price": round(usual),
            "drop": round(drop, 3),
            "days_of_history": days,
            "is_deal": days >= MIN_HISTORY_DAYS and drop >= DEAL_DROP,
            "status": "cached, unconfirmed",
        })
    deals.sort(key=lambda d: (not d["is_deal"], -d["drop"], d["price"]))
    return {"updated": datetime.now(SAST).isoformat(timespec="minutes"), "origin": "CPT", "routes": deals}


def main():
    load_env()
    token = os.environ.get("FLIGHT_API_TOKEN")
    if not token:
        sys.exit("FLIGHT_API_TOKEN is not set. Add it to .env (local) or the repo's Actions secrets.")

    config = json.loads((ROOT / "routes.json").read_text(encoding="utf-8"))
    origin, currency = config["origin"], config["currency"]
    today = datetime.now(SAST).date().isoformat()
    rows = read_fares()
    found = 0

    for destination in config["destinations"]:
        try:
            fare = cheapest_fare(token, origin, destination, currency)
        except (urllib.error.URLError, RuntimeError, ValueError) as err:
            print(f"{origin}-{destination}: failed ({err})")
            continue
        if not fare:
            print(f"{origin}-{destination}: no cached fare")
            continue
        found += 1
        upsert(rows, {
            "fetched_day": today,
            "origin": origin,
            "destination": destination,
            "price": str(fare["price"]),
            "currency": currency.upper(),
            "airline": fare.get("airline", ""),
            "transfers": str(fare.get("transfers", "")),
            "depart_date": (fare.get("departure_at") or "")[:10],
            "return_date": (fare.get("return_at") or "")[:10],
            "link": fare.get("link", ""),
        })
        print(f"{origin}-{destination}: R{fare['price']:,} {fare.get('airline', '')} "
              f"{(fare.get('departure_at') or '')[:10]} to {(fare.get('return_at') or '')[:10]}")

    write_fares(rows)
    DEALS_JSON.write_text(json.dumps(build_deals(rows, today), indent=2), encoding="utf-8")
    print(f"Saved {found}/{len(config['destinations'])} routes for {today}. "
          f"{len(rows)} rows in history.")


if __name__ == "__main__":
    main()
