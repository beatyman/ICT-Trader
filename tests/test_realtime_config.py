from __future__ import annotations

from ict_trader.realtime.config import Settings


def test_stream_grouping_respects_connection_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("MAX_STREAMS_PER_CONN", "20")
    symbols = [f"SYM{i}USDT" for i in range(45)]
    cfg = Settings(symbols=symbols, intervals=["15m", "1h"], max_streams_per_conn=20,
                   mode="demo", data_dir=tmp_path)
    groups = cfg.stream_groups()
    assert len(groups) == 6  # 3 chunks for 15m + 3 for 1h
    assert all(len(g) <= 20 for g in groups)
    assert sum(len(g) for g in groups) == 90
    # grouped by interval
    assert {i for _, i in groups[0]} == {"15m"}
    assert {i for _, i in groups[-1]} == {"1h"}


def test_strategy_interval_is_always_subscribed(tmp_path):
    cfg = Settings(symbols=["BTCUSDT"], intervals=["1h"], strategy_interval="15m",
                   mode="demo", data_dir=tmp_path)
    assert "15m" in cfg.intervals
    assert ("BTCUSDT", "15m") in cfg.streams


def test_public_dict_does_not_leak_secrets(tmp_path):
    cfg = Settings(symbols=["BTCUSDT"], intervals=["15m"], mode="demo",
                   data_dir=tmp_path, deepseek_api_key="sk-secret",
                   redis_url="redis://:password@host:6379/0")
    public = cfg.public_dict()
    assert public["ai"]["configured"] is True
    assert "sk-secret" not in str(public)
    assert "password" not in str(public)
