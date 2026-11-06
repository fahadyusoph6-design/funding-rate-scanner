"""Pair building, cost enrichment and ranking."""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

import ccxt

from . import history, venues
from .costs import entry_slippage, mid_price
from .models import HOURS_PER_YEAR, FundingQuote, Opportunity

log = logging.getLogger(__name__)

MAX_BASIS = 0.05    # >5% cross-venue price gap almost always means two different tokens share a ticker


@dataclass
class ScanConfig:
    venues: list[str]
    notional: float = 10_000.0          # USD per leg
    min_apr: float = 0.10               # minimum gross funding spread to consider
    horizon_h: int = 72                 # holding horizon used for ranking
    book_candidates: int = 40           # how many pairs get order-book checks
    history_top: int = 15               # how many pairs get funding-history analysis
    lookback_days: int = 7
    book_depth: int = 50


def build_pairs(quotes: Iterable[FundingQuote], min_apr: float) -> list[Opportunity]:
    """Every cross-venue pair of the same asset, oriented so we short the higher funding rate."""
    by_base: dict[str, list[FundingQuote]] = defaultdict(list)
    for q in quotes:
        by_base[q.base].append(q)
    out: list[Opportunity] = []
    for base, qs in by_base.items():
        for i in range(len(qs)):
            for j in range(i + 1, len(qs)):
                a, b = qs[i], qs[j]
                if a.venue == b.venue:
                    continue
                long_q, short_q = (a, b) if b.hourly >= a.hourly else (b, a)
                opp = Opportunity(base=base, long=long_q, short=short_q)
                if opp.net_apr >= min_apr:
                    out.append(opp)
    return out


def _per_venue(tasks: dict[str, list], fn: Callable) -> None:
    """Run fn(task) for every task, serial within a venue (rate limits), parallel across venues."""
    def worker(items):
        for t in items:
            try:
                fn(t)
            except (ccxt.BaseError, ValueError, KeyError) as e:
                log.debug("task %s failed: %s", t, e)
    with ThreadPoolExecutor(max_workers=max(len(tasks), 1)) as pool:
        list(pool.map(worker, tasks.values()))


def enrich_books(opps: list[Opportunity], exchanges: dict[str, ccxt.Exchange], cfg: ScanConfig) -> None:
    books: dict[tuple[str, str], dict] = {}
    tasks: dict[str, list] = defaultdict(list)
    for o in opps:
        for q in (o.long, o.short):
            key = (q.venue, q.symbol)
            if key not in tasks[q.venue]:
                tasks[q.venue].append(key)

    def load(key):
        venue, symbol = key
        books[key] = exchanges[venue].fetch_order_book(symbol, limit=venues.BOOK_LIMIT.get(venue, cfg.book_depth))

    _per_venue(tasks, load)

    for o in opps:
        lb, sb = books.get((o.long.venue, o.long.symbol)), books.get((o.short.venue, o.short.symbol))
        if not lb or not sb:
            o.flags.append("no-book")
            continue
        lm, sm = mid_price(lb), mid_price(sb)
        if not lm or not sm:
            o.flags.append("empty-book")
            continue
        # compare prices per canonical unit (1000PEPE vs PEPE)
        o.long_mid, o.short_mid = lm / o.long.multiplier, sm / o.short.multiplier
        o.basis = (o.short_mid - o.long_mid) / o.long_mid
        o.long_slippage = entry_slippage(lb, "buy", cfg.notional, o.long.contract_size)
        o.short_slippage = entry_slippage(sb, "sell", cfg.notional, o.short.contract_size)
        if o.long_slippage is None or o.short_slippage is None:
            o.flags.append("thin-book")
        if abs(o.basis) > MAX_BASIS:
            o.flags.append("ticker-mismatch?")


def enrich_history(opps: list[Opportunity], exchanges: dict[str, ccxt.Exchange], cfg: ScanConfig) -> None:
    since = int(time.time() * 1000) - cfg.lookback_days * 86_400_000
    series: dict[tuple[str, str], dict[int, float]] = {}
    tasks: dict[str, list] = defaultdict(list)
    for o in opps:
        for q in (o.long, o.short):
            key = (q.venue, q.symbol, q.interval_h)
            if key not in tasks[q.venue]:
                tasks[q.venue].append(key)

    def load(key):
        venue, symbol, interval_h = key
        ex = exchanges[venue]
        try:
            recs = ex.fetch_funding_rate_history(symbol, since=since)
        except ccxt.BaseError:
            recs = ex.fetch_funding_rate_history(symbol, limit=100)   # some venues reject `since`
        series[(venue, symbol)] = history.hourly_series(
            [r for r in recs if r.get("timestamp", 0) >= since], interval_h)

    _per_venue(tasks, load)

    for o in opps:
        ls, ss = series.get((o.long.venue, o.long.symbol)), series.get((o.short.venue, o.short.symbol))
        stats = history.spread_stats(ls, ss) if ls and ss else None
        if stats is None:
            o.flags.append("no-history")
            continue
        o.stats = stats
        o.predicted_carry = history.forecast_carry(o.net_hourly, stats, cfg.horizon_h)
        if stats.pct_positive < 0.5:
            o.flags.append("spread-usually-negative")


def _rank_key(o: Opportunity, horizon_h: int) -> float:
    pred = o.predicted_net()
    if pred is not None:
        return pred
    snap = o.snapshot_net(horizon_h)
    # un-forecast pairs rank below forecast ones with the same snapshot value
    return (snap if snap is not None else -1.0) - 1e-3


def scan(cfg: ScanConfig, on_status: Callable[[str], None] = lambda s: None) -> tuple[list[Opportunity], dict]:
    exchanges = {v: venues.make_exchange(v) for v in cfg.venues}
    quotes: list[FundingQuote] = []
    venue_status: dict[str, str] = {}

    def pull(venue):
        t0 = time.time()
        try:
            qs = venues.fetch_quotes(exchanges[venue])
            venue_status[venue] = f"ok: {len(qs)} perps in {time.time() - t0:.1f}s"
            return qs
        except ccxt.BaseError as e:
            venue_status[venue] = f"unavailable: {type(e).__name__}"
            return []

    on_status(f"Fetching funding rates from {len(cfg.venues)} venues...")
    with ThreadPoolExecutor(max_workers=len(cfg.venues)) as pool:
        for qs in pool.map(pull, cfg.venues):
            quotes.extend(qs)
    live = [v for v, s in venue_status.items() if s.startswith("ok")]
    for v in cfg.venues:
        on_status(f"  {v:14} {venue_status[v]}")

    pairs = build_pairs(quotes, cfg.min_apr)
    # pre-rank on funding net of fees only, so the expensive book checks go to plausible candidates
    pairs.sort(key=lambda o: o.net_hourly * cfg.horizon_h - o.fee_cost, reverse=True)
    shortlist = pairs[: cfg.book_candidates]
    on_status(f"{len(quotes)} perps, {len({q.base for q in quotes})} assets, "
              f"{len(pairs)} cross-venue pairs above {cfg.min_apr:.0%} APR; checking order books for top {len(shortlist)}...")
    enrich_books(shortlist, exchanges, cfg)

    tradable = [o for o in shortlist if o.round_trip_cost is not None and "ticker-mismatch?" not in o.flags]
    tradable.sort(key=lambda o: o.snapshot_net(cfg.horizon_h), reverse=True)
    deep = tradable[: cfg.history_top]
    dropped: dict[str, int] = defaultdict(int)
    for o in shortlist:
        if o not in tradable:
            dropped[o.flags[0] if o.flags else 'unknown'] += 1
    if dropped:
        on_status(f"Dropped {sum(dropped.values())} of {len(shortlist)}: " +
                  ", ".join(f"{n} {why}" for why, n in sorted(dropped.items(), key=lambda x: -x[1])) +
                  f" (thin-book = visible depth can't fill ${cfg.notional:,.0f})")
    on_status(f"Analysing {cfg.lookback_days}-day funding history for top {len(deep)}...")
    enrich_history(deep, exchanges, cfg)

    ranked = sorted(tradable, key=lambda o: _rank_key(o, cfg.horizon_h), reverse=True)
    rejected = [o for o in shortlist if o not in tradable]
    meta = {"venues": venue_status, "live_venues": live, "quotes": len(quotes),
            "assets": len({q.base for q in quotes}), "pairs": len(pairs), "rejected": rejected}
    return ranked, meta


__all__ = ["ScanConfig", "build_pairs", "scan", "HOURS_PER_YEAR"]
