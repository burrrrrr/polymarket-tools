"""Whether Polymarket takes new orders from where this process connects.

The block is applied to the address that submits the order, so it is worth
knowing before an order matters. Two signals are read, because either alone
clears places it should not:

  1. The CLOB's own edge. It answers a refused address with 451, and it refuses
     addresses it distrusts, such as a shared VPN exit in an open country,
     which no published list predicts.
  2. The country and region tables below. They catch the close-only places,
     where reads succeed and only a new order is rejected.

The tables are a snapshot of https://docs.polymarket.com/developers/CLOB/geoblock.
A place they cannot classify is allowed, so a change to the published list
cannot stop a run.
"""

from typing import NamedTuple

import httpx

from polymarket.hosts import CLOB_HOST, GEOBLOCK_URL

TRACE_URL = f"{CLOB_HOST}/cdn-cgi/trace"
TIMEOUT_S = 6.0

# Where the API rejects new orders: the docs' close-only list and the fully
# blocked places.
API_BLOCKED_COUNTRIES = frozenset({
    "AU", "BE", "BI", "BR", "BY", "CD", "CF", "CU", "DE", "ET", "FR", "GB",
    "IQ", "IR", "IT", "KP", "LB", "LY", "MM", "NI", "NZ", "PL", "RU", "SD",
    "SG", "SK", "SO", "SS", "SY", "TH", "TW", "UM", "US", "VE", "YE", "ZW",
})

# Countries blocked in part, with the subdivisions that are blocked.
API_BLOCKED_REGIONS = {
    "CA": frozenset({"AB", "BC", "ON", "QC"}),
    "UA": frozenset({"09", "14", "43"}),  # Luhansk, Donetsk, Crimea
}

# Blocked on the website and not on the API.
FRONTEND_ONLY_COUNTRIES = frozenset({"IE", "JP", "MT", "NL"})

GEOBLOCK_MARKER = "restricted in your region"

_UNREADABLE = (httpx.HTTPError, ValueError, AttributeError)


class Verdict(NamedTuple):
    allowed: bool
    message: str


def read_region(http):
    """(country, region) as Polymarket sees this address, in upper case.

    The country falls back to the one the CLOB's CDN edge reports, which knows
    no region. A part that cannot be read is None.
    """
    try:
        data = http.get(GEOBLOCK_URL, timeout=TIMEOUT_S).raise_for_status().json()
        # data["blocked"] is the website's verdict, which blocks places the API accepts.
        country = str(data.get("country") or "").upper()
        if country:
            return country, str(data.get("region") or "").upper() or None
    except _UNREADABLE:
        pass
    try:
        text = http.get(TRACE_URL, timeout=TIMEOUT_S).raise_for_status().text
    except _UNREADABLE:
        return None, None
    fields = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    return fields.get("loc", "").upper() or None, None


def verdict(country, region):
    """Whether the API takes new orders from this country and region."""
    if not country:
        return Verdict(True, "region unreadable, not checked")
    where = _where(country, region)
    partial = API_BLOCKED_REGIONS.get(country, frozenset())
    if country in API_BLOCKED_COUNTRIES or region in partial:
        return Verdict(False, f"{where}: the API rejects new orders from here")
    if partial and not region:
        return Verdict(True, f"{where}: partly blocked country with no region read, not checked")
    if country in FRONTEND_ONLY_COUNTRIES:
        return Verdict(True, f"{where}: new orders permitted (blocked on the website only)")
    return Verdict(True, f"{where}: new orders permitted")


def check(http):
    """The Verdict for the address this process connects from."""
    country, region = read_region(http)
    if _status(http, f"{CLOB_HOST}/time") == 451:
        return Verdict(False, f"{_where(country, region)}: the CLOB refuses this address (451), "
                              "a block on the address itself and not a country rule")
    return verdict(country, region)


def is_geoblocked(exc):
    """Whether an order was rejected for the address that sent it. Retrying
    such an order cannot succeed."""
    status = getattr(exc, "status_code", None)
    if status == 451:
        return True
    if status is not None and status != 403:
        return False
    message = getattr(exc, "error_msg", None)
    return GEOBLOCK_MARKER in str(exc if message is None else message).lower()


def _where(country, region):
    if not country:
        return "unknown region"
    return f"{country}-{region}" if region else country


def _status(http, url):
    try:
        return http.get(url, timeout=TIMEOUT_S).status_code
    except httpx.HTTPError:
        return None
