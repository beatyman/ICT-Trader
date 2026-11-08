"""成交量分布形状分类（D/B/P/Double Zone）验收测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ict_trader.vp_anatomy import poc_vah_val, classify_profile  # noqa: E402


def _gauss(t, m, s):
    return np.exp(-0.5 * ((t - m) / s) ** 2)


def _profile(n, synth):
    prices = list(np.linspace(0, 1, n))
    density = [synth((i + 0.5) / n) + 0.04 for i in range(n)]
    return prices, density


def test_poc_vah_val_returns_valid_levels():
    prices, density = _profile(30, lambda t: _gauss(t, 0.5, 0.15))
    poc, vah, val = poc_vah_val(prices, density, pct=0.7)
    assert val < poc < vah  # VAL < POC < VAH


def test_d_shape_poc_center():
    prices, density = _profile(30, lambda t: _gauss(t, 0.5, 0.17))
    kind, poc, p2 = classify_profile(density)
    assert kind == 1  # D-shape（POC 居中）


def test_b_shape_poc_low():
    prices, density = _profile(30, lambda t: _gauss(t, 0.28, 0.12) + 0.3 * _gauss(t, 0.62, 0.22))
    kind, poc, p2 = classify_profile(density)
    assert kind == 2  # B-shape（POC 偏下）


def test_p_shape_poc_high():
    prices, density = _profile(30, lambda t: _gauss(t, 0.76, 0.10) + 0.28 * _gauss(t, 0.45, 0.22))
    kind, poc, p2 = classify_profile(density)
    assert kind == 3  # P-shape（POC 偏上）


def test_double_zone_bimodal():
    prices, density = _profile(30, lambda t: _gauss(t, 0.72, 0.08) + 0.9 * _gauss(t, 0.27, 0.08))
    kind, poc, p2 = classify_profile(density)
    assert kind == 4  # Double Zone（双峰）
    assert p2 >= 0
