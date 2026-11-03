"""Gates added by the BTC 15m optimisation study, and the cost convention."""
import numpy as np
import pandas as pd
import pytest

from ict_trader.setup import SetupConfig, apply_quality_profile
from ict_trader.strategy import StrategyConfig, net_rr


def test_commission_is_one_round_trip_of_commission_bps():
    """The documented GS-08 formula, and one implementation for the package."""
    entry, stop, target = 100.0, 99.0, 102.5
    gross, net = net_rr(entry, stop, target, "bull",
                        commission_bps=10.0, slippage_bps=0.0)
    cost = 100.0 * 0.0005 + 102.5 * 0.0005        # 5 bps per leg = 10 bps round trip
    assert gross == pytest.approx(2.5, abs=1e-12)
    assert cost == pytest.approx(0.10125, abs=1e-12)
    assert net == pytest.approx((2.5 - cost) / (1.0 + cost), abs=1e-12)
    # halving commission_bps must move the answer the way half the cost would
    _, net_5 = net_rr(entry, stop, target, "bull",
                      commission_bps=5.0, slippage_bps=0.0)
    cost5 = cost / 2
    assert net_5 == pytest.approx((2.5 - cost5) / (1.0 + cost5), abs=1e-12)


def test_slippage_is_per_side_and_rules_agree_with_strategy():
    entry, stop, target = 100.0, 99.0, 102.5
    _, no_slip = net_rr(entry, stop, target, "bull",
                        commission_bps=10.0, slippage_bps=0.0)
    _, with_slip = net_rr(entry, stop, target, "bull",
                          commission_bps=10.0, slippage_bps=1.0)
    cost = 100.0 * 0.0006 + 102.5 * 0.0006        # 5 bps commission + 1 bps slip
    assert with_slip == pytest.approx((2.5 - cost) / (1.0 + cost), abs=1e-12)
    assert with_slip < no_slip
    # every copy in the tree must return the same numbers
    from ict_trader.rules import _plan_net_rr
    from ict_trader.scalper import _net_rr as scalper_net_rr
    from ict_trader.scenarios import _net_rr as scenario_net_rr
    plan = {"entry": entry, "sl": stop, "tp": target, "direction": "long"}
    ref = net_rr(entry, stop, target, "long")
    assert _plan_net_rr(plan) == pytest.approx(ref)
    assert scenario_net_rr(entry, stop, target, "long") == pytest.approx(ref)
    assert scalper_net_rr(entry, stop, target, "long") == pytest.approx(ref)


def test_net_rr_rejects_unknown_direction():
    with pytest.raises(ValueError):
        net_rr(100.0, 99.0, 102.5, "sideways")


def test_btc_profile_requires_wide_stop_and_displacement():
    cfg = apply_quality_profile(SetupConfig(market="crypto", quality_profile="btc_15m"))
    assert cfg.min_stop_atr == 1.4
    assert cfg.min_disp_atr == 1.45
    assert cfg.rr >= 2.5


def test_btc_profile_never_loosens_a_user_setting():
    cfg = apply_quality_profile(SetupConfig(market="crypto", quality_profile="btc_15m",
                                            min_stop_atr=3.0, min_disp_atr=2.0, rr=4.0))
    assert cfg.min_stop_atr == 3.0   # max(), not overwrite
    assert cfg.min_disp_atr == 2.0
    assert cfg.rr == 4.0


def test_standard_profile_is_untouched():
    base = SetupConfig(market="crypto")
    out = apply_quality_profile(base)
    assert out == base
    assert out.min_stop_atr == 0.0 and out.min_sweep_strength == 0.0
