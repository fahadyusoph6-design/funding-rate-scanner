"""CLI entry point: python -m funding_scanner"""
from __future__ import annotations

import argparse
import logging
import sys

from . import report, venues
from .scanner import ScanConfig, scan


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="funding_scanner",
                                description="Cross-venue perpetual funding-rate arbitrage scanner (read-only, public data).")
    p.add_argument("--venues", default=",".join(venues.DEFAULT_VENUES),
                   help=f"comma-separated, from: {','.join(venues.SUPPORTED_VENUES)}")
    p.add_argument("--notional", type=float, default=10_000, help="USD per leg used for slippage (default 10000)")
    p.add_argument("--min-apr", type=float, default=10, help="minimum gross funding spread, %% APR (default 10)")
    p.add_argument("--horizon", type=int, default=72, help="holding horizon in hours for ranking (default 72)")
    p.add_argument("--candidates", type=int, default=40, help="pairs that get order-book checks (default 40)")
    p.add_argument("--history-top", type=int, default=15, help="pairs that get history analysis (default 15)")
    p.add_argument("--lookback-days", type=int, default=7)
    p.add_argument("--top", type=int, default=20, help="rows to print (default 20)")
    p.add_argument("--out", default="out", help="directory for CSV/JSON output")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.WARNING, format="%(levelname)s %(message)s")

    chosen = [v.strip() for v in a.venues.split(",") if v.strip()]
    bad = [v for v in chosen if v not in venues.SUPPORTED_VENUES]
    if bad:
        p.error(f"unsupported venue(s): {bad}")

    cfg = ScanConfig(venues=chosen, notional=a.notional, min_apr=a.min_apr / 100, horizon_h=a.horizon,
                     book_candidates=a.candidates, history_top=a.history_top, lookback_days=a.lookback_days)
    opps, meta = scan(cfg, on_status=lambda s: print(s, flush=True))
    if len(meta["live_venues"]) < 2:
        print("Fewer than two venues reachable - nothing to compare.", file=sys.stderr)
        return 1
    report.print_table(opps, cfg.horizon_h, a.top, cfg.notional)
    csv_path, json_path = report.export(opps, meta, cfg.horizon_h, a.out)
    print(f"\nSaved {len(opps)} opportunities -> {csv_path}\n{'':>6}{'':>13}-> {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
