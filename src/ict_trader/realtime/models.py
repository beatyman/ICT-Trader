"""Wire / storage data contracts.

All timestamps are UTC milliseconds.  A closed bar is immutable; the only
mutable object in the hot path is the unclosed `kline:latest:*` hash.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

INTERVAL_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}

CH_COLUMNS = [
    "symbol", "interval", "open_time", "close_time", "open", "high", "low",
    "close", "volume", "quote_volume", "trades", "taker_buy_base",
    "taker_buy_quote", "ingest_time",
]


def interval_ms(interval: str) -> int:
    try:
        return INTERVAL_MS[interval]
    except KeyError as exc:
        raise ValueError(f"unsupported interval: {interval}") from exc


def utc_ms() -> int:
    return int(time.time() * 1000)


def ms_to_datetime(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)


def dt_to_ms(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


@dataclass(slots=True)
class Kline:
    symbol: str
    interval: str
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float = 0.0
    trades: int = 0
    taker_buy_base: float = 0.0
    taker_buy_quote: float = 0.0
    is_closed: bool = False
    ingest_time: int = 0
    source: str = "ws"

    def __post_init__(self) -> None:
        self.symbol = self.symbol.upper()
        if not self.ingest_time:
            self.ingest_time = utc_ms()

    def key(self) -> tuple[str, str, int]:
        return (self.symbol, self.interval, self.open_time)

    def to_redis(self) -> dict[str, str]:
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "open_time": str(self.open_time),
            "close_time": str(self.close_time),
            "open": repr(self.open),
            "high": repr(self.high),
            "low": repr(self.low),
            "close": repr(self.close),
            "volume": repr(self.volume),
            "quote_volume": repr(self.quote_volume),
            "trades": str(self.trades),
            "taker_buy_base": repr(self.taker_buy_base),
            "taker_buy_quote": repr(self.taker_buy_quote),
            "is_closed": "1" if self.is_closed else "0",
            "ingest_time": str(self.ingest_time),
        }

    def to_ch_row(self) -> tuple:
        return (
            self.symbol,
            self.interval,
            ms_to_datetime(self.open_time),
            ms_to_datetime(self.close_time),
            float(self.open),
            float(self.high),
            float(self.low),
            float(self.close),
            float(self.volume),
            float(self.quote_volume),
            int(self.trades),
            float(self.taker_buy_base),
            float(self.taker_buy_quote),
            ms_to_datetime(self.ingest_time),
        )

    def to_public(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "open_time": self.open_time,
            "close_time": self.close_time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "quote_volume": self.quote_volume,
            "trades": self.trades,
            "is_closed": self.is_closed,
        }

    def to_csv_row(self) -> dict[str, Any]:
        """The ICT engine expects a `date` column at bar-open UTC time."""
        return {
            "date": datetime.fromtimestamp(self.open_time / 1000.0, tz=timezone.utc),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_binance(cls, payload: dict[str, Any], *, symbol: str | None = None,
                     interval: str | None = None, source: str = "ws") -> "Kline":
        """Parse the `k` object from a Binance combined-stream kline event."""
        k = payload.get("k", payload)
        return cls(
            symbol=(symbol or k.get("s") or payload.get("s") or "").upper(),
            interval=(interval or k.get("i") or "").lower(),
            open_time=int(k["t"]),
            close_time=int(k["T"]),
            open=float(k["o"]),
            high=float(k["h"]),
            low=float(k["l"]),
            close=float(k["c"]),
            volume=float(k.get("v", 0.0)),
            quote_volume=float(k.get("q", 0.0)),
            trades=int(k.get("n", 0) or 0),
            taker_buy_base=float(k.get("V", 0.0)),
            taker_buy_quote=float(k.get("Q", 0.0)),
            is_closed=bool(k.get("x", False)),
            source=source,
        )

    @classmethod
    def from_rest(cls, symbol: str, interval: str, row: list[Any],
                  source: str = "rest") -> "Kline":
        """Binance REST kline row:
        [open_time, o, h, l, c, volume, close_time, quote_volume, trades,
         taker_buy_base, taker_buy_quote, ignore]
        """
        return cls(
            symbol=symbol.upper(),
            interval=interval,
            open_time=int(row[0]),
            close_time=int(row[6]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            quote_volume=float(row[7]),
            trades=int(row[8] or 0),
            taker_buy_base=float(row[9] or 0.0),
            taker_buy_quote=float(row[10] or 0.0),
            is_closed=True,
            source=source,
        )

    @classmethod
    def from_ch_row(cls, row: tuple) -> "Kline":
        return cls(
            symbol=str(row[0]),
            interval=str(row[1]),
            open_time=dt_to_ms(row[2]),
            close_time=dt_to_ms(row[3]),
            open=float(row[4]),
            high=float(row[5]),
            low=float(row[6]),
            close=float(row[7]),
            volume=float(row[8]),
            quote_volume=float(row[9]),
            trades=int(row[10]),
            taker_buy_base=float(row[11]),
            taker_buy_quote=float(row[12]),
            is_closed=True,
            ingest_time=dt_to_ms(row[13]),
            source="clickhouse",
        )

    @classmethod
    def from_redis(cls, data: dict[str, str]) -> "Kline":
        return cls(
            symbol=data["symbol"],
            interval=data["interval"],
            open_time=int(data["open_time"]),
            close_time=int(data["close_time"]),
            open=float(data["open"]),
            high=float(data["high"]),
            low=float(data["low"]),
            close=float(data["close"]),
            volume=float(data["volume"]),
            quote_volume=float(data.get("quote_volume", 0.0)),
            trades=int(data.get("trades", 0)),
            taker_buy_base=float(data.get("taker_buy_base", 0.0)),
            taker_buy_quote=float(data.get("taker_buy_quote", 0.0)),
            is_closed=data.get("is_closed") == "1",
            ingest_time=int(data.get("ingest_time", 0)),
            source="redis",
        )


def align_open_time(now_ms: int, interval: str) -> int:
    step = interval_ms(interval)
    return (now_ms // step) * step


def validate_ohlcv(k: Kline) -> bool:
    if not k.symbol or k.interval not in INTERVAL_MS:
        return False
    if k.open_time <= 0 or k.close_time < k.open_time:
        return False
    if not all(x == x and x >= 0 for x in (k.open, k.high, k.low, k.close)):
        return False
    if k.high < max(k.open, k.close) or k.low > min(k.open, k.close):
        return False
    return True
