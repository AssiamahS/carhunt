# carhunt

One page for the cars I'm shopping for. Listings are pulled every 6 hours and
published with GitHub Pages from `docs/`.

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

- Change the car, area, radius or budget in `config.json`.
- Add a car you found elsewhere to `manual.json` (`url`, `price`, `miles`, `trim`, `where`).
- Each run keeps price history per VIN, so drops show up on the page. Cars that
  disappear are kept as "sold or delisted" for 14 days.

## Flags

- **branded title** - the listing itself says the title is not clean
- **too cheap** - under `suspicious_below` with no verified clean title
- **accident reported**
- **basic warranty expired** - past `basic_warranty_miles`
- **ask about buyback** - set by hand in `manual.json`
