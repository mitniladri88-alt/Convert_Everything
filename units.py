"""Unit and currency conversion: a value typed in, no file involved.

Units (length, weight, temperature, volume, area, speed, time, data size) are converted offline
with exact fractions, so 1 ft is exactly 12 in rather than 12.000000000000002.

Currency uses free, keyless exchange-rate services over the standard library's urllib (no new
dependencies). Rates are cached in memory and in a small JSON file, refreshed every few hours,
and an old cached copy is used (and labelled as such) when the services can't be reached.
"""
import json
import os
import re
import tempfile
import threading
import time
import urllib.request
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path

from errors import ConversionError

F = Fraction

# category -> (name, [(unit id, label, symbol, size in the category's base unit)], default from, default to)
# Sizes are exact definitions (e.g. the international inch is exactly 0.0254 m).
_LINEAR = {
    "length": ("Length", "m", [
        ("mm", "Millimetre", "mm", F("0.001")), ("cm", "Centimetre", "cm", F("0.01")), ("m", "Metre", "m", F(1)),
        ("km", "Kilometre", "km", F(1000)), ("in", "Inch", "in", F("0.0254")), ("ft", "Foot", "ft", F("0.3048")),
        ("yd", "Yard", "yd", F("0.9144")), ("mi", "Mile", "mi", F("1609.344")), ("nmi", "Nautical mile", "nmi", F(1852)),
        ("um", "Micrometre", "µm", F("0.000001")),
    ], "km", "mi"),
    "weight": ("Weight", "kg", [
        ("mg", "Milligram", "mg", F("0.000001")), ("g", "Gram", "g", F("0.001")), ("kg", "Kilogram", "kg", F(1)),
        ("t", "Tonne", "t", F(1000)), ("oz", "Ounce", "oz", F("0.028349523125")), ("lb", "Pound", "lb", F("0.45359237")),
        ("st", "Stone", "st", F("6.35029318")), ("ton_us", "US ton", "US ton", F("907.18474")),
        ("ton_uk", "UK ton", "UK ton", F("1016.0469088")), ("ct", "Carat", "ct", F("0.0002")),
    ], "kg", "lb"),
    "volume": ("Volume", "L", [
        ("ml", "Millilitre", "mL", F("0.001")), ("l", "Litre", "L", F(1)), ("m3", "Cubic metre", "m³", F(1000)),
        ("tsp", "Teaspoon (US)", "tsp", F("0.00492892159375")), ("tbsp", "Tablespoon (US)", "tbsp", F("0.01478676478125")),
        ("floz", "Fluid ounce (US)", "fl oz", F("0.0295735295625")), ("cup", "Cup (US)", "cup", F("0.2365882365")),
        ("pt", "Pint (US)", "pt", F("0.473176473")), ("qt", "Quart (US)", "qt", F("0.946352946")),
        ("gal", "Gallon (US)", "gal", F("3.785411784")), ("floz_uk", "Fluid ounce (UK)", "UK fl oz", F("0.0284130625")),
        ("pt_uk", "Pint (UK)", "UK pt", F("0.56826125")), ("gal_uk", "Gallon (UK)", "UK gal", F("4.54609")),
        ("in3", "Cubic inch", "in³", F("0.016387064")), ("ft3", "Cubic foot", "ft³", F("28.316846592")),
    ], "l", "gal"),
    "area": ("Area", "m²", [
        ("mm2", "Square millimetre", "mm²", F("0.000001")), ("cm2", "Square centimetre", "cm²", F("0.0001")),
        ("m2", "Square metre", "m²", F(1)), ("ha", "Hectare", "ha", F(10000)), ("km2", "Square kilometre", "km²", F(1000000)),
        ("in2", "Square inch", "in²", F("0.00064516")), ("ft2", "Square foot", "ft²", F("0.09290304")),
        ("yd2", "Square yard", "yd²", F("0.83612736")), ("ac", "Acre", "ac", F("4046.8564224")),
        ("mi2", "Square mile", "mi²", F("2589988.110336")),
    ], "m2", "ft2"),
    "speed": ("Speed", "m/s", [
        ("mps", "Metre per second", "m/s", F(1)), ("kmh", "Kilometre per hour", "km/h", F(1000, 3600)),
        ("mph", "Mile per hour", "mph", F("0.44704")), ("kn", "Knot", "kn", F(1852, 3600)),
        ("fps", "Foot per second", "ft/s", F("0.3048")),
    ], "kmh", "mph"),
    "time": ("Time", "s", [
        ("ns", "Nanosecond", "ns", F("0.000000001")), ("us", "Microsecond", "µs", F("0.000001")),
        ("ms", "Millisecond", "ms", F("0.001")), ("s", "Second", "s", F(1)), ("min", "Minute", "min", F(60)),
        ("h", "Hour", "h", F(3600)), ("d", "Day", "d", F(86400)), ("wk", "Week", "wk", F(604800)),
        ("mo", "Month (average)", "mo", F(2629746)), ("yr", "Year (average)", "yr", F(31556952)),
    ], "h", "min"),
    "data": ("Data size", "B", [
        ("bit", "Bit", "bit", F(1, 8)), ("B", "Byte", "B", F(1)),
        ("KB", "Kilobyte", "KB", F(1000)), ("MB", "Megabyte", "MB", F(10**6)), ("GB", "Gigabyte", "GB", F(10**9)),
        ("TB", "Terabyte", "TB", F(10**12)), ("PB", "Petabyte", "PB", F(10**15)),
        ("KiB", "Kibibyte", "KiB", F(1024)), ("MiB", "Mebibyte", "MiB", F(1024**2)), ("GiB", "Gibibyte", "GiB", F(1024**3)),
        ("TiB", "Tebibyte", "TiB", F(1024**4)),
        ("kbit", "Kilobit", "kbit", F(125)), ("Mbit", "Megabit", "Mbit", F(125000)), ("Gbit", "Gigabit", "Gbit", F(125000000)),
    ], "GB", "GiB"),
}

# Temperature isn't a plain scale, so each unit has a to-kelvin and from-kelvin function.
_TEMPERATURE = [
    ("c", "Celsius", "°C", lambda v: v + F("273.15"), lambda k: k - F("273.15")),
    ("f", "Fahrenheit", "°F", lambda v: (v - 32) * F(5, 9) + F("273.15"), lambda k: (k - F("273.15")) * F(9, 5) + 32),
    ("k", "Kelvin", "K", lambda v: v, lambda k: k),
    ("r", "Rankine", "°R", lambda v: v * F(5, 9), lambda k: k * F(9, 5)),
]

ORDER = ["length", "weight", "temperature", "volume", "area", "speed", "time", "data", "currency"]

# Common currency names; codes the rate service adds beyond these show by code alone.
CURRENCY_NAMES = {
    "USD": "US dollar", "EUR": "Euro", "GBP": "British pound", "INR": "Indian rupee", "JPY": "Japanese yen",
    "CNY": "Chinese yuan", "AUD": "Australian dollar", "CAD": "Canadian dollar", "CHF": "Swiss franc",
    "HKD": "Hong Kong dollar", "SGD": "Singapore dollar", "NZD": "New Zealand dollar", "SEK": "Swedish krona",
    "NOK": "Norwegian krone", "DKK": "Danish krone", "PLN": "Polish złoty", "CZK": "Czech koruna",
    "HUF": "Hungarian forint", "RON": "Romanian leu", "TRY": "Turkish lira", "RUB": "Russian ruble",
    "UAH": "Ukrainian hryvnia", "ILS": "Israeli shekel", "AED": "UAE dirham", "SAR": "Saudi riyal",
    "QAR": "Qatari riyal", "KWD": "Kuwaiti dinar", "EGP": "Egyptian pound", "ZAR": "South African rand",
    "NGN": "Nigerian naira", "KES": "Kenyan shilling", "BRL": "Brazilian real", "MXN": "Mexican peso",
    "ARS": "Argentine peso", "CLP": "Chilean peso", "COP": "Colombian peso", "PEN": "Peruvian sol",
    "KRW": "South Korean won", "TWD": "New Taiwan dollar", "THB": "Thai baht", "MYR": "Malaysian ringgit",
    "IDR": "Indonesian rupiah", "PHP": "Philippine peso", "VND": "Vietnamese dong", "PKR": "Pakistani rupee",
    "BDT": "Bangladeshi taka", "LKR": "Sri Lankan rupee", "NPR": "Nepalese rupee", "ISK": "Icelandic króna",
    "BGN": "Bulgarian lev",
}

# A plain number: optional sign, digits with an optional decimal part, optional short exponent.
# The exponent is capped so a value like 1e-999999999 can't make a huge exact fraction.
_NUMBER = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d{1,3})?")
MAX_ABS = F(10) ** 30


def parse_value(raw) -> Fraction:
    text = str(raw if raw is not None else "").strip().replace(",", "").replace("_", "").replace(" ", "")
    if not text:
        raise ConversionError("Enter a number to convert.")
    if len(text) > 60 or not _NUMBER.fullmatch(text):
        raise ConversionError(f"{str(raw)[:40]!r} isn't a number.")
    value = Fraction(text)
    if abs(value) > MAX_ABS:
        raise ConversionError("That number is too large.")
    return value


def _to_decimal(value: Fraction) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 40
        return Decimal(value.numerator) / Decimal(value.denominator)


def format_number(value: Fraction, sig: int = 10) -> str:
    """Up to `sig` significant digits, thousands separators, no trailing zeros;
    scientific notation only for very large or very small numbers."""
    if value == 0:
        return "0"
    d = _to_decimal(value)
    exp = d.adjusted()
    if exp >= 15 or exp < -6:
        mantissa, e = f"{d:.{sig - 1}e}".split("e")
        mantissa = mantissa.rstrip("0").rstrip(".")
        return f"{mantissa}e{int(e):+d}"
    places = max(0, sig - 1 - exp)
    text = f"{d.quantize(Decimal(1).scaleb(-places)):,f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def categories_payload() -> list:
    out = []
    for cid in ORDER:
        if cid == "currency":
            out.append({"id": "currency", "name": "Currency", "online": True, "default_from": "USD", "default_to": "EUR", "units": []})
        elif cid == "temperature":
            out.append({"id": cid, "name": "Temperature", "default_from": "c", "default_to": "f",
                        "units": [{"id": u, "label": label, "symbol": sym} for u, label, sym, _, _ in _TEMPERATURE]})
        else:
            name, _base, units, dfrom, dto = _LINEAR[cid]
            out.append({"id": cid, "name": name, "default_from": dfrom, "default_to": dto,
                        "units": [{"id": u, "label": label, "symbol": sym} for u, label, sym, _ in units]})
    return out


def _convert_all(category: str, value: Fraction, src: str) -> dict:
    """Return {unit id: converted value} for every unit in a category."""
    if category == "temperature":
        table = {u: (to_k, from_k) for u, _, _, to_k, from_k in _TEMPERATURE}
        if src not in table:
            raise ConversionError(f"Unknown temperature unit: {src}")
        kelvin = table[src][0](value)
        if kelvin < 0:
            raise ConversionError("That's below absolute zero (0 K, −273.15 °C, −459.67 °F).")
        return {u: from_k(kelvin) for u, (_, from_k) in table.items()}
    if category not in _LINEAR:
        raise ConversionError(f"Unknown category: {category}")
    sizes = {u: size for u, _, _, size in _LINEAR[category][2]}
    if src not in sizes:
        raise ConversionError(f"Unknown {category} unit: {src}")
    base = value * sizes[src]
    return {u: base / size for u, size in sizes.items()}


def _symbols(category: str) -> dict:
    if category == "temperature":
        return {u: sym for u, _, sym, _, _ in _TEMPERATURE}
    return {u: sym for u, _, sym, _ in _LINEAR[category][2]}


def convert_units(category: str, raw_value, src: str, dst: str) -> dict:
    value = parse_value(raw_value)
    results = _convert_all(category, value, src)
    if dst not in results:
        raise ConversionError(f"Unknown {category} unit: {dst}")
    symbols = _symbols(category)
    return {
        "category": category, "from": src, "to": dst,
        "value": format_number(value), "result": format_number(results[dst]),
        "result_raw": float(results[dst]),
        "all": [{"unit": u, "symbol": symbols[u], "value": format_number(v)} for u, v in results.items()],
    }


# ---------------------------------------------------------------- currency

RATES_TTL = int(os.environ.get("RATES_TTL_SECONDS", str(6 * 3600)))
RETRY_AFTER_FAILURE = 60          # don't hammer the services (or stall requests) while offline
FETCH_TIMEOUT = 4
CACHE_FILE = Path(os.environ.get("RATES_CACHE_FILE", str(Path(tempfile.gettempdir()) / "convert-anything-rates.json")))
USER_AGENT = "convert-anything/1.0"

# Tried in order. Both are free and need no key; rates are quoted against USD.
SOURCES = [
    ("ExchangeRate-API", "https://www.exchangerate-api.com", "https://open.er-api.com/v6/latest/USD"),
    ("Frankfurter (ECB)", "https://www.frankfurter.app", "https://api.frankfurter.app/latest?from=USD"),
]

_lock = threading.Lock()
_cache = {"rates": None, "fetched_at": 0.0, "updated": None, "source": None, "source_url": None}
_last_failure = 0.0


class CurrencyUnavailable(ConversionError):
    """No exchange rates: the services can't be reached and nothing is cached."""


def _http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:  # noqa: S310 (fixed https URLs)
        return json.loads(resp.read(512 * 1024))


def _parse_source(name: str, data: dict):
    """Return (rates against USD, date string) from either service's JSON."""
    if name.startswith("ExchangeRate"):
        if data.get("result") != "success":
            raise ValueError("service reported an error")
        rates, updated = data["rates"], time.strftime("%Y-%m-%d", time.gmtime(int(data["time_last_update_unix"])))
    else:
        rates, updated = dict(data["rates"]), str(data["date"])
    rates = {str(k).upper(): float(v) for k, v in rates.items() if isinstance(v, (int, float)) and v > 0}
    rates["USD"] = 1.0
    if len(rates) < 10:
        raise ValueError("too few rates")
    return rates, updated


def _fetch():
    errors = []
    for name, home, url in SOURCES:
        try:
            rates, updated = _parse_source(name, _http_json(url))
            return {"rates": rates, "fetched_at": time.time(), "updated": updated, "source": name, "source_url": home}
        except Exception as exc:  # network, HTTP, JSON or shape problems: try the next service
            errors.append(f"{name}: {exc}")
    raise OSError("; ".join(errors))


def _load_file():
    try:
        data = json.loads(CACHE_FILE.read_text())
        if isinstance(data.get("rates"), dict) and data["rates"]:
            return data
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _save_file(entry):
    try:
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(entry))
        tmp.replace(CACHE_FILE)
    except OSError:
        pass  # the in-memory copy still works


def get_rates() -> dict:
    """Current rates (refetched when older than RATES_TTL), or the last cached copy marked stale."""
    global _last_failure
    with _lock:
        if _cache["rates"] is None:
            saved = _load_file()
            if saved:
                _cache.update({k: saved.get(k) for k in _cache})
        fresh = _cache["rates"] is not None and time.time() - (_cache["fetched_at"] or 0) < RATES_TTL
        if not fresh and time.time() - _last_failure >= RETRY_AFTER_FAILURE:
            try:
                entry = _fetch()
                _cache.update(entry)
                _save_file(entry)
                fresh = True
            except OSError:
                _last_failure = time.time()
        if _cache["rates"] is None:
            raise CurrencyUnavailable(
                "Currency rates can't be loaded right now because the exchange-rate service isn't reachable. "
                "Unit conversions still work offline. Try currency again in a minute.")
        return {**_cache, "stale": not fresh}


def _rates_meta(info: dict) -> dict:
    meta = {"updated": info["updated"], "source": info["source"], "source_url": info["source_url"], "stale": info["stale"]}
    if info["stale"]:
        meta["notice"] = (f"Couldn't reach the exchange-rate service, so these are saved rates from {info['updated']}. "
                          "They may be out of date.")
    return meta


def currencies_payload() -> dict:
    info = get_rates()
    codes = sorted(info["rates"], key=lambda c: (c not in CURRENCY_NAMES, c))
    return {"currencies": [{"code": c, "name": CURRENCY_NAMES.get(c, c)} for c in codes], **_rates_meta(info)}


def _money(value: Fraction) -> str:
    # Two decimals for everyday amounts; more significant digits for tiny ones.
    if value != 0 and abs(value) < F(1, 100):
        return format_number(value, 4)
    d = _to_decimal(value).quantize(Decimal("0.01"))
    return f"{d:,f}"


def convert_currency(raw_value, src: str, dst: str) -> dict:
    value = parse_value(raw_value)
    src, dst = str(src or "").upper(), str(dst or "").upper()
    info = get_rates()
    rates = info["rates"]
    for code in (src, dst):
        if code not in rates:
            raise ConversionError(f"No exchange rate for {code or 'that currency'}.")
    rate = F(str(rates[dst])) / F(str(rates[src]))
    result = value * rate
    return {
        "category": "currency", "from": src, "to": dst,
        "value": format_number(value), "result": _money(result), "result_raw": float(result),
        "rate": f"1 {src} = {format_number(rate, 6)} {dst}",
        "inverse": f"1 {dst} = {format_number(1 / rate, 6)} {src}",
        **_rates_meta(info),
    }


def convert(category: str, raw_value, src: str, dst: str) -> dict:
    category = str(category or "").strip().lower()
    if category == "currency":
        return convert_currency(raw_value, src, dst)
    return convert_units(category, raw_value, str(src or ""), str(dst or ""))
