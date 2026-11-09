"""Deterministic paper broker with fixed-fractional risk.

This is explicitly a simulator: no exchange keys, no order routing, no leverage.
The execution model is deliberately conservative:

* entries pay slippage against the trader;
* stop exits fill at the worse of trigger and market, with slippage;
* if a bar touches both stop and target, the intrabar path is unknown — the
  broker closes at the stop before the target can be assumed;
* the time stop exits at the next strategy-bar close.

One position per symbol, a gross notional cap, a daily loss circuit breaker and
a hard 0.5% default equity risk keep the demo from producing fantasy PnL.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import Settings
from .metrics import Metrics, summarize_trades
from .models import interval_ms, ms_to_datetime, utc_ms
from .strategy_runner import TradeSignal

log = logging.getLogger(__name__)


@dataclass
class Position:
    id: str
    signal_id: str
    symbol: str
    interval: str
    direction: str
    entry: float
    stop: float
    target: float
    quantity: float
    notional: float
    entry_time: int
    entry_bar_time: int
    entry_fee: float
    last_price: float
    bars_held: int = 0
    mfe: float = 0.0
    mae: float = 0.0

    @property
    def side(self) -> int:
        return 1 if self.direction == "long" else -1

    @property
    def unrealized(self) -> float:
        return (self.last_price - self.entry) * self.quantity * self.side

    @property
    def unrealized_r(self) -> float:
        risk = abs(self.entry - self.stop) * self.quantity
        return self.unrealized / risk if risk > 0 else 0.0

    @property
    def pnl_pct(self) -> float:
        return (self.last_price / self.entry - 1.0) * 100.0 * self.side

    def to_public(self) -> dict[str, Any]:
        risk_per_unit = abs(self.entry - self.stop)
        return {
            "id": self.id,
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "interval": self.interval,
            "direction": self.direction,
            "entry": round(self.entry, 8),
            "stop": round(self.stop, 8),
            "target": round(self.target, 8),
            "last": round(self.last_price, 8),
            "quantity": round(self.quantity, 8),
            "notional": round(self.notional, 2),
            "entry_time": self.entry_time,
            "bars_held": self.bars_held,
            "unrealized": round(self.unrealized, 2),
            "unrealized_r": round(self.unrealized_r, 2),
            "pnl_pct": round(self.pnl_pct, 2),
            "risk": round(risk_per_unit * self.quantity, 2),
            "mfe_r": round(self.mfe, 2),
            "mae_r": round(self.mae, 2),
        }


@dataclass
class TradeRecord:
    trade_id: str
    signal_id: str
    symbol: str
    interval: str
    direction: str
    entry_time: int
    exit_time: int
    entry: float
    exit: float
    stop: float
    target: float
    quantity: float
    notional: float
    pnl: float
    fees: float
    r_multiple: float
    exit_reason: str
    created_at: int

    def to_public(self) -> dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "interval": self.interval,
            "direction": self.direction,
            "entry_time": self.entry_time,
            "exit_time": self.exit_time,
            "entry": round(self.entry, 8),
            "exit": round(self.exit, 8),
            "stop": round(self.stop, 8),
            "target": round(self.target, 8),
            "quantity": round(self.quantity, 8),
            "notional": round(self.notional, 2),
            "pnl": round(self.pnl, 2),
            "fees": round(self.fees, 2),
            "r_multiple": round(self.r_multiple, 2),
            "exit_reason": self.exit_reason,
            "created_at": self.created_at,
        }

    def for_clickhouse(self) -> dict[str, Any]:
        out = self.to_public()
        out.update({
            "entry_time_dt": ms_to_datetime(self.entry_time),
            "exit_time_dt": ms_to_datetime(self.exit_time),
            "created_at_dt": ms_to_datetime(self.created_at),
        })
        return out


@dataclass
class ExecutionResult:
    accepted: bool
    reason: str
    position_id: str = ""
    fill: float = 0.0
    quantity: float = 0.0


class PaperBroker:
    def __init__(self, settings: Settings, metrics: Metrics) -> None:
        self.settings = settings
        self.metrics = metrics
        self.initial_equity = float(settings.initial_equity)
        self.realized = 0.0
        self.fees = 0.0
        self.positions: dict[str, Position] = {}
        self.trades: list[TradeRecord] = []
        self.equity_curve: list[dict[str, float | int]] = [{
            "t": utc_ms(), "equity": self.initial_equity,
        }]
        self._day = datetime.now(timezone.utc).date().isoformat()
        self._day_start_realized = 0.0
        self._halted_reason = ""
        self.on_trade = None

    # ------------------------------------------------------------------
    @property
    def equity(self) -> float:
        return self.initial_equity + self.realized + sum(p.unrealized for p in self.positions.values())

    @property
    def gross_exposure(self) -> float:
        return sum(p.notional for p in self.positions.values())

    @property
    def open_risk(self) -> float:
        return sum(abs(p.entry - p.stop) * p.quantity for p in self.positions.values())

    def _roll_day(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        if today != self._day:
            self._day = today
            self._day_start_realized = self.realized
            self._halted_reason = ""

    @property
    def daily_pnl(self) -> float:
        self._roll_day()
        return self.realized - self._day_start_realized

    @property
    def daily_loss_limit(self) -> float:
        return self.initial_equity * self.settings.max_daily_loss_pct

    # ------------------------------------------------------------------
    async def execute(self, signal: TradeSignal, mark: float,
                      *, bar_time: int | None = None) -> ExecutionResult:
        self._roll_day()
        if not self.settings.paper_trade:
            return ExecutionResult(False, "paper trading disabled")
        if self._halted_reason:
            return ExecutionResult(False, self._halted_reason)
        if self.daily_pnl <= -self.daily_loss_limit:
            self._halted_reason = "daily loss circuit breaker"
            self.metrics.event("WARN", "risk", "daily loss limit hit; new entries halted")
            return ExecutionResult(False, self._halted_reason)
        if signal.symbol in self.positions:
            return ExecutionResult(False, "position already open")
        if len(self.positions) >= self.settings.max_open_positions:
            return ExecutionResult(False, "max open positions")
        if not (mark > 0 and signal.stop > 0 and signal.target > 0):
            return ExecutionResult(False, "invalid price")

        slip = self.settings.slippage_bps / 10_000.0
        fill = mark * (1.0 + slip) if signal.direction == "long" else mark * (1.0 - slip)
        risk_per_unit = abs(fill - signal.stop)
        if risk_per_unit <= 0:
            return ExecutionResult(False, "zero stop distance")
        if signal.direction == "long" and not (signal.stop < fill < signal.target):
            return ExecutionResult(False, "long price order invalid")
        if signal.direction == "short" and not (signal.target < fill < signal.stop):
            return ExecutionResult(False, "short price order invalid")
        measured_rr = abs(signal.target - fill) / risk_per_unit
        if measured_rr < 1.2:
            return ExecutionResult(False, f"RR {measured_rr:.2f} below floor")

        equity = self.equity
        qty_risk = equity * self.settings.risk_pct / risk_per_unit
        qty_notional = equity * self.settings.max_position_pct / fill
        remaining_gross = max(0.0, equity * 0.95 - self.gross_exposure)
        qty_gross = remaining_gross / fill
        qty = min(qty_risk, qty_notional, qty_gross)
        if qty <= 0:
            return ExecutionResult(False, "no buying power")
        notional = qty * fill
        if notional < 10.0:
            return ExecutionResult(False, "notional below $10")

        fee = notional * (self.settings.commission_bps / 2.0) / 10_000.0
        now = utc_ms()
        direction_code = "L" if signal.direction == "long" else "S"
        pos_id = "POS-" + hashlib.sha1(f"{signal.id}|{now}".encode()).hexdigest()[:12]
        position = Position(
            id=pos_id,
            signal_id=signal.id,
            symbol=signal.symbol,
            interval=signal.interval,
            direction=signal.direction,
            entry=fill,
            stop=signal.stop,
            target=signal.target,
            quantity=qty,
            notional=notional,
            entry_time=now,
            entry_bar_time=bar_time or signal.bar_time,
            entry_fee=fee,
            last_price=fill,
        )
        self.positions[signal.symbol] = position
        self.fees += fee
        self.realized -= fee
        self.metrics.inc("order")
        self.metrics.event(
            "TRADE", "execution",
            f"{direction_code} {signal.symbol} {qty:.6g} @ {fill:.6g} · {measured_rr:.2f}R",
            position_id=pos_id,
            signal_id=signal.id,
        )
        log.info("paper entry %s %s qty=%.8g fill=%.8g stop=%.8g target=%.8g",
                 signal.direction, signal.symbol, qty, fill, signal.stop, signal.target)
        return ExecutionResult(True, "filled", pos_id, fill, qty)

    # ------------------------------------------------------------------
    async def on_bar_closed(self, symbol: str, *, high: float, low: float,
                            close: float) -> None:
        """Conservative closed-bar trigger.

        The intrabar path is unknown.  If both the stop and target are inside
        the bar the stop is always assumed to be hit first.
        """
        position = self.positions.get(symbol.upper())
        if position is None:
            return
        position.last_price = close
        risk = abs(position.entry - position.stop)
        if risk > 0:
            if position.direction == "long":
                position.mfe = max(position.mfe, (high - position.entry) / risk)
                position.mae = min(position.mae, (low - position.entry) / risk)
            else:
                position.mfe = max(position.mfe, (position.entry - low) / risk)
                position.mae = min(position.mae, (position.entry - high) / risk)
        if position.direction == "long":
            if low <= position.stop:
                await self._close(position, position.stop, "stop")
            elif high >= position.target:
                await self._close(position, position.target, "target")
        else:
            if high >= position.stop:
                await self._close(position, position.stop, "stop")
            elif low <= position.target:
                await self._close(position, position.target, "target")

    async def on_price(self, symbol: str, price: float) -> None:
        position = self.positions.get(symbol.upper())
        if position is None or not (price > 0):
            return
        position.last_price = price
        risk = abs(position.entry - position.stop)
        if risk > 0:
            favorable = (price - position.entry) / risk * position.side
            position.mfe = max(position.mfe, favorable)
            position.mae = min(position.mae, favorable)

        reason = ""
        fill = price
        if position.direction == "long":
            if price <= position.stop:
                reason, fill = "stop", min(position.stop, price)
            elif price >= position.target:
                reason, fill = "target", position.target
        else:
            if price >= position.stop:
                reason, fill = "stop", max(position.stop, price)
            elif price <= position.target:
                reason, fill = "target", position.target
        if reason:
            await self._close(position, fill, reason)

    def increment_bar(self, symbol: str, bar_open_time: int, bar_close: float) -> list[str]:
        """Advance the time stop; returns symbols closed on this bar."""
        position = self.positions.get(symbol.upper())
        if position is None:
            return []
        step = interval_ms(position.interval)
        position.bars_held = max(0, int((bar_open_time - position.entry_bar_time) // step))
        if position.bars_held >= self.settings.max_bars:
            return [symbol.upper()]
        return []

    async def close_time_stop(self, symbol: str, price: float) -> None:
        position = self.positions.get(symbol.upper())
        if position is not None:
            await self._close(position, price, "time")

    async def _close(self, position: Position, raw_fill: float, reason: str) -> None:
        if position.symbol not in self.positions:
            return
        slip = self.settings.slippage_bps / 10_000.0
        if reason == "target":
            fill = position.target
        elif reason == "stop":
            fill = raw_fill * (1.0 - slip) if position.direction == "long" else raw_fill * (1.0 + slip)
        else:
            fill = raw_fill * (1.0 - slip) if position.direction == "long" else raw_fill * (1.0 + slip)

        gross = (fill - position.entry) * position.quantity * position.side
        exit_fee = fill * position.quantity * (self.settings.commission_bps / 2.0) / 10_000.0
        net = gross - position.entry_fee - exit_fee
        self.realized += gross - exit_fee
        self.fees += exit_fee
        del self.positions[position.symbol]
        now = utc_ms()
        risk_amount = abs(position.entry - position.stop) * position.quantity
        trade = TradeRecord(
            trade_id="TRD-" + hashlib.sha1(f"{position.id}|{now}".encode()).hexdigest()[:12],
            signal_id=position.signal_id,
            symbol=position.symbol,
            interval=position.interval,
            direction=position.direction,
            entry_time=position.entry_time,
            exit_time=now,
            entry=position.entry,
            exit=fill,
            stop=position.stop,
            target=position.target,
            quantity=position.quantity,
            notional=position.notional,
            pnl=net,
            fees=position.entry_fee + exit_fee,
            r_multiple=net / risk_amount if risk_amount > 0 else 0.0,
            exit_reason=reason,
            created_at=now,
        )
        self.trades.append(trade)
        self.trades = self.trades[-500:]
        self.equity_curve.append({"t": now, "equity": round(self.equity, 2)})
        self.equity_curve = self.equity_curve[-1000:]
        self.metrics.inc("trade")
        self.metrics.inc("realized_pnl", net)
        self.metrics.event(
            "TRADE", "execution",
            f"CLOSE {position.symbol} {position.direction} @ {fill:.6g} · "
            f"{net:+.2f} USDT · {trade.r_multiple:+.2f}R · {reason}",
            trade_id=trade.trade_id,
            pnl=round(net, 2),
        )
        if self.on_trade is not None:
            await self.on_trade(trade)

    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        self._roll_day()
        equity = self.equity
        unrealized = sum(p.unrealized for p in self.positions.values())
        trades = [t.to_public() for t in self.trades[-40:]]
        all_trades = [t.to_public() for t in self.trades]
        curve = self.equity_curve[-160:]
        return {
            "account": {
                "equity": round(equity, 2),
                "initial_equity": round(self.initial_equity, 2),
                "realized": round(self.realized, 2),
                "unrealized": round(unrealized, 2),
                "fees": round(self.fees, 2),
                "daily_pnl": round(self.daily_pnl, 2),
                "daily_loss_limit": round(self.daily_loss_limit, 2),
                "return_pct": round((equity / self.initial_equity - 1.0) * 100.0, 2),
                "open_risk": round(self.open_risk, 2),
                "open_risk_pct": round(100.0 * self.open_risk / equity, 2) if equity else 0.0,
                "gross_exposure": round(self.gross_exposure, 2),
                "halted": self._halted_reason or None,
            },
            "positions": [p.to_public() for p in self.positions.values()],
            "trades": trades,
            "equity_curve": curve,
            "stats": summarize_trades(all_trades),
        }
