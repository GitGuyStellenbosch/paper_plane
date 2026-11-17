"""Paper Plane patrol: fetch the cheapest cached fares from Cape Town and keep a daily price history.

Run it by hand with `python patrol.py`, or let the GitHub Actions workflow run it every morning.

Writes:
  data/fares.csv   one row per route per day (the cheapest fare seen that day)
  data/deals.json  the latest fare per route, its 30-day usual price, a deal score and whether it is a deal

Deal score:
  Until a route has MIN_HISTORY_DAYS of our own history, the score compares the cheapest
  one-way date in the next few months with the median one-way date ("date spread").
  After that it compares today's return fare with the route's 30-day median.
"""

import csv
import gzip
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FARES_CSV = ROOT / "data" / "fares.csv"
DEALS_JSON = ROOT / "data" / "deals.json"
API_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
GROUPED_URL = "https://api.travelpayouts.com/aviasales/v3/grouped_prices"
BASELINE_MONTHS = 3                  # months of departure dates used for the date-spread baseline
MIN_BASELINE_DATES = 5               # need at least this many priced dates for a date-spread score

SAST = timezone(timedelta(hours=2))  # South Africa has no daylight saving
HISTORY_DAYS = 30                    # window for the "usual" price
MIN_HISTORY_DAYS = 7                 # don't flag deals before a route has this many days
DEAL_DROP = 0.25                     # a deal is 25%+ below the usual price

FIELDS = ["fetched_day", "origin", "destination", "price", "currency",
          "airline", "transfers", "depart_date", "return_date", "link",
          "oneway_min", "oneway_median", "oneway_dates"]


def load_env():
    """Read KEY=value lines from .env when running locally. Real env vars win."""
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def api_get(token, url, params):
    req = urllib.request.Request(
        url + "?" + urllib.parse.urlencode(params),
        headers={"X-Access-Token": token, "Accept-Encoding": "gzip"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
    payload = json.loads(body)
    if not payload.get("success"):
        raise RuntimeError(payload.get("error") or "API returned success=false")
    return payload.get("data")


def cheapest_fare(token, origin, destination, currency):
    """Return the cheapest cached return fare for a route, or None if the cache has nothing."""
    rows = api_get(token, API_URL, {
        "origin": origin, "destination": destination, "currency": currency,
        "one_way": "false", "sorting": "price", "limit": 1,
    }) or []
    return rows[0] if rows else None


def date_spread(token, origin, destination, currency, today):
    """One-way price per departure date over the next few months: (cheapest, median, number of dates)."""
    year, month = int(today[:4]), int(today[5:7])
    prices = []
    for i in range(BASELINE_MONTHS):
        y, m = year + (month - 1 + i) // 12, (month - 1 + i) % 12 + 1
        data = api_get(token, GROUPED_URL, {
            "origin": origin, "destination": destination, "currency": currency,
            "group_by": "departure_at", "departure_at": f"{y}-{m:02d}",
        }) or {}
        prices += [row["price"] for day, row in data.items() if day >= today]
        time.sleep(0.3)
    if not prices:
        return None
    return min(prices), round(statistics.median(prices)), len(prices)


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
            if int(new["price"]) <= int(row["price"]):
                rows[i] = new
            elif new.get("oneway_median"):
                row.update({k: new[k] for k in ("oneway_min", "oneway_median", "oneway_dates")})
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

        oneway_min = int(latest.get("oneway_min") or 0)
        oneway_median = int(latest.get("oneway_median") or 0)
        oneway_dates = int(latest.get("oneway_dates") or 0)
        spread = (1 - oneway_min / oneway_median) if oneway_median and oneway_dates >= MIN_BASELINE_DATES else None

        if days >= MIN_HISTORY_DAYS:
            score, basis = drop, "history"
        elif spread is not None:
            score, basis = spread, "date_spread"
        else:
            score, basis = None, "none"

        deals.append({
            **{k: latest[k] for k in FIELDS if not k.startswith("oneway_")},
            "price": price,
            "transfers": int(latest["transfers"]) if latest["transfers"] != "" else None,
            "usual_price": round(usual),
            "drop": round(drop, 3),
            "days_of_history": days,
            "date_spread": {
                "oneway_min": oneway_min or None,
                "oneway_median": oneway_median or None,
                "dates": oneway_dates,
                "score": round(spread, 3) if spread is not None else None,
            },
            "deal_score": round(score, 3) if score is not None else None,
            "score_basis": basis,
            "is_deal": score is not None and score >= DEAL_DROP,
            "status": "cached, unconfirmed",
        })
    deals.sort(key=lambda d: (not d["is_deal"], -(d["deal_score"] or -1), d["price"]))
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
        try:
            spread = date_spread(token, origin, destination, currency, today)
        except (urllib.error.URLError, RuntimeError, ValueError) as err:
            print(f"{origin}-{destination}: date spread failed ({err})")
            spread = None
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
            "oneway_min": str(spread[0]) if spread else "",
            "oneway_median": str(spread[1]) if spread else "",
            "oneway_dates": str(spread[2]) if spread else "",
        })
        spread_note = (f" | one-way best R{spread[0]:,} vs typical R{spread[1]:,} over {spread[2]} dates"
                       if spread else " | no date spread")
        print(f"{origin}-{destination}: R{fare['price']:,} {fare.get('airline', '')} "
              f"{(fare.get('departure_at') or '')[:10]} to {(fare.get('return_at') or '')[:10]}{spread_note}")

    write_fares(rows)
    DEALS_JSON.write_text(json.dumps(build_deals(rows, today), indent=2), encoding="utf-8")
    print(f"Saved {found}/{len(config['destinations'])} routes for {today}. "
          f"{len(rows)} rows in history.")


if __name__ == "__main__":
    main()
