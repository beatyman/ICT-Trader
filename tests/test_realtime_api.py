from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from ict_trader.realtime.api import create_app  # noqa: E402
from ict_trader.realtime.metrics import Metrics  # noqa: E402
from ict_trader.realtime.models import Kline  # noqa: E402


class FakeRunner:
    def __init__(self) -> None:
        self.signals = []


class FakeEngine:
    def __init__(self, tmp_path):
        from ict_trader.realtime.config import Settings
        self.settings = Settings(symbols=["BTCUSDT"], intervals=["15m"], mode="demo",
                                 data_dir=tmp_path)
        self.metrics = Metrics()
        self.runner = FakeRunner()
        self.phase = "ready"
        self.boot_error = ""
        self.boot_done = 1
        self.boot_total = 1
        self.settings.dashboard_port = 8080

    async def start(self):
        self.phase = "ready"

    async def stop(self):
        self.phase = "stopped"

    async def klines(self, symbol, interval, limit):
        now = 1_700_000_000_000
        return [Kline(symbol=symbol, interval=interval, open_time=now, close_time=now + 899999,
                      open=1, high=2, low=0.5, close=1.5, volume=10, is_closed=True).to_public()]

    async def flatten_all(self):
        return 0

    def snapshot(self):
        return {
            "mode": "demo", "phase": "ready", "boot_error": "", "started_at": "",
            "server_time": 1_700_000_000_000,
            "boot": {"done": 1, "total": 1},
            "venue": "BINANCE SPOT", "trading_mode": "PAPER",
            "config": self.settings.public_dict(),
            "health": {
                "redis": {"ok": True}, "clickhouse": {"ok": True},
                "wal": {"pending": 0, "dead_letters": 0}, "feed": {},
                "sessions": {},
            },
            "last_update": 1_700_000_000_000,
            "strategy": {"interval": "15m"}, "signals": [], "watchlist": [],
            "account": {"equity": 100000, "initial_equity": 100000,
                        "open_risk_pct": 0, "open_risk": 0, "daily_pnl": 0,
                        "return_pct": 0},
            "positions": [], "trades": [], "equity_curve": [],
            "stats": {"trades": 0}, "metrics": self.metrics.snapshot(),
            "selected": "BTCUSDT",
        }


def test_dashboard_routes_and_websocket(tmp_path):
    app = create_app(FakeEngine(tmp_path))
    with TestClient(app) as client:
        index = client.get("/")
        assert index.status_code == 200
        assert "ICT // REALTIME PAPER TERMINAL" in index.text
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["ok"] is True
        bars = client.get("/api/klines?symbol=BTCUSDT&interval=15m&limit=20")
        assert bars.status_code == 200
        assert len(bars.json()["bars"]) == 1
        with client.websocket_connect("/ws") as ws:
            payload = ws.receive_json()
            assert payload["trading_mode"] == "PAPER"
