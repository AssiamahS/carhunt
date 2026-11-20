#!/usr/bin/env python3
"""Pull used-car listings into docs/data/listings.json for the Pages site.

Sources:
  truecar  - scraped (the only big listing site that answers a plain request)
  manual   - manual.json, cars you found yourself on CarGurus, Edmunds, etc.
             Each needs the car's own listing url and an image url.

Standard library only, so the GitHub Action needs no install step.

    python3 scrape.py            # refresh docs/data/listings.json
    python3 scrape.py --dry-run  # print a summary, write nothing
"""
import argparse
import datetime as dt
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
MANUAL = ROOT / "manual.json"
OUT = ROOT / "docs" / "data" / "listings.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
PAGE_SIZE = 30
MAX_PAGES = 10
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


class SourceError(Exception):
    """A source answered, but not with listings (blocked, redesigned, down)."""


def fetch(url, tries=3):
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    for attempt in range(1, tries + 1):
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                return response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as err:
            if err.code in (403, 404) or attempt == tries:
                raise SourceError("HTTP %s for %s" % (err.code, url)) from err
        except OSError as err:
            if attempt == tries:
                raise SourceError("%s for %s" % (err, url)) from err
        time.sleep(3 * attempt)
    raise SourceError("no response for %s" % url)


def truecar_url(search, page):
    years = str(search["year_min"])
    if search["year_max"] != search["year_min"]:
        years += "-%s" % search["year_max"]
    url = (
        "https://www.truecar.com/used-cars-for-sale/listings/{make}/{model}/"
        "year-{years}/location-{city}-{state}/?searchRadius={radius}&sort[]=price_asc"
    ).format(years=years, **search)
    return url + ("&page=%d" % page if page > 1 else "")


def parse_truecar(html):
    """Return (rows, total_count, has_next_page) from one results page."""
    match = NEXT_DATA.search(html)
    if not match:
        raise SourceError("truecar: no page data (blocked or redesigned)")
    try:
        apollo = json.loads(match.group(1))["props"]["pageProps"]["__APOLLO_STATE__"]
    except (ValueError, KeyError) as err:
        raise SourceError("truecar: page data changed shape (%s)" % err) from err

    total, has_next = 0, False
    for key, value in apollo.get("ROOT_QUERY", {}).items():
        if key.startswith("marketplaceListingSearch(") and isinstance(value, dict):
            total = value.get("totalCount") or 0
            has_next = bool((value.get("pageInfo") or {}).get("hasNextPage"))

    rows = []
    for key, item in apollo.items():
        if not key.startswith("ConsumerSummaryListing:"):
            continue
        vehicle = item.get("vehicle") or {}
        details = vehicle.get("details") or {}
        style = vehicle.get("style") or {}
        price = (item.get("pricing") or {}).get("listPrice")
        vin = vehicle.get("vin")
        if not vin or not price:
            continue
        dealer = apollo.get((item.get("dealership") or {}).get("__ref"), {})
        history = item.get("conditionHistory") or {}
        images = next((v for k, v in item.items() if k.startswith("galleryImages")), None) or {}
        image = next((e["node"]["url"] for e in images.get("edges", []) if e.get("node")), None)
        city, state = details.get("dealerCity"), details.get("dealerState")
        distance = details.get("dealerDistanceMiles")
        rows.append({
            "vin": vin,
            "source": "truecar",
            "url": "https://www.truecar.com/used-cars-for-sale/listing/%s/" % vin,
            "year": vehicle.get("year"),
            "make": (vehicle.get("make") or {}).get("name"),
            "model": (vehicle.get("model") or {}).get("name"),
            "trim": style.get("name") or style.get("trimName"),
            "drive": details.get("driveTypeAcronym"),
            "price": round(price),
            "miles": vehicle.get("mileage"),
            "color": details.get("exteriorColorName") or vehicle.get("exteriorColor"),
            "dealer": dealer.get("name"),
            "where": ", ".join(p for p in (city, state) if p),
            "state": state,
            "distance": round(distance) if distance is not None else None,
            "listed": (details.get("listedAt") or "")[:10] or None,
            "accidents": history.get("accidentCount"),
            "owners": history.get("ownerCount"),
            "clean_title": history.get("isCleanTitle"),
            "deal": (item.get("marketAnalysis") or {}).get("priceQuality"),
            "image": image,
        })
    return rows, total, has_next


def within_radius(rows, radius):
    """TrueCar pads thin local results with cars from across the country."""
    return [r for r in rows if r.get("distance") is None or r["distance"] <= radius]


def within_limits(rows, search):
    """Drop cars over the search's optional price_max or miles_max."""
    price_max, miles_max = search.get("price_max"), search.get("miles_max")
    return [
        r for r in rows
        if (not price_max or r["price"] <= price_max)
        and (not miles_max or (r.get("miles") or 0) <= miles_max)
    ]


def juniper_status(row):
    """Model Y only: is this the 2025 "Juniper" redesign?

    Every 2026 is, and so is the 2025 Launch Series. Other 2025 cars were built
    in both bodies under the same trim names, so a hand-added one says "check".

    TrueCar is the exception: it files the redesign under 2026. All 49 of its
    2025 cars nationwide (checked by photo, 2026-11-19) were the old body, and
    nothing in the VIN, trim, range or colour tells the two apart.
    """
    if "model y" not in (row.get("model") or "").lower():
        return None
    year = row.get("year") or 0
    if year >= 2026 or "launch" in (row.get("trim") or "").lower():
        return "yes"
    if year == 2025 and row.get("source") != "truecar":
        return "check"
    return "no"


def scrape_truecar(search):
    rows = {}
    price_max = search.get("price_max")
    for page in range(1, MAX_PAGES + 1):
        found, total, has_next = parse_truecar(fetch(truecar_url(search, page)))
        for row in found:
            row["search"] = search["label"]
            row["juniper"] = juniper_status(row)
            rows.setdefault(row["vin"], row)
        if not found or not has_next or len(rows) >= total:
            break
        # Results come cheapest first, so a whole page over the cap means the rest are too.
        if price_max and min(r["price"] for r in found) > price_max:
            break
        time.sleep(2)
    if not rows:
        raise SourceError("truecar: zero listings for %s" % search["label"])
    kept = within_limits(within_radius(list(rows.values()), search["radius"]), search)
    if search.get("juniper_only"):
        kept = [r for r in kept if r["juniper"] in ("yes", "check")]
    return kept


def is_direct(row):
    """True when the row links to one car's own listing page and has a photo of it."""
    return row.get("exact") is not False and bool(row.get("url")) and bool(row.get("image"))


def load_manual():
    """Cars added by hand. A row with no VIN is keyed by its link + price.

    Every car on the page links to its own listing and shows its photo, so a
    row that only points at a search page, or has no image, is left out.
    """
    if not MANUAL.exists():
        return []
    rows = []
    for item in json.loads(MANUAL.read_text()):
        row = dict(item)
        if not is_direct(row):
            print("manual: skipped %s %s (needs a direct listing url and an image)"
                  % (row.get("price"), row.get("url")), file=sys.stderr)
            continue
        row.setdefault("source", "manual")
        row.setdefault("vin", "manual:%s:%s" % (row.get("url", ""), row.get("price", "")))
        row.setdefault("juniper", juniper_status(row))
        rows.append(row)
    return rows


def add_flags(rows, config):
    """Mark the things worth asking the seller about before driving out."""
    for row in rows:
        flags = list(row.get("manual_flags", []))
        if row.get("clean_title") is False:
            flags.append("branded-title")
        if row.get("accidents"):
            flags.append("accident")
        # Far under market with no verified clean title usually means salvage or a scam.
        if row.get("price") and row["price"] < config["suspicious_below"] and row.get("clean_title") is not True:
            flags.append("too-cheap")
        if (row.get("miles") or 0) > config["basic_warranty_miles"]:
            flags.append("no-basic-warranty")
        row["flags"] = flags
    return rows


def merge(old, new_rows, today, keep_gone_days, failed_sources=()):
    """Carry price history forward and keep sold cars around for a while."""
    merged = []
    seen = set()
    for row in new_rows:
        previous = old.get(row["vin"], {})
        history = [list(point) for point in previous.get("price_history", [])]
        if not history or history[-1][1] != row["price"]:
            history.append([today, row["price"]])
        row = dict(row)
        row["first_seen"] = previous.get("first_seen", today)
        row["last_seen"] = today
        row["price_history"] = history
        row["price_change"] = row["price"] - history[0][1]
        row["gone"] = False
        merged.append(row)
        seen.add(row["vin"])

    cutoff = dt.date.fromisoformat(today) - dt.timedelta(days=keep_gone_days)
    for vin, row in old.items():
        if vin in seen:
            continue
        row = dict(row)
        if row.get("source") in failed_sources:
            row["gone"] = False  # the source failed, the car did not sell
        elif dt.date.fromisoformat(row.get("last_seen", today)) < cutoff:
            continue
        else:
            row["gone"] = True
        merged.append(row)
    return merged


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print a summary, write nothing")
    args = parser.parse_args()

    config = json.loads(CONFIG.read_text())
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    previous = json.loads(OUT.read_text()) if OUT.exists() else {}
    # Search-page leads from before the direct-link rule are dropped, not shown as sold.
    old = {row["vin"]: row for row in previous.get("listings", [])
           if row.get("source") == "truecar" or is_direct(row)}

    rows, errors, failed = [], [], set()
    for search in config["searches"]:
        try:
            found = scrape_truecar(search)
            rows.extend(found)
            print("truecar: %d listings for %s" % (len(found), search["label"]))
        except SourceError as err:
            errors.append(str(err))
            failed.add("truecar")
            print("FAILED %s" % err, file=sys.stderr)
    manual = load_manual()
    rows.extend(manual)
    print("manual: %d listings" % len(manual))

    merged = add_flags(merge(old, rows, today, config["keep_gone_days"], failed), config)
    merged.sort(key=lambda r: (r.get("gone", False), r.get("price") or 0))
    output = {
        "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "errors": errors,
        "config": {key: config[key] for key in ("budget", "tax_rate", "basic_warranty_miles", "suspicious_below")},
        "searches": config["searches"],
        "links": config.get("links", []),
        "listings": merged,
    }
    active = [r for r in merged if not r["gone"]]
    print("total: %d active, %d gone" % (len(active), len(merged) - len(active)))
    if not args.dry_run:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(output, indent=1) + "\n")
        print("wrote %s" % OUT.relative_to(ROOT))
    # Non-zero when a scraped source failed, so the Action goes red and the last good data stays live.
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
