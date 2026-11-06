"""Offline tests for the pure maths - no network access needed."""
import math

import pytest

from funding_scanner.costs import entry_slippage, vwap_fill
from funding_scanner.history import forecast_carry, hourly_series, spread_stats
from funding_scanner.models import FundingQuote, Opportunity, SpreadStats
from funding_scanner.scanner import build_pairs
from funding_scanner.venues import normalize_base, parse_interval


def q(venue, rate, interval_h=8.0, base="BTC", fee=0.0005):
    return FundingQuote(venue=venue, symbol=f"{base}/USDT:USDT", base=base, multiplier=1.0, settle="USDT",
                        rate=rate, interval_h=interval_h, taker_fee=fee)


# --- normalisation ---------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("BTC", ("BTC", 1.0)), ("1000PEPE", ("PEPE", 1000.0)), ("kPEPE", ("PEPE", 1000.0)),
    ("1000000MOG", ("MOG", 1e6)), ("1INCH", ("1INCH", 1.0)), ("KAS", ("KAS", 1.0)),
])
def test_normalize_base(raw, expected):
    assert normalize_base(raw) == expected


@pytest.mark.parametrize("raw,expected", [("8h", 8.0), ("1h", 1.0), ("4", 4.0), (None, None), ("", None), ("x", None)])
def test_parse_interval(raw, expected):
    assert parse_interval(raw) == expected


def test_hourly_normalisation_makes_venues_comparable():
    eight = q("okx", 0.0008, 8)
    one = q("hyperliquid", 0.0001, 1)
    assert eight.hourly == pytest.approx(one.hourly)
    assert eight.apr == pytest.approx(0.0001 * 24 * 365)


# --- pairing ---------------------------------------------------------------------

def test_build_pairs_shorts_the_higher_rate():
    pairs = build_pairs([q("okx", 0.0001), q("gate", 0.0010)], min_apr=0.0)
    assert len(pairs) == 1
    o = pairs[0]
    assert (o.long.venue, o.short.venue) == ("okx", "gate")
    assert o.net_hourly == pytest.approx((0.0010 - 0.0001) / 8)


def test_build_pairs_handles_negative_funding():
    # long the venue where longs get paid (negative), short where shorts get paid (positive)
    o = build_pairs([q("okx", -0.0005), q("gate", 0.0002)], min_apr=0.0)[0]
    assert (o.long.venue, o.short.venue) == ("okx", "gate")
    assert o.net_hourly == pytest.approx(0.0007 / 8)


def test_build_pairs_threshold_and_same_venue():
    qs = [q("okx", 0.0001), q("gate", 0.00011), q("okx", 0.002, base="ETH")]
    assert build_pairs(qs, min_apr=0.10) == []


# --- costs -----------------------------------------------------------------------

def test_vwap_fill_walks_levels():
    asks = [[100.0, 1.0], [101.0, 1.0]]
    # 150 USD: 100 at 100 (1 unit) + 50 at 101 (0.495 units)
    px = vwap_fill(asks, 150.0)
    assert px == pytest.approx(150.0 / (1 + 50 / 101))


def test_vwap_fill_respects_contract_size_and_depth():
    asks = [[100.0, 10.0]]                                # 10 contracts x 0.01 = 0.1 units = $10
    assert vwap_fill(asks, 10.0, contract_size=0.01) == pytest.approx(100.0)
    assert vwap_fill(asks, 11.0, contract_size=0.01) is None


def test_entry_slippage_both_sides():
    book = {"bids": [[99.0, 5.0], [98.0, 5.0]], "asks": [[101.0, 5.0], [102.0, 5.0]]}
    assert entry_slippage(book, "buy", 100.0) == pytest.approx(0.01)
    assert entry_slippage(book, "sell", 99.0) == pytest.approx(0.01)


def test_round_trip_cost_and_breakeven():
    o = Opportunity(base="BTC", long=q("okx", 0.0, fee=0.0005), short=q("gate", 0.0008, fee=0.0005))
    o.long_slippage, o.short_slippage = 0.0002, 0.0003
    assert o.round_trip_cost == pytest.approx(2 * 0.001 + 2 * 0.0005)    # 30 bps
    assert o.breakeven_hours == pytest.approx(0.003 / (0.0008 / 8))       # 30 hours
    assert o.snapshot_net(72) == pytest.approx(0.0001 * 72 - 0.003)


# --- history / forecast ----------------------------------------------------------

def test_hourly_series_spreads_payment_and_infers_interval():
    H = 3_600_000
    recs = [{"timestamp": 8 * H, "fundingRate": 0.0008}, {"timestamp": 12 * H, "fundingRate": 0.0004}]
    s = hourly_series(recs, default_interval_h=8)
    assert len(s) == 12
    assert s[0] == pytest.approx(0.0001) and s[7 * H] == pytest.approx(0.0001)
    assert s[8 * H] == pytest.approx(0.0001) and s[11 * H] == pytest.approx(0.0001)   # 4h interval inferred


def test_spread_stats_recovers_ar1_persistence():
    H = 3_600_000
    phi, mu = 0.9, 0.00005
    d, short = 0.0, {}
    import random
    rnd = random.Random(1)
    for t in range(2000):
        d = phi * d + rnd.gauss(0, 0.00001)
        short[t * H] = mu + d
    flat = {t * H: 0.0 for t in range(2000)}
    st = spread_stats(flat, short)
    assert st.phi == pytest.approx(phi, abs=0.03)
    assert st.mean_hourly == pytest.approx(mu, abs=0.00001)
    assert st.half_life_h == pytest.approx(math.log(0.5) / math.log(st.phi))


def test_forecast_decays_toward_mean():
    st = SpreadStats(hours=168, mean_hourly=0.00001, std_hourly=0.0, pct_positive=1.0, phi=0.9, half_life_h=6.6)
    # current spread far above the mean: forecast must sit between "mean forever" and "current forever"
    f = forecast_carry(0.0001, st, 72)
    assert 0.00001 * 72 < f < 0.0001 * 72
    # no persistence -> pure mean
    st0 = SpreadStats(168, 0.00001, 0.0, 1.0, 0.0, None)
    assert forecast_carry(0.0001, st0, 72) == pytest.approx(0.00001 * 72)
