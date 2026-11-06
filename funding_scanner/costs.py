"""Execution-cost helpers: walking order books for size-aware slippage."""
from __future__ import annotations

from typing import Optional, Sequence

Level = Sequence[float]   # [price, amount_in_contracts, ...] as returned by ccxt


def vwap_fill(levels: Sequence[Level], notional: float, contract_size: float = 1.0) -> Optional[float]:
    """Average fill price for `notional` (quote currency) taken from one side of the book.

    Returns None when the visible book is too thin to fill the full size.
    """
    if notional <= 0:
        raise ValueError("notional must be positive")
    remaining = notional
    cost = 0.0      # quote currency spent
    qty = 0.0       # base units acquired
    for level in levels:
        price, amount = float(level[0]), float(level[1])
        level_notional = price * amount * contract_size
        take = min(remaining, level_notional)
        cost += take
        qty += take / price
        remaining -= take
        if remaining <= 1e-9:
            return cost / qty
    return None


def mid_price(book: dict) -> Optional[float]:
    if not book.get("bids") or not book.get("asks"):
        return None
    return (float(book["bids"][0][0]) + float(book["asks"][0][0])) / 2


def entry_slippage(book: dict, side: str, notional: float, contract_size: float = 1.0) -> Optional[float]:
    """Slippage versus mid, as a positive fraction, for a market order of `notional`.

    side="buy" walks the asks (opening a long); side="sell" walks the bids (opening a short).
    """
    mid = mid_price(book)
    if mid is None:
        return None
    levels = book["asks"] if side == "buy" else book["bids"]
    px = vwap_fill(levels, notional, contract_size)
    if px is None:
        return None
    return (px - mid) / mid if side == "buy" else (mid - px) / mid
