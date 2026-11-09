from __future__ import annotations

from ict_trader.realtime.config import Settings, load_env_file


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


def test_env_file_supports_inline_comments(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "ICT_TEST_FLAG=abc # demo | live\n"
        "ICT_TEST_QUOTED='value # kept'\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("ICT_TEST_FLAG", raising=False)
    monkeypatch.delenv("ICT_TEST_QUOTED", raising=False)
    load_env_file(env)
    import os
    assert os.environ["ICT_TEST_FLAG"] == "abc"
    assert os.environ["ICT_TEST_QUOTED"] == "value # kept"


def test_public_dict_does_not_leak_secrets(tmp_path):
    cfg = Settings(symbols=["BTCUSDT"], intervals=["15m"], mode="demo",
                   data_dir=tmp_path, deepseek_api_key="sk-secret",
                   redis_url="redis://:password@host:6379/0")
    public = cfg.public_dict()
    assert public["ai"]["configured"] is True
    assert "sk-secret" not in str(public)
    assert "password" not in str(public)


def test_production_default_mode_is_live(tmp_path):
    cfg = Settings(symbols=["BTCUSDT"], intervals=["15m"], data_dir=tmp_path)
    assert cfg.mode == "live"


def test_kline_source_survives_ch_and_redis(tmp_path):
    from ict_trader.realtime.models import Kline

    bar = Kline(
        symbol="BTCUSDT", interval="15m", open_time=1_700_000_000_000,
        close_time=1_700_000_899_999, open=1.0, high=2.0, low=0.5,
        close=1.5, volume=10.0, is_closed=True, source="ws",
    )
    row = bar.to_ch_row()
    assert len(row) == 15
    assert row[-1] == "ws"
    assert Kline.from_ch_row(row).source == "ws"
    cached = bar.to_redis()
    assert cached["source"] == "ws"
    assert Kline.from_redis(cached).source == "ws"


def test_live_mode_rejects_synthetic_bar_before_cache():
    import asyncio
    from types import SimpleNamespace
    from ict_trader.realtime.engine import StreamSession
    from ict_trader.realtime.metrics import Metrics
    from ict_trader.realtime.models import Kline

    class Redis:
        async def update_latest(self, bar):
            raise AssertionError("demo bar must not reach Redis")

    fake = SimpleNamespace(
        settings=SimpleNamespace(mode="live"),
        metrics=Metrics(),
        latest_bars={},
        redis=Redis(),
        observability=None,
    )
    session = StreamSession(fake, "BTCUSDT", "15m")
    bar = Kline(
        symbol="BTCUSDT", interval="15m", open_time=1_700_000_000_000,
        close_time=1_700_000_899_999, open=1.0, high=2.0, low=0.5,
        close=1.5, volume=10.0, is_closed=False, source="demo",
    )
    asyncio.run(session._handle_live(bar))
    assert fake.latest_bars == {}
    assert fake.metrics.snapshot()["counters"]["rejected_demo_bar"] == 1
