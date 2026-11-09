"""Binance USDT quote-volume universe selection.

This is intentionally a startup snapshot, not a per-tick ranking: adding or
removing websocket streams is expensive and must not race the feed.
"""
from __future__ import annotations

from typing import Any

import httpx

STABLE_BASES = {
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "USDD", "PYUSD",
    "EUR", "GBP", "AEUR", "USD1", "RLUSD", "USDS", "USDE", "GHO",
    "EURC", "AUSD", "USDG", "USDL", "USDX",
}
LEVERAGED = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")


def _eligible(symbol: str) -> bool:
    if not symbol.endswith("USDT") or len(symbol) <= 4:
        return False
    if symbol.endswith(LEVERAGED):
        return False
    base = symbol[:-4]
    return base not in STABLE_BASES and not base.endswith("3L") and not base.endswith("3S")


async def fetch_top_usdt_symbols(rest_base: str, limit: int = 10,
                                 timeout: float = 20.0) -> list[dict[str, Any]]:
    """Return top USDT spot symbols by 24h quote volume, with audit fields."""
    url = rest_base.rstrip("/") + "/api/v3/ticker/24hr"
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0)) as client:
        response = await client.get(url)
        response.raise_for_status()
        rows = response.json()
    ranked: list[dict[str, Any]] = []
    for row in rows:
        symbol = str(row.get("symbol", "")).upper()
        if not _eligible(symbol):
            continue
        try:
            quote_volume = float(row.get("quoteVolume", 0) or 0)
            last_price = float(row.get("lastPrice", 0) or 0)
        except (TypeError, ValueError):
            continue
        if quote_volume <= 0 or last_price <= 0:
            continue
        ranked.append({
            "symbol": symbol,
            "quote_volume": quote_volume,
            "last_price": last_price,
        })
    ranked.sort(key=lambda x: x["quote_volume"], reverse=True)
    return ranked[:max(1, int(limit))]
