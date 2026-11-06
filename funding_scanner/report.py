"""Console table and CSV/JSON export."""
from __future__ import annotations

import csv
import json
import os
import time
from datetime import datetime, timezone
from typing import Optional

from .models import Opportunity

VENUE_SHORT = {"okx": "OKX", "gate": "Gate", "bitget": "Bitget", "hyperliquid": "HypL",
               "kucoinfutures": "KuCoin", "binance": "Binance", "bybit": "Bybit"}


def _bps(x: Optional[float]) -> str:
    return "-" if x is None else f"{x * 1e4:,.0f}"


def _pct(x: Optional[float]) -> str:
    return "-" if x is None else f"{x * 100:,.0f}%"


def _hours(x: Optional[float]) -> str:
    return "-" if x is None else (f"{x:,.0f}" if x < 10_000 else ">10k")


def _next_in(ms: Optional[int], estimated: bool = False) -> str:
    if not ms:
        return "?"
    mins = max(0, (ms - time.time() * 1000) / 60_000)
    txt = f"{mins / 60:.1f}h" if mins >= 60 else f"{mins:.0f}m"
    return "~" + txt if estimated else txt


def row(o: Opportunity, horizon_h: int) -> dict:
    s = o.stats
    return {
        "asset": o.base,
        "long_venue": o.long.venue, "long_symbol": o.long.symbol,
        "long_rate": o.long.rate, "long_interval_h": o.long.interval_h, "long_apr": o.long.apr,
        "short_venue": o.short.venue, "short_symbol": o.short.symbol,
        "short_rate": o.short.rate, "short_interval_h": o.short.interval_h, "short_apr": o.short.apr,
        "net_funding_apr": o.net_apr,
        "fees_bps": o.fee_cost * 1e4,
        "slippage_bps": None if o.round_trip_cost is None else (o.round_trip_cost - o.fee_cost) * 1e4,
        "round_trip_cost_bps": None if o.round_trip_cost is None else o.round_trip_cost * 1e4,
        "basis_bps": None if o.basis is None else o.basis * 1e4,
        "breakeven_h": o.breakeven_hours,
        f"snapshot_net_{horizon_h}h_bps": None if o.snapshot_net(horizon_h) is None else o.snapshot_net(horizon_h) * 1e4,
        f"predicted_net_{horizon_h}h_bps": None if o.predicted_net() is None else o.predicted_net() * 1e4,
        "hist_mean_spread_apr": s.mean_apr if s else None,
        "hist_pct_positive": s.pct_positive if s else None,
        "hist_half_life_h": s.half_life_h if s else None,
        "hist_hours": s.hours if s else None,
        "long_next_funding_utc": _iso(o.long.next_funding_ms),
        "short_next_funding_utc": _iso(o.short.next_funding_ms),
        "flags": ";".join(o.flags),
    }


def _iso(ms: Optional[int]) -> Optional[str]:
    return None if not ms else datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="minutes")


def print_table(opps: list[Opportunity], horizon_h: int, top: int, notional: float) -> None:
    head = ["#", "Asset", "Long (APR, next)", "Short (APR, next)", "Net APR", "Cost bp", "Basis bp",
            "B/E h", f"Snap {horizon_h}h bp", f"Pred {horizon_h}h bp", "7d mean", "%pos", "Half-life h", "Flags"]
    rows = []
    for i, o in enumerate(opps[:top], 1):
        s = o.stats
        rows.append([
            str(i), o.base,
            f"{VENUE_SHORT.get(o.long.venue, o.long.venue)} {_pct(o.long.apr)} {o.long.interval_h:g}h/{_next_in(o.long.next_funding_ms, o.long.next_funding_estimated)}",
            f"{VENUE_SHORT.get(o.short.venue, o.short.venue)} {_pct(o.short.apr)} {o.short.interval_h:g}h/{_next_in(o.short.next_funding_ms, o.short.next_funding_estimated)}",
            _pct(o.net_apr), _bps(o.round_trip_cost), _bps(o.basis), _hours(o.breakeven_hours),
            _bps(o.snapshot_net(horizon_h)), _bps(o.predicted_net()),
            _pct(s.mean_apr) if s else "-", _pct(s.pct_positive) if s else "-",
            _hours(s.half_life_h) if s else "-", ",".join(o.flags),
        ])
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(head)]
    right = {0, 4, 5, 6, 7, 8, 9, 10, 11, 12}
    fmt = lambda cells: "  ".join(c.rjust(w) if i in right else c.ljust(w) for i, (c, w) in enumerate(zip(cells, widths)))
    print()
    print(f"Top {len(rows)} funding-carry opportunities  (notional ${notional:,.0f} per leg, horizon {horizon_h}h)")
    print(fmt(head))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print(fmt(r))
    print()
    print("Cost = taker fees + 2x entry slippage, both legs, open and close.  Basis = short-venue premium over long venue.")
    print("Snap = net return if today's funding spread held for the horizon.  Pred = AR(1) decay forecast from history.")
    print("Next = time to next funding payment (~ = estimated from the interval; venue did not report it).")


def export(opps: list[Opportunity], meta: dict, horizon_h: int, out_dir: str) -> tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ")
    rows = [row(o, horizon_h) for o in opps]
    csv_path = os.path.join(out_dir, f"opportunities_{stamp}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    json_path = os.path.join(out_dir, f"opportunities_{stamp}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"generated_utc": stamp, "venues": meta["venues"], "perps": meta["quotes"],
                   "assets": meta["assets"], "pairs_above_threshold": meta["pairs"],
                   "opportunities": rows}, f, indent=2)
    return csv_path, json_path
