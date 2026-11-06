"""Venue adapters: pull current funding for every linear perpetual and normalise it."""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

import ccxt

from .models import FundingQuote

log = logging.getLogger(__name__)

# Binance and Bybit work too, but both geo-block some regions (HTTP 451/403),
# so they are opt-in via --venues.
DEFAULT_VENUES = ["okx", "gate", "bitget", "hyperliquid", "kucoinfutures"]
SUPPORTED_VENUES = DEFAULT_VENUES + ["binance", "bybit"]

DEFAULT_INTERVAL_H = {"hyperliquid": 1.0}       # everyone else defaults to 8h
FALLBACK_TAKER = {                               # base-tier taker fees if the market metadata lacks one
    "okx": 0.0005, "gate": 0.0005, "bitget": 0.0006, "hyperliquid": 0.00045,
    "kucoinfutures": 0.0006, "binance": 0.0005, "bybit": 0.00055,
}
SETTLE_CCYS = {"USDT", "USDC"}
BOOK_LIMIT = {"kucoinfutures": 100}            # venues that only accept fixed order-book depths

# Multiplied contracts: 1000PEPE (CEX style) and kPEPE (Hyperliquid style) both mean 1000 PEPE.
_PREFIX = re.compile(r"^(1000000|100000|10000|1000|k)(?=[A-Z0-9])")
_PREFIX_MULT = {"1000000": 1e6, "100000": 1e5, "10000": 1e4, "1000": 1e3, "k": 1e3}


def normalize_base(base: str) -> tuple[str, float]:
    m = _PREFIX.match(base)
    if not m:
        return base.upper(), 1.0
    return base[m.end():].upper(), _PREFIX_MULT[m.group(1)]


def parse_interval(value) -> Optional[float]:
    """Accept ccxt's '8h' style strings or bare hour counts ('8', 8)."""
    if value is None or value == "":
        return None
    s = str(value).strip().lower()
    if s.endswith("h"):
        s = s[:-1]
    try:
        hours = float(s)
    except ValueError:
        return None
    return hours if hours > 0 else None


def make_exchange(venue: str) -> ccxt.Exchange:
    if venue not in SUPPORTED_VENUES:
        raise ValueError(f"unsupported venue {venue!r}; choose from {SUPPORTED_VENUES}")
    return getattr(ccxt, venue)({"enableRateLimit": True, "timeout": 20000,
                                 "options": {"defaultType": "swap"}})


def _market_interval(venue: str, market: dict) -> Optional[float]:
    info = market.get("info") or {}
    if venue == "bitget":
        return parse_interval(info.get("fundInterval"))
    if venue == "kucoinfutures" and info.get("fundingRateGranularity"):
        return float(info["fundingRateGranularity"]) / 3_600_000
    return None


def _next_funding(fr: dict, now_ms: int) -> Optional[int]:
    # Venues disagree on which field is "next"; take the earliest future timestamp.
    candidates = [fr.get("fundingTimestamp"), fr.get("nextFundingTimestamp")]
    future = [int(t) for t in candidates if t and int(t) > now_ms]
    return min(future) if future else None


def fetch_quotes(ex: ccxt.Exchange) -> list[FundingQuote]:
    venue = ex.id
    markets = ex.load_markets()
    rates = ex.fetch_funding_rates()
    now_ms = int(time.time() * 1000)
    quotes: list[FundingQuote] = []
    for symbol, fr in rates.items():
        market = markets.get(symbol)
        if not market or not market.get("swap") or not market.get("linear") or not market.get("active", True):
            continue
        if market.get("settle") not in SETTLE_CCYS or fr.get("fundingRate") is None:
            continue
        interval = (parse_interval(fr.get("interval"))
                    or _market_interval(venue, market)
                    or DEFAULT_INTERVAL_H.get(venue, 8.0))
        base, mult = normalize_base(market["base"])
        next_ms = _next_funding(fr, now_ms)
        estimated = next_ms is None
        if estimated:   # funding settles on interval boundaries aligned to 00:00 UTC
            step = int(interval * 3_600_000)
            next_ms = (now_ms // step + 1) * step
        quotes.append(FundingQuote(
            venue=venue, symbol=symbol, base=base, multiplier=mult, settle=market["settle"],
            rate=float(fr["fundingRate"]), interval_h=interval,
            taker_fee=float(market.get("taker") or FALLBACK_TAKER.get(venue, 0.0006)),
            contract_size=float(market.get("contractSize") or 1.0),
            next_funding_ms=next_ms, next_funding_estimated=estimated,
        ))
    return quotes
