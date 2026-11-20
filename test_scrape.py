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


class SearchLimitTest(unittest.TestCase):
    def test_price_and_mileage_caps(self):
        rows = [
            {"vin": "OK", "price": 29000, "miles": 80000},
            {"vin": "PRICEY", "price": 41000, "miles": 20000},
            {"vin": "WORN", "price": 22000, "miles": 120000},
            {"vin": "NOMILES", "price": 25000, "miles": None},
        ]
        kept = [r["vin"] for r in scrape.within_limits(rows, {"price_max": 40000, "miles_max": 100000})]
        self.assertEqual(kept, ["OK", "NOMILES"])
        self.assertEqual(len(scrape.within_limits(rows, {})), 4)

    def test_juniper_status(self):
        status = scrape.juniper_status
        self.assertEqual(status({"model": "Model Y", "year": 2026, "trim": "Premium RWD"}), "yes")
        self.assertEqual(status({"model": "Model Y", "year": 2025, "trim": "Long Range Launch Series"}), "yes")
        # US cars with the new body are all model year 2026: every 2025 checked by photo was the old one.
        self.assertEqual(status({"model": "Model Y", "year": 2025, "trim": "Long Range RWD"}), "no")
        self.assertEqual(status({"model": "Model Y", "year": 2023, "trim": "Long Range AWD"}), "no")
        self.assertIsNone(status({"model": "Model 3", "year": 2026, "trim": "RWD"}))

    def test_search_applies_caps_and_stops_paging_past_the_price_cap(self):
        pages = [
            page([{"vin": "CHEAP", "price": 30000, "miles": 50000}], total=90, has_next=True),
            page([{"vin": "OVER", "price": 46000, "miles": 10000}], total=90, has_next=True),
            page([{"vin": "NEVER", "price": 20000, "miles": 10}], total=90, has_next=False),
        ]
        calls = []
        real_fetch, real_sleep = scrape.fetch, scrape.time.sleep
        scrape.fetch = lambda url: calls.append(url) or pages[len(calls) - 1]
        scrape.time.sleep = lambda seconds: None
        try:
            search = {"label": "Test", "make": "tesla", "model": "model-s", "year_min": 2017, "year_max": 2023,
                      "city": "union", "state": "nj", "radius": 150, "price_max": 45000}
            rows = scrape.scrape_truecar(search)
        finally:
            scrape.fetch, scrape.time.sleep = real_fetch, real_sleep
        self.assertEqual([r["vin"] for r in rows], ["CHEAP"])
        self.assertEqual(len(calls), 2)
        self.assertIn("/tesla/model-s/year-2017-2023/", calls[0])


class DirectLinkTest(unittest.TestCase):
    def test_only_cars_with_their_own_link_and_photo_count(self):
        self.assertTrue(scrape.is_direct({"url": "https://x/vin/1", "image": "https://x/1.jpg"}))
        self.assertFalse(scrape.is_direct({"url": "https://x/search", "image": "https://x/1.jpg", "exact": False}))
        self.assertFalse(scrape.is_direct({"url": "https://x/vin/1"}))
        self.assertFalse(scrape.is_direct({"image": "https://x/1.jpg"}))


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


KBB_ITEM = {
    "id": 789325621, "vin": "5YJ3E1EA3RF739738", "year": 2024,
    "make": {"name": "Tesla"}, "model": {"name": "Model 3"}, "trim": {"name": "Standard Range"},
    "driveType": {"name": "RWD"}, "color": {"exteriorColor": "Gray"}, "mileage": {"value": "41,535"},
    "pricingDetail": {"salePrice": 29890, "kbbFppAmount": 31730.0},
    "vhrPreview": ["NO_SALVAGE_TITLE", "ACCIDENTS_REPORTED", "ONE_OWNER"],
    "images": {"primary": 0, "sources": [{"src": "https://img.example/kbb.jpg"}]},
    "owner": {"name": "Certified Luxury Motors", "distanceFromSearch": 29.4,
              "location": {"address": {"city": "Great Neck", "state": "NY"}}},
}
CARGURUS_ITEM = {
    "id": 462267333, "vin": "5YJ3E1EB8RF730591", "carYear": 2024, "makeName": "Tesla", "modelName": "Model 3",
    "trimName": "Long Range AWD", "price": 38489.0, "expectedPrice": 38806, "mileage": 17647,
    "localizedExteriorColor": "Pearl White Multi-Coat", "serviceProviderName": "Hyundai of Trenton",
    "sellerCity": "Ewing, NJ", "sellerRegion": "NJ", "distance": 24.66, "dealRating": "GOOD_PRICE",
    "originalPictureData": {"url": "https://img.example/cg.jpg"},
}


class OtherSourcesTest(unittest.TestCase):
    def test_parses_kbb(self):
        rows, total = scrape.parse_kbb(json.dumps({"totalResultCount": 77, "listings": [KBB_ITEM, {"vin": "NOPRICE"}]}))
        self.assertEqual((total, len(rows)), (77, 1))
        row = rows[0]
        self.assertEqual(row["url"], "https://www.kbb.com/cars-for-sale/vehicle/789325621")
        self.assertEqual((row["price"], row["miles"], row["kbb_fair"]), (29890, 41535, 31730))
        self.assertEqual((row["clean_title"], row["accidents"], row["owners"]), (True, 1, 1))
        self.assertEqual((row["where"], row["distance"], row["image"]), ("Great Neck, NY", 29, "https://img.example/kbb.jpg"))

    def test_kbb_history_is_unknown_when_the_preview_is_missing(self):
        rows, _ = scrape.parse_kbb(json.dumps({"listings": [dict(KBB_ITEM, vhrPreview=None)]}))
        self.assertIsNone(rows[0]["clean_title"])
        self.assertIsNone(rows[0]["accidents"])

    def test_parses_cargurus(self):
        row = scrape.parse_cargurus(json.dumps([CARGURUS_ITEM, {"vin": "NOPRICE"}]))[0]
        self.assertEqual(row["url"], "https://www.cargurus.com/details/462267333")
        self.assertEqual((row["price"], row["miles"], row["cg_value"]), (38489, 17647, 38806))
        self.assertEqual((row["where"], row["distance"], row["deal"], row["deal_by"]), ("Ewing, NJ", 25, "GOOD", "CarGurus"))
        self.assertIsNone(row["clean_title"])

    def test_parses_carmax_and_skips_cars_that_cannot_be_bought(self):
        cars = [{"stockNumber": 70336313, "vin": "5YJ3E1EA1PF557517", "year": 2023, "make": "Tesla", "model": "Model 3",
                 "trim": None, "basePrice": 27998.0, "mileage": 42532, "storeName": "Edison", "storeCity": "Edison",
                 "stateAbbreviation": "NJ", "distance": 0.4, "exteriorColor": "White", "isSaleable": True,
                 "heroImageUrl": "https://img.example/hero.jpg"},
                {"stockNumber": 1, "vin": "RESERVED", "basePrice": 1.0, "isSaleable": False}]
        rows = scrape.parse_carmax("<script>const cars = %s;</script>" % json.dumps(cars))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["url"], row["price"], row["miles"]), ("https://www.carmax.com/car/70336313", 27998, 42532))
        self.assertEqual((row["dealer"], row["where"], row["distance"]), ("CarMax Edison", "Edison, NJ", 0))

    def test_parses_carscom(self):
        cars = [{"vin": "5YJ3E1EA4MF854793", "trim": "Standard Range Plus", "model": "Model 3", "make": "Tesla",
                 "price": "10949", "year": "2021", "mileage": "170726", "exteriorColor": "white",
                 "listingId": "2a391b72", "primaryThumbnail": "https://img.example/c.jpg",
                 "seller": {"state": "NJ", "city": "Paterson", "dealerName": "Auto Market Street"}}]
        import html as html_lib
        page_html = '<search-provider data-vehicle-array="%s"></search-provider>' % html_lib.escape(json.dumps(cars))
        row = scrape.parse_carscom(page_html)[0]
        self.assertEqual(row["url"], "https://www.cars.com/vehicledetail/2a391b72/")
        self.assertEqual((row["year"], row["price"], row["miles"], row["color"]), (2021, 10949, 170726, "White"))
        self.assertEqual((row["where"], row["distance"]), ("Paterson, NJ", None))

    def test_parses_carvana(self):
        car = {"@type": "Vehicle", "modelDate": 2023, "manufacturer": "Tesla", "model": "Model 3", "color": "White",
               "image": "https://img.example/v.jpg", "mileageFromOdometer": 32238,
               "description": "Used 2023 Tesla Model 3 Standard with 32238 miles - $28,990",
               "vehicleIdentificationNumber": "5YJ3E1EA0PF575667",
               "offers": {"price": 28990, "url": "https://www.carvana.com/vehicle/4825793"}}
        page_html = '<script type="application/ld+json" data-testid="vehicle-ld">%s</script>' % json.dumps(car)
        row = scrape.parse_carvana(page_html)[0]
        self.assertEqual((row["url"], row["price"], row["trim"]), ("https://www.carvana.com/vehicle/4825793", 28990, "Standard"))
        self.assertEqual((row["year"], row["miles"], row["distance"]), (2023, 32238, None))

    def test_browser_and_curl_sources_raise_on_a_bot_wall(self):
        for parse in (scrape.parse_carmax, scrape.parse_carscom, scrape.parse_carvana):
            with self.assertRaises(scrape.SourceError):
                parse("<html><title>Just a moment...</title></html>")
        with self.assertRaises(scrape.SourceError):
            scrape.browser_page("https://never-loaded.example/")

    def test_years_outside_the_search_are_dropped(self):
        rows = [{"vin": "A", "year": 2020}, {"vin": "B", "year": 2022}, {"vin": "C", "year": None}]
        self.assertEqual([r["vin"] for r in scrape.in_years(rows, {"year_min": 2021, "year_max": 2023})], ["B"])

    def test_carvana_url_carries_the_search(self):
        url = scrape.carvana_url({"make": "tesla", "model": "model-y", "year_min": 2026, "year_max": 2026})
        packed = url.split("cvnaid=")[1]
        wanted = json.loads(__import__("base64").urlsafe_b64decode(packed + "=" * (-len(packed) % 4)))
        self.assertEqual(wanted["filters"]["makes"], [{"name": "Tesla", "models": [{"name": "Model Y"}]}])
        self.assertEqual(wanted["filters"]["year"], {"min": 2026, "max": 2026})

    def test_a_blocked_source_raises(self):
        with self.assertRaises(scrape.SourceError):
            scrape.parse_kbb("<html>captcha</html>")
        with self.assertRaises(scrape.SourceError):
            scrape.parse_cargurus('{"error": "nope"}')

    def test_same_car_on_two_sites_becomes_one_row(self):
        truecar = {"vin": "V", "source": "truecar", "url": "t", "price": 30000, "clean_title": True, "accidents": 0, "image": None}
        kbb = {"vin": "V", "source": "kbb", "url": "k", "price": 29500, "clean_title": True, "accidents": 1,
               "image": "pic", "kbb_fair": 31000}
        other = {"vin": "W", "source": "cargurus", "url": "c", "price": 20000, "clean_title": None, "accidents": None}
        rows = {r["vin"]: r for r in scrape.combine([truecar, kbb, other])}
        car = rows["V"]
        self.assertEqual((car["source"], car["url"], car["price"]), ("kbb", "k", 29500))
        self.assertEqual(car["also"], [{"source": "truecar", "url": "t", "price": 30000}])
        self.assertEqual((car["accidents"], car["kbb_fair"], car["sources"]), (1, 31000, ["kbb", "truecar"]))
        self.assertEqual(rows["W"]["also"], [])

    def test_sites_disagreeing_on_the_title_is_flagged_not_hidden(self):
        rows = scrape.combine([
            {"vin": "V", "source": "truecar", "url": "t", "price": 42794, "clean_title": True, "accidents": 0},
            {"vin": "V", "source": "kbb", "url": "k", "price": 42794, "clean_title": False, "accidents": None},
        ])
        scrape.add_flags(rows, {"basic_warranty_miles": 50000, "suspicious_below": 21000})
        self.assertIsNone(rows[0]["clean_title"])
        self.assertEqual(rows[0]["flags"], ["title-conflict"])

    def test_far_below_market_value_is_flagged_even_with_a_clean_title(self):
        rows = [
            {"vin": "WRECK", "price": 21900, "miles": 100, "clean_title": True, "accidents": 0, "kbb_fair": 42790},
            {"vin": "DEAL", "price": 20000, "miles": 100, "clean_title": None, "accidents": None, "cg_value": 22000},
        ]
        scrape.add_flags(rows, {"basic_warranty_miles": 50000, "suspicious_below": 21000})
        self.assertEqual(rows[0]["flags"], ["too-cheap"])
        self.assertEqual(rows[1]["flags"], [])

    def test_car_from_a_failed_second_source_is_not_marked_gone(self):
        old = {"A": {"vin": "A", "price": 30000, "source": "truecar", "sources": ["truecar", "kbb"],
                     "first_seen": "2026-11-01", "last_seen": "2026-11-17", "price_history": [["2026-11-01", 30000]]}}
        merged = scrape.merge(old, [], "2026-11-18", keep_gone_days=14, failed_sources={"kbb"})
        self.assertFalse(merged[0]["gone"])


if __name__ == "__main__":
    unittest.main()
