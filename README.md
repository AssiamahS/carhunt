# carhunt

One page for the cars I'm shopping for. Listings are pulled every 6 hours and
published with GitHub Pages from `docs/`.

Live: https://assiamahs.github.io/carhunt/

The refresh runs from my Mac (`refresh.sh` under launchd, `com.sly.carhunt`).
`.github/workflows/refresh.yml` does the same job on Actions and is disabled
for now.

## Where the data comes from

| Source | How |
|---|---|
| TrueCar | scraped by `scrape.py` (price, miles, trim, dealer, title, accidents, owners) |
| CarGurus, Edmunds, Cars.com, Autotrader, anything else | added by hand in `manual.json`, or with the form on the page |

CarGurus, Edmunds, Cars.com, Autotrader, KBB, CarMax, Carvana, Carfax and
Tesla all refuse plain requests, so they can't be scraped from an Action. The
page links straight into each site's search instead.

## Use it

```sh
python3 scrape.py            # refresh docs/data/listings.json
python3 scrape.py --dry-run  # summary only
python3 -m unittest -q       # tests
python3 -m http.server -d docs 8000   # preview
```

- Change the cars, area, radius or budget in `config.json`. Each entry in
  `searches` is one model. Optional per search: `price_max`, `miles_max`, and
  `juniper_only` (Model Y: keep 2026 cars, the 2025 Launch Series, and other
  2025 cars marked "confirm it's the Juniper body").
- TrueCar's data does not say whether a car has FSD. Set `"fsd": true` on a
  car in `manual.json`, or tick the box in the form on the page.
- Add a car you found elsewhere to `manual.json` (`url`, `price`, `miles`, `trim`, `where`).
- Each run keeps price history per VIN, so drops show up on the page. Cars that
  disappear are kept as "sold or delisted" for 14 days.

A car with no title or accident data (everything added by hand) shows
"History not verified" and never counts as "verified clean".

## Flags

- **branded title** - the listing itself says the title is not clean
- **too cheap** - under `suspicious_below` with no verified clean title
- **accident reported**
- **basic warranty expired** - past `basic_warranty_miles`
- **ask about buyback** - set by hand in `manual.json`
