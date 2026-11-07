"""自动分析工具验收测试（背景层 / 融合层 / 评分层）。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ict_trader.background import wyckoff_background, ichimoku_background  # noqa: E402
from ict_trader.main_strategy import ICTSignal  # noqa: E402
from ict_trader.fusion import fuse  # noqa: E402
from ict_trader.scoring import score  # noqa: E402
from ict_trader.background import (WyckoffBackground, IchimokuBackground,  # noqa: E402
                                   ChanlunBackground)


def _trend_df(n=200, up=True, seed=1):
    rng = np.random.RandomState(seed)
    close = np.cumsum(rng.randn(n) * (0.5 if up else -0.5)) + 100
    high = close + abs(rng.randn(n)) * 0.3
    low = close - abs(rng.randn(n)) * 0.3
    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    df = pd.DataFrame({"date": idx, "open": close, "high": high, "low": low,
                       "close": close, "volume": abs(rng.randn(n)) * 100})
    return df


def test_background_never_outputs_entry():
    """验收2：辅助背景只输出 allow/veto/size，不输出入场价。"""
    df = _trend_df(200, up=True)
    wy = wyckoff_background(df)
    ichi = ichimoku_background(df)
    for b in (wy, ichi):
        assert hasattr(b, "allow_long") and hasattr(b, "allow_short") and hasattr(b, "size_factor")
        assert not hasattr(b, "entry") and not hasattr(b, "stop")


def test_fusion_hard_veto():
    """验收4：辅助 veto 硬性生效，ICT 高分不能覆盖。"""
    ict = ICTSignal(direction="long", confidence=95, entry=100, stop=95, tp1=110, tp2=115)
    wy = WyckoffBackground(bias="short", allow_long=False, allow_short=True, size_factor=1.0)
    ichi = IchimokuBackground(cloud_state="above", allow_long=True, size_factor=1.0)
    out = fuse(ict, wy, ichi)
    assert out.action == "avoid"  # Wyckoff veto 生效
    assert out.veto_reason != ""


def test_fusion_no_ict_signal():
    """验收3：ICT 无信号 → 不交易。"""
    ict = ICTSignal(direction="neutral", confidence=0)
    wy = WyckoffBackground(bias="long", allow_long=True, size_factor=1.0)
    ichi = IchimokuBackground(cloud_state="above", allow_long=True, size_factor=1.0)
    out = fuse(ict, wy, ichi)
    assert out.action == "wait"


def test_fusion_all_align_full_size():
    """验收3：一致 → size 缩放。"""
    ict = ICTSignal(direction="long", confidence=80, entry=100, stop=95, tp1=110, tp2=115)
    wy = WyckoffBackground(bias="long", allow_long=True, size_factor=1.0)
    ichi = IchimokuBackground(cloud_state="above", allow_long=True, size_factor=1.0)
    out = fuse(ict, wy, ichi)
    assert out.action == "enter"
    assert out.size_factor > 0


def test_fusion_chanlun_htf_veto():
    """缠论 HTF 硬性否决：ICT long 但日线缠论 short → avoid。"""
    ict = ICTSignal(direction="long", confidence=95, entry=100, stop=95, tp1=110, tp2=115)
    wy = WyckoffBackground(bias="long", allow_long=True, size_factor=1.0)
    ichi = IchimokuBackground(cloud_state="above", allow_long=True, size_factor=1.0)
    chan_htf = ChanlunBackground(bias="short", allow_short=True, size_factor=0.7)
    out = fuse(ict, wy, ichi, chan_htf=chan_htf)
    assert out.action == "avoid"
    assert "Chanlun" in out.veto_reason


def test_fusion_chanlun_ltf_delays_entry_not_flip():
    """缠论 LTF 择时：ICT long 但 15m 缠论 short → wait（不翻转方向）。"""
    ict = ICTSignal(direction="long", confidence=80, entry=100, stop=95, tp1=110, tp2=115)
    wy = WyckoffBackground(bias="long", allow_long=True, size_factor=1.0)
    ichi = IchimokuBackground(cloud_state="above", allow_long=True, size_factor=1.0)
    chan_ltf = ChanlunBackground(bias="short", allow_short=True, size_factor=0.7)
    out = fuse(ict, wy, ichi, chan_ltf=chan_ltf)
    assert out.direction == "long"  # 方向不翻转
    assert out.action == "wait"     # LTF 未对齐，等入场


def test_chanlun_background_output():
    """缠论背景只输出 allow/veto/size，不输出入场价。"""
    from ict_trader.chanlun import chanpy_available
    if not chanpy_available():
        pytest.skip("chan.py not available")
    from ict_trader.background import chanlun_background
    df = _trend_df(200, up=True)
    b = chanlun_background(df, freq="1d")
    assert hasattr(b, "bias") and hasattr(b, "position") and hasattr(b, "bsp_type")
    assert hasattr(b, "allow_long") and hasattr(b, "allow_short") and hasattr(b, "size_factor")
    assert not hasattr(b, "entry") and not hasattr(b, "stop")


def test_scoring_outputs_full_json():
    """验收5：评分输出严格 JSON，字段完整。"""
    ict = ICTSignal(direction="long", confidence=70, entry=100, stop=95, tp1=110, tp2=115,
                    rules={"L2-04": "PASS", "L1-04": "PASS", "L3-10": "PASS"})
    wy = WyckoffBackground(bias="long", allow_long=True, size_factor=1.0)
    ichi = IchimokuBackground(cloud_state="above", cloud_color="bullish", allow_long=True, size_factor=1.0)
    from ict_trader.fusion import FusionOutput
    fused = FusionOutput(direction="long", entry=100, stop=95, tp1=110, tp2=115,
                         size_factor=0.8, confidence=80, action="enter")
    out = score(ict, wy, ichi, fused, symbol="X", timestamp="2020-01-01")
    for k in ["timestamp", "symbol", "ict", "wyckoff_background", "ichimoku_background",
              "fused_direction", "fused_confidence", "size_factor", "score",
              "score_breakdown", "action", "entry", "stop", "tp1", "tp2",
              "invalidation", "reasoning", "red_flags"]:
        assert k in out, f"missing {k}"
    assert set(out["score_breakdown"]) == {"ict_setup_quality", "liquidity_and_structure",
                                           "wyckoff_alignment", "ichimoku_alignment",
                                           "risk_reward_quality"}
    assert -100 <= out["score"] <= 100
