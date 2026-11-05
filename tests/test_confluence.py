"""三引擎融合决策算法验收测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ict_trader.confluence import FusionInput, decide  # noqa: E402


def test_vote_short_wins():
    # Ichimoku short + ICT short = 2 short votes; Wyckoff accumulation valid = 1 long vote -> short
    inp = FusionInput(current_price=76208, wyckoff_phase="accumulation", wyckoff_valid=True,
                      key_level=75736, ichimoku_filter="short", cloud_bottom=78149,
                      cloud_top=82494, ict_direction="short", price_in_premium=False)
    p = decide(inp)
    assert p.direction == "short"
    assert p.lean == "bearish"
    assert p.position_state == "wait for position"  # not in premium
    assert p.layers["L4_position"] == "FAIL"
    assert p.confidence == 6  # 5+2+1-1=7, capped to 6 (position not ready)


def test_vote_long_wins_and_immediate():
    inp = FusionInput(current_price=100, wyckoff_phase="accumulation", wyckoff_valid=True,
                      key_level=95, ichimoku_filter="long", cloud_bottom=90,
                      cloud_top=110, ict_direction="long", price_in_premium=True)
    p = decide(inp)
    assert p.direction == "long"
    assert p.position_state == "can enter"
    assert p.layers["L4_position"] == "PASS"
    # entry = [100*0.998, 100*1.002] = [99.8, 100.2] -> [100, 100]
    # stop = 100*0.975 = 97.5 -> 98 (round half up)
    assert p.stop == 98
    # risk = 99.8 - 97.5 = 2.3; target1 = 99.8 + 4.6 = 104.4 -> 104
    assert p.target1 == 104


def test_tie_is_wait():
    inp = FusionInput(current_price=100, wyckoff_phase="neutral", wyckoff_valid=False,
                      key_level=0, ichimoku_filter="wait", cloud_bottom=0,
                      cloud_top=0, ict_direction="wait", price_in_premium=False)
    p = decide(inp)
    assert p.direction == "wait"
    assert p.entry_low == 0 and p.entry_high == 0 and p.stop == 0
    assert p.confidence == 5


def test_short_in_premium_is_immediate():
    # already in premium -> immediate entry allowed
    inp = FusionInput(current_price=100, wyckoff_phase="distribution", wyckoff_valid=True,
                      key_level=110, ichimoku_filter="short", cloud_bottom=90,
                      cloud_top=110, ict_direction="short", price_in_premium=True)
    p = decide(inp)
    assert p.direction == "short"
    assert p.position_state == "can enter"
    assert p.layers["L4_position"] == "PASS"
    # stop = 100*1.025 = 102.5 -> 103
    assert p.stop == 103


def test_invalidation_includes_broken_key_when_invalid():
    inp = FusionInput(current_price=100, wyckoff_phase="distribution", wyckoff_valid=False,
                      key_level=95, ichimoku_filter="short", cloud_bottom=90,
                      cloud_top=110, ict_direction="short", price_in_premium=False)
    p = decide(inp)
    assert any("close above" in iv or "broke below" in iv for iv in p.invalidations)
    # invalidation should include "broke below key_level"
    assert any("broke below" in iv for iv in p.invalidations)
