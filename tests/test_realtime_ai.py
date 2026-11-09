from __future__ import annotations

import pytest

from ict_trader.realtime.ai import AiReviewError, DeepSeekReviewer
from ict_trader.realtime.config import Settings
from ict_trader.realtime.metrics import Metrics
from ict_trader.realtime.models import Kline


def reviewer(tmp_path, key: str = "") -> DeepSeekReviewer:
    cfg = Settings(symbols=["BTCUSDT"], intervals=["15m"], mode="demo",
                   data_dir=tmp_path, deepseek_api_key=key)
    return DeepSeekReviewer(cfg, Metrics())


def bar(i: int) -> Kline:
    t = 1_700_000_000_000 + i * 900_000
    return Kline(symbol="BTCUSDT", interval="15m", open_time=t, close_time=t + 899_999,
                 open=100, high=102, low=99, close=101, volume=10, is_closed=True)


def test_status_and_availability(tmp_path):
    r = reviewer(tmp_path)
    assert not r.available
    assert r.status()["configured"] is False
    r2 = reviewer(tmp_path / "with-key", key="sk-test-not-real")
    assert r2.available
    assert r2.status()["model"] == "deepseek-flash"


def test_context_is_compact_and_contains_risk(tmp_path):
    r = reviewer(tmp_path)
    snapshot = {
        "mode": "demo", "trading_mode": "PAPER", "venue": "BINANCE SPOT",
        "config": {"strategy": "ICT", "risk_pct": 0.005, "max_position_pct": 0.3,
                   "max_open_positions": 5},
        "strategy": {"stack": "4h_15m", "interval": "15m"},
        "watchlist": [{"symbol": "BTCUSDT", "price": 101, "change_pct": 1.2,
                       "high": 110, "low": 95}],
        "signals": [{"id": "SIG-1", "symbol": "BTCUSDT", "direction": "long",
                     "status": "HISTORICAL", "actionable": False, "entry": 100,
                     "stop": 98, "target": 106, "rr": 3, "grade": 4, "quality": .7,
                     "bar_time": 1_700_000_000_000, "checks": {"sweep": True}}],
        "positions": [], "account": {"equity": 100000}, "stats": {"trades": 0},
        "health": {"redis": {"ok": True}, "clickhouse": {"ok": True},
                   "wal": {"pending": 0}, "sessions": {"BTCUSDT:15m": {"gaps": 1}}},
        "metrics": {"latency": {"pipeline_ms": {"p50": 1, "p99": 3}}},
    }
    ctx = r._context(snapshot, [bar(i) for i in range(100)], "BTCUSDT")
    assert ctx["selected_symbol"] == "BTCUSDT"
    assert len(ctx["closed_bars"]) == 50
    assert ctx["signals_for_selected_symbol"][0]["status"] == "HISTORICAL"
    assert ctx["health"]["gaps"] == 1
    assert ctx["constraints"]["paper_only"] is True
    assert "deepseek" not in str(ctx).lower()


def test_parse_valid_fenced_json():
    content = """```json
    {"verdict":"reject","confidence":88,"headline":"不可执行",
     "summary":"历史信号","risk_flags":["信号已过期"],
     "execution_notes":["等待新收盘确认"],"invalidation":"新高",
     "data_quality":"完整"}
    ```"""
    out = DeepSeekReviewer._parse(content)
    assert out["verdict"] == "reject"
    assert out["confidence"] == 88
    assert out["risk_flags"] == ["信号已过期"]


def test_parse_invalid_json_raises():
    with pytest.raises(AiReviewError):
        DeepSeekReviewer._parse("not json")


def test_parse_clamps_confidence_and_unknown_verdict():
    out = DeepSeekReviewer._parse('{"verdict":"buy","confidence":999}')
    assert out["verdict"] == "caution"
    assert out["confidence"] == 100
