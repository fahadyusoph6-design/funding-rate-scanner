"""Funding-history statistics and a simple persistence forecast.

The forecast is a deliberately simple baseline: model the hourly funding
spread as AR(1) mean-reverting to its lookback mean. It answers the two
questions that matter before entering - "how persistent has this spread
been?" and "how much carry is left if it decays the way it usually does?" -
and gives any fancier model something honest to beat.
"""
from __future__ import annotations

import math
from typing import Iterable, Optional

from .models import SpreadStats

HOUR_MS = 3_600_000


def hourly_series(records: Iterable[dict], default_interval_h: float) -> dict[int, float]:
    """Spread each settled funding payment evenly over the hours it covered.

    `records` are ccxt funding-history dicts with `timestamp` (settlement time, ms)
    and `fundingRate`. The interval for each record is inferred from the gap to
    the previous settlement, so venues that change interval mid-history still line up.
    """
    rows = sorted(
        ((int(r["timestamp"]), float(r["fundingRate"])) for r in records
         if r.get("timestamp") is not None and r.get("fundingRate") is not None),
        key=lambda x: x[0],
    )
    out: dict[int, float] = {}
    prev_ts: Optional[int] = None
    for ts, rate in rows:
        if prev_ts is None:
            interval_h = default_interval_h
        else:
            interval_h = min(max(round((ts - prev_ts) / HOUR_MS), 1), 8)
        prev_ts = ts
        end_hour = (ts // HOUR_MS) * HOUR_MS
        for k in range(1, int(interval_h) + 1):
            out[end_hour - k * HOUR_MS] = rate / interval_h
    return out


def spread_stats(long_series: dict[int, float], short_series: dict[int, float],
                 min_hours: int = 24) -> Optional[SpreadStats]:
    hours = sorted(set(long_series) & set(short_series))
    if len(hours) < min_hours:
        return None
    s = [short_series[h] - long_series[h] for h in hours]
    n = len(s)
    mean = sum(s) / n
    std = math.sqrt(sum((x - mean) ** 2 for x in s) / max(n - 1, 1))
    pct_pos = sum(1 for x in s if x > 0) / n

    # AR(1) on the demeaned series: d_t = phi * d_{t-1} + e_t
    d = [x - mean for x in s]
    num = sum(d[i] * d[i - 1] for i in range(1, n))
    den = sum(d[i - 1] ** 2 for i in range(1, n))
    phi = num / den if den > 0 else 0.0
    phi = min(max(phi, 0.0), 0.995)
    half_life = math.log(0.5) / math.log(phi) if 0 < phi < 1 else None
    return SpreadStats(hours=n, mean_hourly=mean, std_hourly=std, pct_positive=pct_pos,
                       phi=phi, half_life_h=half_life)


def forecast_carry(current_hourly: float, stats: SpreadStats, horizon_h: int) -> float:
    """Expected cumulative funding over the next `horizon_h` hours under AR(1) decay.

    E[s_{t+k}] = mu + (s_t - mu) * phi^k, summed for k = 1..H.
    """
    mu, phi = stats.mean_hourly, stats.phi
    gap = current_hourly - mu
    if phi == 0:
        decay_sum = 0.0
    else:
        decay_sum = phi * (1 - phi ** horizon_h) / (1 - phi)
    return mu * horizon_h + gap * decay_sum
