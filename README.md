# carhunt

One page for the cars I'm shopping for. Listings are pulled every 6 hours and
published with GitHub Pages from `docs/`.

Live: https://assiamahs.github.io/carhunt/

The refresh runs on GitHub Actions (`.github/workflows/refresh-listings.yml`),
every 6 hours and on any change to the config or scraper. `refresh.sh` does the
same job from a Mac under launchd (`com.sly.carhunt`); run only one of the two.

## Where the data comes from

| Source | How |
|---|---|
| TrueCar | search pages: price, miles, trim, dealer, title, accidents, owners |
| KBB (same inventory as Autotrader) | its listing feed: price, miles, KBB fair price, AutoCheck title/accident preview |
| CarGurus | its search feed: price, miles, CarGurus market value. No history |
| CarMax | its search page, fetched with `curl` (nearest stores first). No history |
| Craigslist | its search page, then each posting for miles, title status and VIN. A seller's "clean" is not trusted; "salvage" is |
| Cars.com, Carvana | one results page per search, loaded in a real browser by `browser_fetch.mjs`. No history |
| anything else | added by hand in `manual.json` (direct link and photo required), or with the form on the page |

A car listed on more than one site is one card: the cheapest listing leads and
the others show as "Also on" links. If two sites disagree about the title the
card says so instead of picking one.

### Getting past the bot walls

Three levels, tried 2026-11-19 from a home connection:

1. Plain request (Python): TrueCar, KBB, CarGurus.
2. `curl` with a full set of browser headers: CarMax. It rejects Python's TLS handshake, not the request.
3. A real, visible Chrome parked off-screen, driven by [patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright)
   (Playwright with the automation tells removed): Cars.com, Carvana. Headless Chrome is blocked, and
   Cars.com answers a second page within a few seconds with a bot check, so it is one page per search.

`browser_fetch.mjs` loads patchright from `~/.claude/mcp-servers/apartments/node_modules/`
(override with `CARHUNT_PATCHRIGHT`). Without it those two sites are skipped and the rest still refresh.

Still refusing even the real browser: Edmunds, eBay and Tesla's own inventory.
Checked and left out: EchoPark and AutoNation load in the browser but have no store within 150 miles
(nearest: Raleigh NC and Timonium MD); OnlyUsedTesla lists nine cars nationwide with no location;
Capital One's page carries no listing data without running its scripts. Autotrader's pages
block too, but KBB's feed is the same inventory. All of it only works from a residential IP.

## Use it

```sh
python3 scrape.py            # refresh docs/data/listings.json
python3 scrape.py --dry-run  # summary only
python3 -m unittest -q       # tests
python3 -m http.server -d docs 8000   # preview
```

- Change the cars, area, radius or budget in `config.json`. Each entry in
  `searches` is one model. Optional per search: `price_max`, `miles_max`, and
  `juniper_only` (Model Y: keep only the redesigned body. On TrueCar that is every
  2026 and any Launch Series; its 2025 cars are the old body and are dropped).
- Add a car you found elsewhere to `manual.json` (`url`, `image`, `price`, `miles`, `model`, `trim`, `where`).
  `url` must be the car's own listing page and `image` a photo of it. Rows
  without both are skipped, so every car on the page is one link, one photo.
- Each run keeps price history per VIN, so drops show up on the page. Cars that
  disappear are kept as "sold or delisted" for 14 days.

A car with no title or accident data (everything added by hand) shows
"History not verified" and never counts as "verified clean".

## Flags

- **branded title** - the listing itself says the title is not clean
- **far below market** - 25% or more under KBB's fair price or CarGurus' value (35% with a
  verified clean title). With no value to compare: under `suspicious_below` and no verified clean title
- **sites disagree on the title** - one site says clean, another says branded
- **accident reported**
- **basic warranty expired** - past `basic_warranty_miles`
- **ask about buyback** - set by hand in `manual.json`
