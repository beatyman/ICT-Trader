"""三引擎融合决策算法验收测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ict_trader.confluence import FusionInput, decide  # noqa: E402


def test_vote_short_wins():
    # Ichimoku 空 + ICT 空 = 2 空票；Wyckoff 吸筹 valid = 1 多票 → 空
    inp = FusionInput(current_price=76208, wyckoff_phase="吸筹", wyckoff_valid=True,
                      key_level=75736, ichimoku_filter="空", cloud_bottom=78149,
                      cloud_top=82494, ict_direction="空", price_in_premium=False)
    p = decide(inp)
    assert p.direction == "空"
    assert p.lean == "偏空"
    assert p.position_state == "需等待合适位置"  # 不在 premium
    assert p.layers["L4_位置校验"] == "FAIL"
    assert p.confidence == 6  # 5+2+1-1=7，位置未到压到 6


def test_vote_long_wins_and_immediate():
    inp = FusionInput(current_price=100, wyckoff_phase="吸筹", wyckoff_valid=True,
                      key_level=95, ichimoku_filter="多", cloud_bottom=90,
                      cloud_top=110, ict_direction="多", price_in_premium=True)
    p = decide(inp)
    assert p.direction == "多"
    assert p.position_state == "可考虑入场"
    assert p.layers["L4_位置校验"] == "PASS"
    # 入场 = [100*0.998, 100*1.002] = [99.8, 100.2] → [100, 100]
    # 止损 = 100*0.975 = 97.5 → 98（round）
    assert p.stop == 98
    # 风险 = 99.8 - 97.5 = 2.3；目标1 = 99.8 + 4.6 = 104.4 → 104
    assert p.target1 == 104


def test_tie_is_wait():
    inp = FusionInput(current_price=100, wyckoff_phase="中性", wyckoff_valid=False,
                      key_level=0, ichimoku_filter="观望", cloud_bottom=0,
                      cloud_top=0, ict_direction="观望", price_in_premium=False)
    p = decide(inp)
    assert p.direction == "观望"
    assert p.entry_low == 0 and p.entry_high == 0 and p.stop == 0
    assert p.confidence == 5


def test_short_in_premium_is_immediate():
    # 已在 premium → 允许立即入场
    inp = FusionInput(current_price=100, wyckoff_phase="派发", wyckoff_valid=True,
                      key_level=110, ichimoku_filter="空", cloud_bottom=90,
                      cloud_top=110, ict_direction="空", price_in_premium=True)
    p = decide(inp)
    assert p.direction == "空"
    assert p.position_state == "可考虑入场"
    assert p.layers["L4_位置校验"] == "PASS"
    # 止损 = 100*1.025 = 102.5 → 103
    assert p.stop == 103


def test_invalidation_includes_broken_key_when_invalid():
    inp = FusionInput(current_price=100, wyckoff_phase="派发", wyckoff_valid=False,
                      key_level=95, ichimoku_filter="空", cloud_bottom=90,
                      cloud_top=110, ict_direction="空", price_in_premium=False)
    p = decide(inp)
    assert any("跌破" in iv or "站上" in iv for iv in p.invalidations)
    # 失效条件应包含「已跌破 key_level」
    assert any("跌破" in iv for iv in p.invalidations)
