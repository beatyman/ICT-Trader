from ict_trader.realtime.review import build_review, summarize


def trades():
    return [
        {"trade_id": "T1", "signal_id": "SET-1", "symbol": "BTCUSDT",
         "direction": "long", "pnl": 100.0, "r_multiple": 2.0,
         "exit_reason": "target", "exit_time": 2, "bars_held": 4,
         "mfe_r": 2.2, "mae_r": -0.3},
        {"trade_id": "T2", "signal_id": "SET-2", "symbol": "ETHUSDT",
         "direction": "short", "pnl": -50.0, "r_multiple": -1.0,
         "exit_reason": "stop", "exit_time": 1, "bars_held": 2,
         "mfe_r": 0.4, "mae_r": -1.0},
    ]


def setups():
    return [
        {"setup_id": "SET-1", "engine": "chan", "rr": 2.0,
         "resonance_score": 3, "interval": "15m"},
        {"setup_id": "SET-2", "engine": "wyckoff", "rr": 2.0,
         "resonance_score": 1, "interval": "15m"},
    ]


def test_review_aggregates_actual_rr_and_pnl():
    review = build_review(trades(), setups())
    summary = review["summary"]
    assert summary["trades"] == 2
    assert summary["wins"] == 1
    assert summary["losses"] == 1
    assert summary["win_rate"] == 0.5
    assert summary["net_pnl"] == 50.0
    assert summary["profit_factor"] == 2.0
    assert summary["avg_r"] == 0.5
    assert summary["avg_planned_rr"] == 2.0
    assert summary["rr_gap"] == -1.5
    assert review["by_symbol"][0]["key"] == "BTCUSDT"
    assert review["by_exit"][0]["key"] == "stop"
    assert review["trades"][0]["setup_id"] == "SET-1"
    assert review["trades"][0]["engine"] == "chan"
