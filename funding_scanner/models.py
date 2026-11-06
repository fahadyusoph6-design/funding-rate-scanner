"""Core data types shared across the scanner."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

HOURS_PER_YEAR = 24 * 365


@dataclass(frozen=True)
class FundingQuote:
    """One perpetual contract's current funding state on one venue.

    `rate` is the venue's quoted funding rate for one funding interval
    (positive = longs pay shorts). Everything downstream works on the
    normalised `hourly` rate so 1h, 4h and 8h venues are comparable.
    """
    venue: str
    symbol: str             # ccxt unified symbol, e.g. "BTC/USDT:USDT"
    base: str               # canonical base asset after prefix normalisation, e.g. "PEPE"
    multiplier: float       # units of canonical base per 1 unit of the listed base (1000PEPE -> 1000)
    settle: str             # settlement currency (USDT / USDC)
    rate: float             # funding rate per interval
    interval_h: float       # funding interval in hours
    taker_fee: float        # taker fee as a fraction of notional
    contract_size: float = 1.0
    next_funding_ms: Optional[int] = None
    next_funding_estimated: bool = False   # venue did not report it; derived from the interval

    @property
    def hourly(self) -> float:
        return self.rate / self.interval_h

    @property
    def apr(self) -> float:
        return self.hourly * HOURS_PER_YEAR


@dataclass
class SpreadStats:
    """Historical behaviour of the (short - long) hourly funding spread."""
    hours: int
    mean_hourly: float
    std_hourly: float
    pct_positive: float
    phi: float                       # AR(1) persistence coefficient, clamped to [0, 0.995]
    half_life_h: Optional[float]     # hours for a deviation from the mean to halve

    @property
    def mean_apr(self) -> float:
        return self.mean_hourly * HOURS_PER_YEAR


@dataclass
class Opportunity:
    """A long-on-one-venue / short-on-another funding carry trade."""
    base: str
    long: FundingQuote
    short: FundingQuote

    # filled by order-book enrichment
    long_mid: Optional[float] = None
    short_mid: Optional[float] = None
    long_slippage: Optional[float] = None    # fraction of notional, entry
    short_slippage: Optional[float] = None
    basis: Optional[float] = None            # (short mid - long mid) / long mid, per canonical unit

    # filled by history enrichment
    stats: Optional[SpreadStats] = None
    predicted_carry: Optional[float] = None  # forecast cumulative carry over the horizon (fraction)

    flags: list[str] = field(default_factory=list)

    @property
    def net_hourly(self) -> float:
        """Funding earned per hour per unit notional: receive on the short leg, pay on the long leg."""
        return self.short.hourly - self.long.hourly

    @property
    def net_apr(self) -> float:
        return self.net_hourly * HOURS_PER_YEAR

    @property
    def fee_cost(self) -> float:
        """Taker fees to open and close both legs."""
        return 2 * (self.long.taker_fee + self.short.taker_fee)

    @property
    def round_trip_cost(self) -> Optional[float]:
        """Fees plus entry slippage, assuming exit slippage is similar to entry."""
        if self.long_slippage is None or self.short_slippage is None:
            return None
        return self.fee_cost + 2 * (self.long_slippage + self.short_slippage)

    @property
    def breakeven_hours(self) -> Optional[float]:
        cost = self.round_trip_cost
        if cost is None or self.net_hourly <= 0:
            return None
        return cost / self.net_hourly

    def snapshot_net(self, horizon_h: float) -> Optional[float]:
        """Net return over the horizon if the current funding spread simply persisted."""
        cost = self.round_trip_cost
        return None if cost is None else self.net_hourly * horizon_h - cost

    def predicted_net(self) -> Optional[float]:
        cost = self.round_trip_cost
        if cost is None or self.predicted_carry is None:
            return None
        return self.predicted_carry - cost
