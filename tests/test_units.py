"""Tests for the Units & currency converter.

Unit conversion is offline and exact. Currency calls are mocked, so these tests never use the network.
Run with:  python -m unittest discover -s tests -v
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import units  # noqa: E402
from app import app  # noqa: E402

ER_API = {"result": "success", "time_last_update_unix": 1764201600,
          "rates": {"USD": 1, "EUR": 0.8, "GBP": 0.75, "INR": 88.5, "JPY": 150, "CAD": 1.4, "AUD": 1.5,
                    "CHF": 0.9, "CNY": 7.1, "SGD": 1.3, "XYZ": 2}}
FRANKFURTER = {"amount": 1.0, "base": "USD", "date": "2026-11-26",
               "rates": {"EUR": 0.81, "GBP": 0.76, "INR": 88.0, "JPY": 151, "CAD": 1.41, "AUD": 1.51,
                         "CHF": 0.91, "CNY": 7.2, "SGD": 1.31, "SEK": 10.5}}


def conv(category, value, src, dst):
    return units.convert(category, value, src, dst)["result"]


class UnitTests(unittest.TestCase):
    def test_exact_lengths_and_weights(self):
        self.assertEqual(conv("length", "1", "ft", "in"), "12")
        self.assertEqual(conv("length", "1", "mi", "km"), "1.609344")
        self.assertEqual(conv("length", "5", "km", "m"), "5,000")
        self.assertEqual(conv("weight", "1", "lb", "oz"), "16")
        self.assertEqual(conv("weight", "1", "st", "lb"), "14")
        self.assertEqual(conv("weight", "1,000", "g", "kg"), "1")

    def test_temperature(self):
        self.assertEqual(conv("temperature", "100", "c", "f"), "212")
        self.assertEqual(conv("temperature", "-40", "f", "c"), "-40")
        self.assertEqual(conv("temperature", "0", "c", "k"), "273.15")
        self.assertEqual(conv("temperature", "0", "k", "r"), "0")
        self.assertEqual(conv("temperature", "98.6", "f", "c"), "37")
        with self.assertRaisesRegex(units.ConversionError, "absolute zero"):
            conv("temperature", "-300", "c", "k")

    def test_other_categories(self):
        self.assertEqual(conv("volume", "1", "gal", "l"), "3.785411784")
        self.assertEqual(conv("volume", "1", "gal_uk", "pt_uk"), "8")
        self.assertEqual(conv("volume", "1", "cup", "tbsp"), "16")
        self.assertEqual(conv("area", "1", "ha", "m2"), "10,000")
        self.assertEqual(conv("area", "640", "ac", "mi2"), "1")
        self.assertEqual(conv("speed", "36", "kmh", "mps"), "10")
        self.assertEqual(conv("speed", "1", "kn", "kmh"), "1.852")
        self.assertEqual(conv("time", "2", "h", "min"), "120")
        self.assertEqual(conv("time", "1", "wk", "d"), "7")
        self.assertEqual(conv("data", "1", "GiB", "MiB"), "1,024")
        self.assertEqual(conv("data", "1", "GB", "Mbit"), "8,000")
        self.assertEqual(conv("data", "8", "bit", "B"), "1")

    def test_every_unit_round_trips(self):
        for cat in units.categories_payload():
            if cat["id"] == "currency":
                continue
            for u in cat["units"]:
                out = units.convert(cat["id"], "123.456", u["id"], cat["units"][0]["id"])
                back = units.convert(cat["id"], str(out["result_raw"]), cat["units"][0]["id"], u["id"])
                self.assertAlmostEqual(back["result_raw"], 123.456, places=6, msg=f"{cat['id']} {u['id']}")
                self.assertEqual(len(out["all"]), len(cat["units"]))

    def test_formatting(self):
        self.assertEqual(units.format_number(units.F(1, 3)), "0.3333333333")
        self.assertEqual(conv("length", "1e20", "m", "km"), "1e+17")
        self.assertEqual(conv("length", "1e-7", "m", "km"), "1e-10")
        self.assertEqual(conv("length", "-2.5", "m", "cm"), "-250")
        self.assertEqual(conv("length", "0", "m", "cm"), "0")

    def test_bad_input(self):
        for bad in ["", "abc", "1e9999999", "1e-99999", "nan", "inf", "1/2", "1" * 80, "1e40"]:
            with self.subTest(bad=bad), self.assertRaises(units.ConversionError):
                conv("length", bad, "m", "cm")
        with self.assertRaisesRegex(units.ConversionError, "Unknown"):
            conv("length", "1", "m", "parsec")
        with self.assertRaisesRegex(units.ConversionError, "Unknown category"):
            conv("mood", "1", "m", "cm")


class FakeResponse:
    def __init__(self, data):
        self.data = json.dumps(data).encode()

    def read(self, _n=-1):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class CurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patches = [
            mock.patch.object(units, "CACHE_FILE", Path(self.tmp.name) / "rates.json"),
            mock.patch.dict(units._cache, {"rates": None, "fetched_at": 0.0, "updated": None, "source": None, "source_url": None}),
            mock.patch.object(units, "_last_failure", 0.0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def serve(self, by_url):
        def fake(req, timeout=None):
            for key, data in by_url.items():
                if key in req.full_url:
                    if isinstance(data, Exception):
                        raise data
                    return FakeResponse(data)
            raise OSError("offline")
        return mock.patch("urllib.request.urlopen", side_effect=fake)

    def test_converts_with_live_rates_and_caches(self):
        with self.serve({"er-api": ER_API}) as fetch:
            out = units.convert("currency", "100", "usd", "eur")
            self.assertEqual(out["result"], "80.00")
            self.assertEqual(out["rate"], "1 USD = 0.8 EUR")
            self.assertFalse(out["stale"])
            self.assertEqual(out["source"], "ExchangeRate-API")
            self.assertEqual(units.convert("currency", "1", "EUR", "GBP")["result"], "0.94")
            self.assertEqual(units.convert("currency", "1", "JPY", "USD")["result"], "0.006667")
            self.assertEqual(fetch.call_count, 1)  # second and third calls used the cache
        self.assertTrue(units.CACHE_FILE.exists())

    def test_falls_back_to_second_service(self):
        with self.serve({"er-api": OSError("down"), "frankfurter": FRANKFURTER}):
            out = units.convert("currency", "10", "USD", "SEK")
        self.assertEqual(out["result"], "105.00")
        self.assertEqual(out["updated"], "2026-11-26")
        self.assertIn("Frankfurter", out["source"])

    def test_offline_with_nothing_cached(self):
        with self.serve({}):
            with self.assertRaises(units.CurrencyUnavailable):
                units.convert("currency", "1", "USD", "EUR")
            with app.test_client() as c:
                r = c.post("/api/units/convert", json={"category": "currency", "value": "1", "from": "USD", "to": "EUR"})
                self.assertEqual(r.status_code, 503)
                self.assertIn("still work offline", r.get_json()["error"])
                self.assertEqual(c.get("/api/units/currencies").status_code, 503)
                # units are unaffected
                self.assertEqual(c.post("/api/units/convert", json={"category": "length", "value": "1", "from": "m", "to": "cm"}).status_code, 200)

    def test_offline_uses_saved_rates_marked_stale(self):
        old = {"rates": {**ER_API["rates"]}, "fetched_at": time.time() - 10 * 86400, "updated": "2026-11-17",
               "source": "ExchangeRate-API", "source_url": "https://www.exchangerate-api.com"}
        units.CACHE_FILE.write_text(json.dumps(old))
        with self.serve({}):
            out = units.convert("currency", "2", "USD", "INR")
        self.assertEqual(out["result"], "177.00")
        self.assertTrue(out["stale"])
        self.assertIn("2026-11-17", out["notice"])

    def test_failure_backoff(self):
        with self.serve({}) as fetch:
            for _ in range(3):
                with self.assertRaises(units.CurrencyUnavailable):
                    units.get_rates()
            self.assertEqual(fetch.call_count, len(units.SOURCES))  # tried once, then waits before retrying

    def test_unknown_currency(self):
        with self.serve({"er-api": ER_API}):
            with self.assertRaisesRegex(units.ConversionError, "No exchange rate for ABC"):
                units.convert("currency", "1", "USD", "ABC")
            names = {c["code"]: c["name"] for c in units.currencies_payload()["currencies"]}
        self.assertEqual(names["EUR"], "Euro")
        self.assertEqual(names["XYZ"], "XYZ")


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_lists_categories(self):
        cats = self.client.get("/api/units").get_json()["categories"]
        self.assertEqual([c["id"] for c in cats],
                         ["length", "weight", "temperature", "volume", "area", "speed", "time", "data", "currency"])

    def test_convert_endpoint(self):
        r = self.client.post("/api/units/convert", json={"category": "temperature", "value": "37", "from": "c", "to": "f"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["result"], "98.6")
        r = self.client.post("/api/units/convert", json={"category": "length", "value": "x", "from": "m", "to": "cm"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("isn't a number", r.get_json()["error"])
        r = self.client.post("/api/units/convert", data={"category": "weight", "value": "2", "from": "kg", "to": "g"})
        self.assertEqual(r.get_json()["result"], "2,000")

    def test_page_has_units_tab(self):
        r = self.client.get("/")
        html = r.get_data(as_text=True)
        r.close()
        self.assertIn("id: 'units'", html)
        self.assertIn('id="unitsBody"', html)


if __name__ == "__main__":
    unittest.main()
