"""AMD Po3 目标 + 实时优势统计验收测试。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ict_trader.amd import po3_fib_target, AmdEdgeStats  # noqa: E402


def test_fib_target_bull_sell_side_sweep():
    # sell-side 扫单：操纵跌破下沿(90)，上沿 110 → fib 1.5 外延
    tgt, d, leg = po3_fib_target(range_high=110.0, range_low=100.0,
                                 manipulation_extreme=90.0, sweep_direction="bull", fib=1.5)
    assert d == "long"
    assert leg == 20.0            # 110 - 90
    assert tgt == 110.0 + 0.5 * 20.0 == 120.0


def test_fib_target_bear_buy_side_sweep():
    # buy-side 扫单：操纵涨破上沿(120)，下沿 100 → fib 1.5 外延
    tgt, d, leg = po3_fib_target(range_high=110.0, range_low=100.0,
                                 manipulation_extreme=120.0, sweep_direction="bear", fib=1.5)
    assert d == "short"
    assert leg == 20.0            # 120 - 100
    assert tgt == 100.0 - 0.5 * 20.0 == 90.0


def test_edge_stats_target_rate_and_avg_r():
    s = AmdEdgeStats(min_sample=3)
    s.record("target", 2.0)
    s.record("stop", -1.0)
    s.record("timeout", 0.3)
    assert s.total == 3
    assert abs(s.target_rate - 1 / 3) < 1e-9
    assert abs(s.avg_r - (2.0 - 1.0 + 0.3) / 3) < 1e-9
    assert s.ready is True
    assert s.form == "▰▱◌"


def test_edge_stats_not_ready_below_min_sample():
    s = AmdEdgeStats(min_sample=10)
    s.record("target", 1.0)
    s.record("stop", -1.0)
    assert s.ready is False
    d = s.as_dict()
    assert d["target_rate"] == 0.5
    assert d["ready"] is False
