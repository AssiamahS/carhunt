#!/usr/bin/env python3
"""Pull used-car listings into docs/data/listings.json for the Pages site.

Sources:
  truecar  - search pages (title, accident and owner history included)
  kbb      - KBB's listing feed, the same inventory Autotrader shows. Comes with
             KBB's fair price and an AutoCheck title/accident preview.
  cargurus - CarGurus' search feed. Has its market value, no history.
  carmax   - CarMax's search page, fetched with curl (it turns Python away).
  craigslist - its search page, then each posting for miles, title and VIN.
  cars.com, carvana - search pages loaded in a real browser by browser_fetch.mjs,
             one page per search, because they block everything else.
  manual   - manual.json, cars you found yourself anywhere else.
             Each needs the car's own listing url and an image url.

A car listed on more than one site becomes one row with a link to each.

Standard library only, so the GitHub Action needs no install step.

    python3 scrape.py            # refresh docs/data/listings.json
    python3 scrape.py --dry-run  # print a summary, write nothing
"""
import argparse
import base64
import datetime as dt
import html
import json
import re
import shutil
import subprocess
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
MAX_PAGES = 10
KBB_PAGE = 100
CARGURUS_PAGE = 48  # the feed never returns more, whatever maxResults says
KBB_MODELS = {"model-3": "TESMOD3", "model-s": "TESMODS", "model-y": "TESMODY"}
CARGURUS_MODELS = {"model-3": "d2475", "model-s": "d2039", "model-y": "d3044"}
CARGURUS_DEALS = {"GREAT_PRICE": "EXCELLENT", "GOOD_PRICE": "GOOD", "FAIR_PRICE": "FAIR",
                  "POOR_PRICE": "HIGH", "OVERPRICED": "HIGH"}
# When two sites list the same car at the same price, link to this one first.
SOURCE_ORDER = ("truecar", "kbb", "cargurus", "carmax", "cars.com", "carvana", "craigslist")
CRAIGSLIST_SITE = "newjersey"  # any regional site works: the search is by zip and radius
CRAIGSLIST_MAX = 40            # postings opened per search
CRAIGSLIST_MIN_PRICE = 3000    # below this it is a down payment or a parts car
CRAIGSLIST_BAD_TITLES = ("salvage", "rebuilt", "parts only", "missing")
# These show one page out of a much longer list, so a car missing from it has not sold.
# It stays for a couple of days in case it comes back, then leaves without a "sold" label.
SAMPLED_SOURCES = {"carmax", "cars.com", "carvana"}
SAMPLED_KEEP_DAYS = 2
BROWSER_FETCH = ROOT / "browser_fetch.mjs"
BROWSER_TIMEOUT = 9 * 60
# A price this far under KBB's or CarGurus' value means damage, a salvage title or a scam.
# A verified clean title earns more room before the warning.
FAR_BELOW_MARKET = 0.75
FAR_BELOW_MARKET_CLEAN = 0.65
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


class SourceError(Exception):
    """A source answered, but not with listings (blocked, redesigned, down)."""


def fetch(url, tries=3):
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/json",
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


def fetch_curl(url, cache={}):
    """For sites that reject Python's TLS handshake but accept curl's. One fetch per URL per run."""
    if url not in cache:
        done = subprocess.run(
            ["curl", "-s", "-g", "--http2", "--compressed", "-m", "45", "-w", "\n%{http_code}", url,
             "-H", "User-Agent: " + USER_AGENT,
             "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
             "-H", "Accept-Language: en-US,en;q=0.9",
             # CarMax checks for the full set a real Chrome sends on a typed-in address.
             "-H", 'sec-ch-ua: "Chromium";v="141", "Not?A_Brand";v="8"', "-H", "sec-ch-ua-mobile: ?0",
             "-H", 'sec-ch-ua-platform: "macOS"', "-H", "Sec-Fetch-Dest: document", "-H", "Sec-Fetch-Mode: navigate",
             "-H", "Sec-Fetch-Site: none", "-H", "Sec-Fetch-User: ?1", "-H", "Upgrade-Insecure-Requests: 1"],
            capture_output=True, text=True, timeout=60, check=False)
        body, _, code = done.stdout.rpartition("\n")
        if code != "200":
            raise SourceError("HTTP %s for %s" % (code or "error", url))
        cache[url] = body
    return cache[url]


BROWSER_PAGES = {}


def browser_load(urls):
    """Load every url in one real-browser session and remember the pages. Empty on failure."""
    urls = [url for url in dict.fromkeys(urls) if url not in BROWSER_PAGES]
    if not urls:
        return
    try:
        # launchd runs this with a bare PATH, so look in the usual install spots too.
        node = shutil.which("node") or next((n for n in ("/usr/local/bin/node", "/opt/homebrew/bin/node") if Path(n).exists()), "node")
        done = subprocess.run([node, str(BROWSER_FETCH)], input=json.dumps(urls), capture_output=True,
                              text=True, timeout=BROWSER_TIMEOUT, check=False)
        BROWSER_PAGES.update(json.loads(done.stdout))
        if done.stderr.strip():
            print(done.stderr.strip(), file=sys.stderr)
    except (OSError, ValueError, subprocess.TimeoutExpired) as err:
        print("browser: %s" % err, file=sys.stderr)


def browser_page(url):
    page = BROWSER_PAGES.get(url)
    if not page:
        raise SourceError("browser could not load %s" % url)
    return page


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
    """Model Y only: is this the "Juniper" redesign?

    US cars with the new body are model year 2026, plus anything sold as a
    Launch Series. A 2025 is the old body: all 49 for sale nationwide on
    2026-11-19 were, checked by photo, and nothing in a 2025's VIN, trim,
    range or colour tells the two apart.
    """
    if "model y" not in (row.get("model") or "").lower():
        return None
    year = row.get("year") or 0
    if year >= 2026 or "launch" in (row.get("trim") or "").lower():
        return "yes"
    return "no"


def finish(rows, search):
    """Tag rows with their search and drop what falls outside it."""
    for row in rows:
        row["search"] = search["label"]
        row["juniper"] = juniper_status(row)
    kept = within_limits(within_radius(rows, search["radius"]), search)
    if search.get("juniper_only"):
        kept = [r for r in kept if r["juniper"] == "yes"]
    return kept


def in_years(rows, search):
    """For sources whose search page cannot be narrowed to the search's years."""
    return [r for r in rows if search["year_min"] <= (r.get("year") or 0) <= search["year_max"]]


def same_model(rows, search):
    """For sources whose search is fuzzy about the model."""
    make, model = search["make"].lower(), search["model"].replace("-", " ").lower()
    return [r for r in rows if (r.get("make") or "").lower() == make and (r.get("model") or "").lower() == model]


def whole(value):
    """'41,535' or 41535.0 -> 41535. None when there is no number."""
    digits = re.sub(r"[^\d.]", "", str(value if value is not None else ""))
    return round(float(digits)) if digits else None


def scrape_truecar(search):
    rows = {}
    price_max = search.get("price_max")
    for page in range(1, MAX_PAGES + 1):
        found, total, has_next = parse_truecar(fetch(truecar_url(search, page)))
        for row in found:
            rows.setdefault(row["vin"], row)
        if not found or not has_next or len(rows) >= total:
            break
        # Results come cheapest first, so a whole page over the cap means the rest are too.
        if price_max and min(r["price"] for r in found) > price_max:
            break
        time.sleep(2)
    if not rows:
        raise SourceError("truecar: zero listings for %s" % search["label"])
    return finish(list(rows.values()), search)


def kbb_url(search, first):
    url = (
        "https://www.kbb.com/rest/lsc/listing?makeCode={make}&modelCode={model}&zip={zip}"
        "&searchRadius={radius}&listingType=USED&startYear={year_min}&endYear={year_max}"
        "&sortBy=derivedpriceASC&numRecords={size}&firstRecord={first}"
    ).format(**dict(search, make=search["make"].upper(), model=KBB_MODELS[search["model"]],
                    size=KBB_PAGE, first=first))
    return url + ("&maxPrice=%d" % search["price_max"] if search.get("price_max") else "")


def parse_kbb(text):
    """Return (rows, total_count) from one page of KBB's listing feed."""
    try:
        data = json.loads(text)
    except ValueError as err:
        raise SourceError("kbb: no listing data (blocked or redesigned)") from err
    rows = []
    for item in data.get("listings") or []:
        pricing = item.get("pricingDetail") or {}
        vin, price = item.get("vin"), pricing.get("salePrice")
        if not vin or not price:
            continue
        owner = item.get("owner") or {}
        address = (owner.get("location") or {}).get("address") or {}
        # AutoCheck's free preview. A missing entry means not checked, not clean.
        history = item.get("vhrPreview") or []
        pictures = (item.get("images") or {}).get("sources") or []
        picked = min((item.get("images") or {}).get("primary") or 0, len(pictures) - 1)
        distance = owner.get("distanceFromSearch")
        rows.append({
            "vin": vin,
            "source": "kbb",
            "url": "https://www.kbb.com/cars-for-sale/vehicle/%s" % item.get("id"),
            "year": item.get("year"),
            "make": (item.get("make") or {}).get("name"),
            "model": (item.get("model") or {}).get("name"),
            "trim": (item.get("trim") or {}).get("name"),
            "drive": (item.get("driveType") or {}).get("name"),
            "price": round(price),
            "miles": whole((item.get("mileage") or {}).get("value")),
            "color": (item.get("color") or {}).get("exteriorColor"),
            "dealer": "Private seller" if owner.get("privateSeller") else owner.get("name"),
            "where": ", ".join(p for p in (address.get("city"), address.get("state")) if p),
            "state": address.get("state"),
            "distance": round(distance) if distance is not None else None,
            "accidents": 1 if "ACCIDENTS_REPORTED" in history else 0 if "NO_ACCIDENTS_REPORTED" in history else None,
            "owners": 1 if "ONE_OWNER" in history else None,
            "clean_title": False if "SALVAGE_TITLE" in history else True if "NO_SALVAGE_TITLE" in history else None,
            "kbb_fair": whole(pricing.get("kbbFppAmount")),
            "image": pictures[picked].get("src") if pictures else None,
        })
    return rows, data.get("totalResultCount") or 0


def scrape_kbb(search):
    rows = {}
    for page in range(MAX_PAGES):
        found, total = parse_kbb(fetch(kbb_url(search, page * KBB_PAGE)))
        for row in found:
            rows.setdefault(row["vin"], row)
        if len(found) < KBB_PAGE or len(rows) >= total:
            break
        time.sleep(2)
    return finish(list(rows.values()), search)


def cargurus_url(search, offset):
    return (
        "https://www.cargurus.com/Cars/searchResults.action?zip={zip}&distance={radius}"
        "&inventorySearchWidgetType=AUTO&shopByTypes=MIX&sortDir=ASC&sortType=PRICE"
        "&entitySelectingHelper.selectedEntity={model}&startYear={year_min}&endYear={year_max}"
        "&maxResults={size}&offset={offset}"
    ).format(**dict(search, model=CARGURUS_MODELS[search["model"]], size=CARGURUS_PAGE, offset=offset))


def parse_cargurus(text):
    """Return rows from one page of CarGurus' search feed. It carries no title or accident data."""
    try:
        data = json.loads(text)
    except ValueError as err:
        raise SourceError("cargurus: no listing data (blocked or redesigned)") from err
    if not isinstance(data, list):
        raise SourceError("cargurus: feed changed shape")
    rows = []
    for item in data:
        vin, price = item.get("vin"), item.get("price")
        if not vin or not price:
            continue
        distance = item.get("distance")
        rows.append({
            "vin": vin,
            "source": "cargurus",
            "url": "https://www.cargurus.com/details/%s" % item.get("id"),
            "year": item.get("carYear"),
            "make": item.get("makeName"),
            "model": item.get("modelName"),
            "trim": item.get("trimName"),
            "drive": item.get("driveTrain"),
            "price": round(price),
            "miles": whole(item.get("mileage")),
            "color": item.get("localizedExteriorColor"),
            "dealer": item.get("serviceProviderName"),
            "where": item.get("sellerCity"),
            "state": item.get("sellerRegion"),
            "distance": round(distance) if distance is not None else None,
            "accidents": None,
            "owners": None,
            "clean_title": None,
            "deal": CARGURUS_DEALS.get(item.get("dealRating")),
            "deal_by": "CarGurus",
            "cg_value": whole(item.get("expectedPrice")),
            "image": (item.get("originalPictureData") or {}).get("url"),
        })
    return rows


def scrape_cargurus(search):
    rows = {}
    price_max = search.get("price_max")
    for page in range(MAX_PAGES):
        found = parse_cargurus(fetch(cargurus_url(search, page * CARGURUS_PAGE)))
        new = [row for row in found if row["vin"] not in rows]
        for row in new:
            rows[row["vin"]] = row
        # Cheapest first, so a page with nothing under the cap ends the search.
        if len(found) < CARGURUS_PAGE or not new or (price_max and min(r["price"] for r in found) > price_max):
            break
        time.sleep(2)
    return finish(list(rows.values()), search)


def carmax_url(search):
    return "https://www.carmax.com/cars/{make}/{model}".format(**search)


def parse_carmax(page):
    """Rows from CarMax's search page: the nearest stores' cars, any year."""
    start = page.find("const cars = ")
    if start < 0:
        raise SourceError("carmax: no listing data (blocked or redesigned)")
    try:
        cars, _ = json.JSONDecoder().raw_decode(page, start + len("const cars = "))
    except ValueError as err:
        raise SourceError("carmax: listing data changed shape (%s)" % err) from err
    rows = []
    for item in cars:
        vin, price = item.get("vin"), item.get("basePrice")
        if not vin or not price or item.get("isSaleable") is False:
            continue
        distance = item.get("distance")
        rows.append({
            "vin": vin,
            "source": "carmax",
            "url": "https://www.carmax.com/car/%s" % item.get("stockNumber"),
            "year": item.get("year"),
            "make": item.get("make"),
            "model": item.get("model"),
            "trim": item.get("trim"),
            "price": round(price),
            "miles": whole(item.get("mileage")),
            "color": item.get("exteriorColor"),
            "dealer": "CarMax %s" % (item.get("storeName") or ""),
            "where": ", ".join(p for p in (item.get("storeCity"), item.get("stateAbbreviation")) if p),
            "state": item.get("stateAbbreviation"),
            "distance": round(distance) if distance is not None else None,
            "accidents": None,
            "owners": None,
            "clean_title": None,
            "image": item.get("heroImageUrl"),
        })
    return rows


def scrape_carmax(search):
    return finish(in_years(parse_carmax(fetch_curl(carmax_url(search))), search), search)


def carscom_url(search):
    url = (
        "https://www.cars.com/shopping/results/?makes[]={make}&models[]={make}-{slug}&zip={zip}"
        "&maximum_distance={radius}&stock_type=used&sort=list_price&year_min={year_min}&year_max={year_max}"
    ).format(**dict(search, slug=search["model"].replace("-", "_")))
    return url + ("&list_price_max=%d" % search["price_max"] if search.get("price_max") else "")


def parse_carscom(page):
    """Rows from one Cars.com results page. It only searches inside the radius, and gives no distance."""
    match = re.search(r'data-vehicle-array="([^"]*)"', page)
    if not match:
        raise SourceError("cars.com: no listing data (bot check or redesigned)")
    try:
        cars = json.loads(html.unescape(match.group(1)))
    except ValueError as err:
        raise SourceError("cars.com: listing data changed shape (%s)" % err) from err
    rows = []
    for item in cars:
        vin, price = item.get("vin"), whole(item.get("price"))
        if not vin or not price:
            continue
        seller = item.get("seller") or {}
        rows.append({
            "vin": vin,
            "source": "cars.com",
            "url": "https://www.cars.com/vehicledetail/%s/" % item.get("listingId"),
            "year": whole(item.get("year")),
            "make": item.get("make"),
            "model": item.get("model"),
            "trim": item.get("trim"),
            "drive": item.get("drivetrain"),
            "price": price,
            "miles": whole(item.get("mileage")),
            "color": (item.get("exteriorColor") or "").title() or None,
            "dealer": seller.get("dealerName"),
            "where": ", ".join(p for p in (seller.get("city"), seller.get("state")) if p),
            "state": seller.get("state"),
            "distance": None,
            "accidents": None,
            "owners": None,
            "clean_title": None,
            "image": item.get("primaryThumbnail"),
        })
    return rows


def scrape_carscom(search):
    return finish(in_years(parse_carscom(browser_page(carscom_url(search))), search), search)


def carvana_url(search):
    model = search["model"].replace("-", " ").title()
    wanted = {"filters": {"makes": [{"name": search["make"].title(), "models": [{"name": model}]}],
                          "year": {"min": search["year_min"], "max": search["year_max"]}},
              "sortBy": "LowestPrice"}
    packed = base64.urlsafe_b64encode(json.dumps(wanted, separators=(",", ":")).encode()).decode().rstrip("=")
    return "https://www.carvana.com/cars/filters?cvnaid=" + packed


def parse_carvana(page):
    """Rows from one Carvana results page. Carvana delivers, so there is no lot and no distance."""
    blocks = re.findall(r'<script type="application/ld\+json" data-testid="vehicle-ld"[^>]*>(.*?)</script>', page, re.S)
    if not blocks:
        raise SourceError("carvana: no listing data (bot check or redesigned)")
    rows = []
    for block in blocks:
        try:
            item = json.loads(block)
        except ValueError:
            continue
        offer = item.get("offers") or {}
        vin, price = item.get("vehicleIdentificationNumber"), offer.get("price")
        if not vin or not price or not offer.get("url"):
            continue
        # "Used 2023 Tesla Model 3 Standard with 32238 miles - $28,990" -> "Standard"
        trim = re.search(r"%s (.*?) with \d" % re.escape(item.get("model") or ""), item.get("description") or "")
        rows.append({
            "vin": vin,
            "source": "carvana",
            "url": offer["url"],
            "year": whole(item.get("modelDate")),
            "make": item.get("manufacturer"),
            "model": item.get("model"),
            "trim": trim.group(1) if trim else None,
            "price": round(price),
            "miles": whole(item.get("mileageFromOdometer")),
            "color": item.get("color"),
            "dealer": "Carvana",
            "where": "Delivered by Carvana",
            "state": None,
            "distance": None,
            "accidents": None,
            "owners": None,
            "clean_title": None,
            "image": item.get("image"),
        })
    return rows


def scrape_carvana(search):
    return finish(in_years(parse_carvana(browser_page(carvana_url(search))), search), search)


def craigslist_url(search):
    url = (
        "https://{site}.craigslist.org/search/cta?auto_make_model={make}%20{words}&postal={zip}"
        "&search_distance={radius}&min_auto_year={year_min}&max_auto_year={year_max}&min_price={low}"
    ).format(**dict(search, site=CRAIGSLIST_SITE, words=search["model"].replace("-", "%20"), low=CRAIGSLIST_MIN_PRICE))
    return url + ("&max_price=%d" % search["price_max"] if search.get("price_max") else "")


def parse_craigslist_search(page):
    """The postings on a Craigslist search page: link, price, photo and town. The rest is on each posting."""
    if "craigslist" not in page:
        raise SourceError("craigslist: not a search page (blocked?)")
    extras = []
    data = re.search(r'<script[^>]*id="ld_searchpage_results"[^>]*>(.*?)</script>', page, re.S)
    if data:
        try:
            extras = [entry.get("item") or {} for entry in json.loads(data.group(1)).get("itemListElement") or []]
        except ValueError:
            extras = []
    cards = re.findall(
        r'<li class="cl-static-search-result"[^>]*>\s*<a href="([^"]+)">.*?<div class="price">([^<]*)</div>', page, re.S)
    found = []
    for index, (url, price) in enumerate(cards):
        price = whole(re.sub(r"[^0-9.]", "", price) or None)
        if not price:
            continue
        extra = extras[index] if len(extras) == len(cards) else {}
        address = ((extra.get("offers") or {}).get("availableAtOrFrom") or {}).get("address") or {}
        found.append({
            "url": url,
            "price": price,
            "image": (extra.get("image") or [None])[0],
            "where": ", ".join(p for p in (address.get("addressLocality"), address.get("addressRegion")) if p),
            "state": address.get("addressRegion") or None,
        })
    return found


def parse_craigslist_listing(page, found):
    """One row from a Craigslist posting, or None when the posting is gone or is not a car."""
    def attr(name):
        match = re.search(r'<div class="attr %s">.*?<span class="valu[^"]*">(.*?)</span>' % name, page, re.S)
        return html.unescape(re.sub(r"<[^>]+>", "", match.group(1))).strip() if match else None

    year = re.search(r'<span class="valu year">\s*(\d{4})', page)
    name = re.search(r'<span class="valu makemodel">(.*?)</span>', page, re.S)
    if not year or not name:
        return None
    words = html.unescape(re.sub(r"<[^>]+>", "", name.group(1))).split()
    cut = 3 if len(words) > 2 and words[1].lower() == "model" else 2
    title = (attr("auto_title_status") or "").lower()
    return {
        "vin": (attr("auto_vin") or "").upper() or "craigslist-" + found["url"].rstrip("/").rsplit("/", 1)[-1],
        "source": "craigslist",
        "url": found["url"],
        "year": int(year.group(1)),
        "make": words[0].title() if words else None,
        "model": " ".join(words[1:cut]).title() or None,
        "trim": " ".join(words[cut:]) or None,
        "drive": attr("auto_drivetrain"),
        "price": found["price"],
        "miles": whole(re.sub(r"[^0-9]", "", attr("auto_miles") or "") or None),
        "color": (attr("auto_paint") or "").title() or None,
        "dealer": "Craigslist seller",
        "where": found["where"],
        "state": found["state"],
        "distance": None,
        "accidents": None,
        "owners": None,
        # A seller saying "clean" proves nothing; a seller admitting salvage does.
        "clean_title": False if title in CRAIGSLIST_BAD_TITLES else None,
        "image": found["image"],
    }


def scrape_craigslist(search):
    rows = []
    for found in parse_craigslist_search(fetch(craigslist_url(search)))[:CRAIGSLIST_MAX]:
        try:
            row = parse_craigslist_listing(fetch(found["url"]), found)
        except SourceError:
            continue  # one posting pulled mid-run should not sink the rest
        if row:
            rows.append(row)
        time.sleep(1)
    return finish(in_years(same_model(rows, search), search), search)


SCRAPERS = (
    ("craigslist", scrape_craigslist),
    ("truecar", scrape_truecar), ("kbb", scrape_kbb), ("cargurus", scrape_cargurus),
    ("carmax", scrape_carmax), ("cars.com", scrape_carscom), ("carvana", scrape_carvana),
)


def combine(rows):
    """One row per car. The cheapest listing leads; the rest fill its gaps and become extra links."""
    by_vin = {}
    for row in rows:
        by_vin.setdefault(row["vin"], []).append(row)
    order = {name: place for place, name in enumerate(SOURCE_ORDER)}
    combined = []
    for group in by_vin.values():
        group.sort(key=lambda r: (r["price"], order.get(r.get("source"), len(order))))
        lead = dict(group[0])
        for other in group[1:]:
            for key, value in other.items():
                if lead.get(key) is None and value is not None:
                    lead[key] = value
        titles = {r["clean_title"] for r in group if r.get("clean_title") is not None}
        if len(titles) > 1:
            lead["clean_title"], lead["title_conflict"] = None, True
        accidents = [r["accidents"] for r in group if r.get("accidents") is not None]
        if accidents:
            lead["accidents"] = max(accidents)
        lead["sources"] = [r.get("source") for r in group]
        lead["also"] = [{"source": r.get("source"), "url": r.get("url"), "price": r["price"]} for r in group[1:]]
        combined.append(lead)
    return combined


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
        if row.get("title_conflict"):
            flags.append("title-conflict")
        if row.get("accidents"):
            flags.append("accident")
        market = row.get("kbb_fair") or row.get("cg_value")
        if market:
            limit = FAR_BELOW_MARKET_CLEAN if row.get("clean_title") is True else FAR_BELOW_MARKET
            too_cheap = row["price"] < limit * market
        else:
            # No market value to compare with: a low price and no verified clean title is the warning.
            too_cheap = row["price"] < config["suspicious_below"] and row.get("clean_title") is not True
        if row.get("price") and too_cheap:
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
        sources = set(row.get("sources") or [row.get("source")])
        last_seen = dt.date.fromisoformat(row.get("last_seen", today))
        if sources & set(failed_sources):
            row["gone"] = False  # the source failed, the car did not sell
        elif sources <= SAMPLED_SOURCES:
            if (dt.date.fromisoformat(today) - last_seen).days > SAMPLED_KEEP_DAYS:
                continue
            row["gone"] = False
        elif last_seen < cutoff:
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

    # One browser session for every page that needs one. Alternating sites keeps each one slow.
    browser_load([make_url(search) for search in config["searches"] for make_url in (carscom_url, carvana_url)])

    rows, errors, failed = [], [], set()
    for search in config["searches"]:
        for name, scraper in SCRAPERS:
            try:
                found = scraper(search)
                rows.extend(found)
                print("%s: %d listings for %s" % (name, len(found), search["label"]))
            except SourceError as err:
                errors.append(str(err))
                failed.add(name)
                print("FAILED %s" % err, file=sys.stderr)
    scraped = len(rows)
    manual = load_manual()
    rows.extend(manual)
    print("manual: %d listings" % len(manual))
    rows = combine(rows)

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
    # One site failing still publishes the others (the page names the site that failed).
    # Non-zero only when nothing was scraped at all, so the last good data stays live.
    return 1 if errors and not scraped else 0


if __name__ == "__main__":
    sys.exit(main())
