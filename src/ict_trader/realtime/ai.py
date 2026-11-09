"""On-demand DeepSeek V4.1 Flash risk review.

This layer is deliberately narrow:

* it receives only a compact snapshot of the paper account, closed bars and
  current ICT setup(s);
* it can return a structured opinion, never an order;
* it is called only when the operator clicks Review — not on every tick/bar;
* the API key lives in the server process and is never sent to the browser.

The model is a reviewer, not an execution authority.  The deterministic
broker/risk code remains the only component that can open or close positions.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import orjson

from .config import Settings
from .metrics import Metrics
from .models import Kline, ms_to_datetime

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一名只做风险审查的加密货币量化副驾。
你审查的是一套 ICT/SMC 策略的 **模拟盘**。你绝不能下单、修改仓位、编造价格，
也不能把历史信号说成实时可执行信号。

规则：
1. 只能使用用户提供的 JSON 数据；数据不足时明确说"数据不足"。
2. 如果信号 status=HISTORICAL 或 actionable=false，必须明确指出"不可执行"。
3. 必须检查：止损距离、RR、仓位规模、当日亏损、相关性/集中度、
   数据缺口、延迟、以及信号是否已过期。
4. 用简体中文，语气克制，不做收益承诺。
5. 只输出一个 JSON 对象，不要 Markdown，不要代码围栏。
6. ICT 是主交易策略；chan.py / Ichimoku / Wyckoff 只作为辅助上下文。必须使用 analysis_context 中的 15m/1h/4h/1d K 线、ICT arrays、chan 结构、AuxiliaryContext 和共振评分综合判断；不要只依据 signals_for_selected_symbol 的历史信号。DETECTED 不等于已成交。
7. 如果 analysis_context.market_scan=true，且没有 active_setup，允许你基于最新 ICT arrays、流动性、Chan 结构和多周期共振直接提出 trade_plan；不要把“没有 Setup”当作禁止交易。若不满足 RR>=2 或方向冲突，则 trade_plan.open=false。
8. 如果 mode=demo，这是加速模拟行情（不是物理实时行情）；不要把模拟
   时间戳与物理时钟强行比较，也不要用"尚未收盘"作为主要风险，
   应聚焦数据本身和 setup 的可执行性。

输出字段（全部必填）：
{
  "verdict": "support" | "caution" | "reject",
  "confidence": 0-100 的整数,
  "headline": "一句话结论",
  "summary": "2-4 句审查意见",
  "risk_flags": ["具体风险", "..."],
  "execution_notes": ["若执行需要注意的点", "..."],
  "invalidation": "什么情况说明当前判断失效",
  "data_quality": "对数据完整性的简短评价",
  "trade_plan": {
    "open": true/false,
    "direction": "long" | "short",
    "entry_low": 数字,
    "entry_high": 数字,
    "stop": 数字,
    "targets": [TP1, TP2, TP3],
    "size_pct": 0.0-0.30,
    "reason": "为什么这个计划可执行"
  }
}
如果 verdict 不是 support，trade_plan.open 必须为 false。
如果 support，必须给出方向、entry 区间、stop、至少一个 target，且 RR>=2.0。
你只提出 paper trading plan；最终执行由 deterministic paper broker 完成。
"""


class AiReviewError(RuntimeError):
    pass


def _safe_number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if number == number else default
    except (TypeError, ValueError):
        return default


class DeepSeekReviewer:
    def __init__(self, settings: Settings, metrics: Metrics) -> None:
        self.settings = settings
        self.metrics = metrics
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        self._last_review: dict[str, Any] | None = None
        self._last_error = ""
        self.reviews = 0
        self.total_tokens = 0

    @property
    def available(self) -> bool:
        return bool(self.settings.deepseek_api_key)

    def status(self) -> dict[str, Any]:
        return {
            "configured": self.available,
            "enabled": self.available,
            "provider": "deepseek",
            "model": self.settings.deepseek_model,
            "model_name": "DeepSeek-V4.1-Flash",
            "mode": "on-demand risk review",
            "reviews": self.reviews,
            "total_tokens": self.total_tokens,
            "last_error": self._last_error,
            "last_review": self._last_review,
        }

    async def close(self) -> None:
        return None

    # ------------------------------------------------------------------
    def _context(self, snapshot: dict[str, Any], bars: list[Kline],
                 symbol: str, analysis_context: dict[str, Any] | None = None) -> dict[str, Any]:
        bars = bars[-50:]
        all_signals = snapshot.get("signals") or []
        selected_signals = [s for s in all_signals if s.get("symbol") == symbol][:6]
        signal_rows = []
        for sig in selected_signals:
            signal_rows.append({
                "id": sig.get("id"),
                "symbol": sig.get("symbol"),
                "direction": sig.get("direction"),
                "status": sig.get("status"),
                "actionable": sig.get("actionable"),
                "entry": sig.get("entry"),
                "stop": sig.get("stop"),
                "target": sig.get("target"),
                "rr": sig.get("rr"),
                "grade": sig.get("grade"),
                "quality": sig.get("quality"),
                "bar_time": ms_to_datetime(int(sig.get("bar_time", 0))).isoformat()
                if sig.get("bar_time") else None,
                "checks": sig.get("checks"),
                "reason": sig.get("reason"),
            })
        positions = []
        for pos in snapshot.get("positions") or []:
            positions.append({k: pos.get(k) for k in (
                "symbol", "direction", "entry", "last", "stop", "target",
                "quantity", "notional", "unrealized", "unrealized_r", "bars_held",
            )})
        health = snapshot.get("health") or {}
        sessions = health.get("sessions") or {}
        gaps = sum(int((s or {}).get("gaps", 0)) for s in sessions.values())
        selected = next(
            (w for w in (snapshot.get("watchlist") or []) if w.get("symbol") == symbol),
            {},
        )
        active_setup = next(
            (s for s in (snapshot.get("setups") or [])
             if s.get("symbol") == symbol and s.get("status") in {"DETECTED", "OPEN"}),
            None,
        )
        latest_bars = [
            {
                "t": ms_to_datetime(b.open_time).isoformat(),
                "o": b.open, "h": b.high, "l": b.low, "c": b.close, "v": b.volume,
            }
            for b in bars
        ]
        return {
            "as_of_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "mode": snapshot.get("mode"),
            "data_mode_note": (
                "DEMO accelerated simulated tape; timestamps are logical, not wall-clock."
                if snapshot.get("mode") == "demo" else
                "LIVE Binance market data; closed bars are authoritative."
            ),
            "trading_mode": snapshot.get("trading_mode"),
            "venue": snapshot.get("venue"),
            "strategy": snapshot.get("config", {}).get("strategy"),
            "stack": snapshot.get("strategy", {}).get("stack"),
            "interval": snapshot.get("strategy", {}).get("interval"),
            "selected_symbol": symbol,
            "market_context": {
                "last_price": selected.get("price"),
                "change_24h_pct": selected.get("change_pct"),
                "high_24h": selected.get("high"),
                "low_24h": selected.get("low"),
            },
            "closed_bars": latest_bars,
            "active_setup": active_setup,
            "signals_for_selected_symbol": signal_rows,
            "signals_note": (
                "no ICT TradeSignal for the selected symbol; active_setup is the only candidate"
                if active_setup is not None and not signal_rows else
                "no signals for the selected symbol; do not infer one"
                if not signal_rows else "newest first"
            ),
            "other_symbols_with_signals": sorted({
                str(s.get("symbol")) for s in all_signals if s.get("symbol") != symbol
            }),
            "positions": positions,
            "account": snapshot.get("account"),
            "stats": snapshot.get("stats"),
            "health": {
                "redis_ok": (health.get("redis") or {}).get("ok"),
                "clickhouse_ok": (health.get("clickhouse") or {}).get("ok"),
                "wal_pending": (health.get("wal") or {}).get("pending"),
                "gaps": gaps,
                "pipeline_p50_ms": ((snapshot.get("metrics") or {}).get("latency") or {})
                .get("pipeline_ms", {}).get("p50"),
                "pipeline_p99_ms": ((snapshot.get("metrics") or {}).get("latency") or {})
                .get("pipeline_ms", {}).get("p99"),
            },
            "analysis_context": analysis_context or {},
            "constraints": {
                "paper_only": True,
                "max_position_pct": snapshot.get("config", {}).get("max_position_pct"),
                "max_open_positions": snapshot.get("config", {}).get("max_open_positions"),
                "risk_pct": snapshot.get("config", {}).get("risk_pct"),
                "executor": "deterministic paper broker; AI cannot trade",
            },
        }

    # ------------------------------------------------------------------
    async def review(self, snapshot: dict[str, Any], bars: list[Kline],
                     symbol: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.available:
            raise AiReviewError("DeepSeek API key is not configured")
        # One in-flight request and a short cooldown protect against accidental
        # click storms and surprise token spend.
        async with self._lock:
            now = time.monotonic()
            if (now - self._last_call < 15.0 and self._last_review is not None
                    and self._last_review.get("symbol") == symbol):
                return self._last_review
            self._last_call = now
            context = self._context(snapshot, bars, symbol, context)
            user_prompt = (
                "审查以下模拟盘 JSON。只审查 selected_symbol 对应的信号；"
                "若没有该标的的可执行 setup，直接说 no active setup，"
                "不要为了给建议而编造交易。\n"
                + orjson.dumps(context, option=orjson.OPT_INDENT_2).decode()
            )
            payload = {
                "model": self.settings.deepseek_model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.15,
                "max_tokens": self.settings.deepseek_max_tokens,
                "reasoning_effort": self.settings.deepseek_effort,
                "response_format": {"type": "json_object"},
                "stream": False,
            }
            started = time.perf_counter()
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(self.settings.deepseek_timeout, connect=15.0),
                    headers={
                        "Authorization": f"Bearer {self.settings.deepseek_api_key}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                ) as client:
                    response = await client.post(
                        self.settings.deepseek_base_url.rstrip("/") + "/chat/completions",
                        json=payload,
                    )
                if response.status_code != 200:
                    body = response.text[:400].replace(self.settings.deepseek_api_key, "***")
                    raise AiReviewError(f"DeepSeek HTTP {response.status_code}: {body}")
                data = orjson.loads(response.content)
                choice = data["choices"][0]
                if choice.get("finish_reason") == "length":
                    raise AiReviewError(
                        "DeepSeek response hit the token limit; raise DEEPSEEK_MAX_TOKENS"
                    )
                content = choice["message"]["content"]
                review = self._parse(content)
                usage = data.get("usage") or {}
                tokens = int(usage.get("total_tokens", 0) or 0)
                self.reviews += 1
                self.total_tokens += tokens
                self._last_error = ""
                result = {
                    **review,
                    "raw_output": content[:20000],
                    "model": data.get("model", self.settings.deepseek_model),
                    "model_name": "DeepSeek-V4.1-Flash",
                    "usage": usage,
                    "latency_ms": round((time.perf_counter() - started) * 1000.0, 1),
                    "reviewed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "symbol": symbol,
                    "disclaimer": "AI review is not an execution signal or financial advice.",
                }
                self._last_review = result
                self.metrics.inc("ai_review")
                self.metrics.inc("ai_tokens", tokens)
                self.metrics.event(
                    "INFO", "ai",
                    f"DeepSeek review {review['verdict']} · {tokens} tokens",
                    symbol=symbol,
                )
                return result
            except AiReviewError as exc:
                self._last_error = str(exc)[:400]
                self.metrics.inc("ai_error")
                self.metrics.event("ERROR", "ai", "DeepSeek review failed", error=self._last_error)
                raise
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"[:400]
                self.metrics.inc("ai_error")
                self.metrics.event("ERROR", "ai", "DeepSeek review failed", error=self._last_error)
                raise AiReviewError(self._last_error) from exc

    @staticmethod
    def _parse(content: str) -> dict[str, Any]:
        text = (content or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()
        try:
            obj = orjson.loads(text)
        except Exception as exc:
            raise AiReviewError(f"DeepSeek returned non-JSON content: {text[:160]}") from exc
        if not isinstance(obj, dict):
            raise AiReviewError("DeepSeek returned a non-object JSON document")
        verdict = str(obj.get("verdict", "caution")).lower()
        if verdict not in {"support", "caution", "reject"}:
            verdict = "caution"
        confidence = int(max(0, min(100, _safe_number(obj.get("confidence"), 0))))
        normalized = {
            "verdict": verdict,
            "confidence": confidence,
            "headline": str(obj.get("headline") or "No headline"),
            "summary": str(obj.get("summary") or ""),
            "risk_flags": [str(x) for x in (obj.get("risk_flags") or [])][:8],
            "execution_notes": [str(x) for x in (obj.get("execution_notes") or [])][:8],
            "invalidation": str(obj.get("invalidation") or ""),
            "data_quality": str(obj.get("data_quality") or ""),
            "trade_plan": obj.get("trade_plan"),
        }
        from .ai_schema import ReviewDecision
        try:
            return ReviewDecision.model_validate(normalized).model_dump()
        except Exception as exc:
            raise AiReviewError(f"DeepSeek schema invalid: {exc}") from exc
