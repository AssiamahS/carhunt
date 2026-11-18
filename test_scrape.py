"""Tests for the parts of scrape.py that decide what ends up on the page."""
import json
import unittest

import scrape


def page(listings, total=None, has_next=False):
    """Build a minimal TrueCar results page around the given listing dicts."""
    apollo = {
        "ListingDealership:D1": {
            "name": "Test Motors",
            "location": {"geolocation": {"city": "Union", "state": "NJ", "postalCode": "07083"}},
        },
        "ROOT_QUERY": {
            'marketplaceListingSearch({"first":30})': {
                "totalCount": len(listings) if total is None else total,
                "pageInfo": {"hasNextPage": has_next},
            }
        },
    }
    for item in listings:
        apollo["ConsumerSummaryListing:" + item["vin"]] = {
            "pricing": {"listPrice": item["price"]},
            "marketAnalysis": {"priceQuality": item.get("quality", "GOOD")},
            "dealership": {"__ref": "ListingDealership:D1"},
            "conditionHistory": {
                "accidentCount": item.get("accidents", 0),
                "ownerCount": 1,
                "isCleanTitle": item.get("clean", True),
            },
            "vehicle": {
                "vin": item["vin"],
                "year": 2024,
                "mileage": item["miles"],
                "make": {"name": "Tesla"},
                "model": {"name": "Model 3"},
                "style": {"name": "Long Range AWD", "trimName": "Long Range"},
                "details": {
                    "listedAt": "2026-11-06T18:45:00.000Z",
                    "driveTypeAcronym": "AWD",
                    "exteriorColorName": "Ultra Red",
                    "dealerCity": "Union",
                    "dealerState": "NJ",
                    "dealerDistanceMiles": 3.4,
                },
            },
        }
    blob = json.dumps({"props": {"pageProps": {"__APOLLO_STATE__": apollo}}})
    return '<html><script id="__NEXT_DATA__" type="application/json">%s</script></html>' % blob


class ParseTest(unittest.TestCase):
    def test_parses_listing_fields(self):
        html = page([{"vin": "5YJ3E1EB6RF807099", "price": 34793, "miles": 27213}], total=52, has_next=True)
        rows, total, has_next = scrape.parse_truecar(html)
        self.assertEqual((total, has_next), (52, True))
        row = rows[0]
        self.assertEqual(row["vin"], "5YJ3E1EB6RF807099")
        self.assertEqual(row["price"], 34793)
        self.assertEqual(row["miles"], 27213)
        self.assertEqual(row["trim"], "Long Range AWD")
        self.assertEqual(row["dealer"], "Test Motors")
        self.assertEqual(row["where"], "Union, NJ")
        self.assertEqual(row["url"], "https://www.truecar.com/used-cars-for-sale/listing/5YJ3E1EB6RF807099/")
        self.assertTrue(row["clean_title"])

    def test_cars_outside_the_radius_are_dropped(self):
        rows = [{"vin": "NEAR", "distance": 64}, {"vin": "FAR", "distance": 2396}, {"vin": "UNKNOWN", "distance": None}]
        kept = [r["vin"] for r in scrape.within_radius(rows, 150)]
        self.assertEqual(kept, ["NEAR", "UNKNOWN"])

    def test_listing_without_price_is_skipped(self):
        html = page([{"vin": "5YJ3E1EB6RF807099", "price": None, "miles": 10}])
        self.assertEqual(scrape.parse_truecar(html)[0], [])

    def test_page_without_data_raises(self):
        with self.assertRaises(scrape.SourceError):
            scrape.parse_truecar("<html>Access Denied</html>")


class FlagTest(unittest.TestCase):
    def rows(self):
        return [
            {"vin": "A", "price": 28000, "miles": 20000, "clean_title": True, "accidents": 0},
            {"vin": "B", "price": 29000, "miles": 30000, "clean_title": True, "accidents": 0},
            {"vin": "C", "price": 30000, "miles": 60000, "clean_title": True, "accidents": 1},
            {"vin": "D", "price": 17000, "miles": 15000, "clean_title": None, "accidents": None},
            {"vin": "E", "price": 27000, "miles": 12000, "clean_title": False, "accidents": 0},
        ]

    def test_flags(self):
        rows = self.rows()
        scrape.add_flags(rows, {"basic_warranty_miles": 50000, "suspicious_below": 21000})
        flags = {r["vin"]: r["flags"] for r in rows}
        self.assertEqual(flags["A"], [])
        self.assertEqual(flags["C"], ["accident", "no-basic-warranty"])
        self.assertEqual(flags["D"], ["too-cheap"])
        self.assertEqual(flags["E"], ["branded-title"])


class MergeTest(unittest.TestCase):
    def test_price_drop_is_recorded_and_missing_cars_marked_gone(self):
        old = {
            "A": {"vin": "A", "price": 30000, "first_seen": "2026-11-01", "last_seen": "2026-11-10",
                  "price_history": [["2026-11-01", 30000]]},
            "B": {"vin": "B", "price": 25000, "first_seen": "2026-11-01", "last_seen": "2026-11-10",
                  "price_history": [["2026-11-01", 25000]]},
            "OLD": {"vin": "OLD", "price": 1, "first_seen": "2026-09-01", "last_seen": "2026-10-01",
                    "price_history": [["2026-09-01", 1]]},
        }
        new = [{"vin": "A", "price": 28500}, {"vin": "N", "price": 26000}]
        merged = {r["vin"]: r for r in scrape.merge(old, new, "2026-11-18", keep_gone_days=14)}

        self.assertEqual(merged["A"]["price_history"], [["2026-11-01", 30000], ["2026-11-18", 28500]])
        self.assertEqual(merged["A"]["first_seen"], "2026-11-01")
        self.assertEqual(merged["A"]["price_change"], -1500)
        self.assertFalse(merged["A"]["gone"])
        self.assertEqual(merged["N"]["first_seen"], "2026-11-18")
        self.assertTrue(merged["B"]["gone"])
        self.assertNotIn("OLD", merged)

    def test_failed_source_does_not_mark_its_cars_gone(self):
        old = {"A": {"vin": "A", "price": 30000, "source": "truecar", "first_seen": "2026-11-01",
                     "last_seen": "2026-11-17", "price_history": [["2026-11-01", 30000]]}}
        merged = scrape.merge(old, [], "2026-11-18", keep_gone_days=14, failed_sources={"truecar"})
        self.assertFalse(merged[0]["gone"])


if __name__ == "__main__":
    unittest.main()
