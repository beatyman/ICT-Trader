"""P0-18 regressions: one accounting model across every surface.

Invariants (CODE_MAPPING P0-18; RE:RM-14, RE:RM-16, RK-07, RE:RM-15/V28, A7,
TB-T11):

* one ``net_rr`` formula and one cost basis, shared by the backtest, the risk
  plan, the CLI plan card and the paper broker;
* one declared same-bar tie-break helper, called by both execution paths;
* one ``risk_pct`` default, read from one register entry;
* one RR floor per declared mode (``floor_3_1`` / ``floor_session_1_5`` /
  ``filter_2_1`` / ``none``) -- no private 1.2 / 2.0 / 2.5 in a gate;
* one ``min_1d_score`` default across ``SetupConfig``, ``setup.main`` and
  ``cli.py``;
* one trades/day value across the same surfaces.
"""
from __future__ import annotations

import argparse
import asyncio

import pandas as pd
import pytest

import ict_trader.cli as cli_module
import ict_trader.setup as setup_api
from ict_trader.cisd import CISD
from ict_trader.concepts import ICTConfig, ICTResult
from ict_trader.realtime.analysis.risk import (
    NET_RR_BLOCKER,
    build_risk_plan,
)
from ict_trader.realtime.broker import PaperBroker
from ict_trader.realtime.config import Settings
from ict_trader.realtime.metrics import Metrics
from ict_trader.realtime.models import utc_ms
from ict_trader.realtime.strategy_runner import TradeSignal
from ict_trader.setup import SetupConfig
from ict_trader.strategy import (
    ACCOUNTING_REGISTER,
    DEFAULT_COMMISSION_BPS,
    DEFAULT_COST_MODEL,
    DEFAULT_MAX_TRADES_PER_DAY,
    DEFAULT_RISK_PCT,
    DEFAULT_RR_MODE,
    DEFAULT_SELL_TAX_BPS,
    DEFAULT_SLIPPAGE_BPS,
    MIN_RR,
    NET_RR_FLOOR,
    RR_MODE_FLOORS,
    CostModel,
    StrategyConfig,
    declared,
    net_rr,
    rr_floor,
    same_bar_exit,
    simulate_cisd,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _ohlc(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df["date"] = pd.date_range("2026-01-01", periods=len(df), freq="15min")
    df["volume"] = 1000.0
    return df


def _one_cisd(df: pd.DataFrame, *, stop: float = 95.0,
              target: float | None = 115.0, end: int = 1) -> ICTResult:
    c = CISD("bull", level=100.0, extreme=stop, body_extreme=stop,
             start=max(0, end - 1), end=end, sl=stop, target=target,
             grade=4, checks={"sweep": True})
    return ICTResult(bars=df, cisds=[c], config=ICTConfig())


def _signal(direction: str = "long", *, stop: float = 95.0,
            target: float = 110.6) -> TradeSignal:
    return TradeSignal(
        id="SIG-ACC", symbol="BTCUSDT", interval="15m", bar_time=utc_ms(),
        created_at=utc_ms(), direction=direction, entry=100.0, stop=stop,
        target=target, rr=3.0, grade=4, quality=0.8, status="ARMED",
        actionable=True, executed=False, reason="accounting test",
        checks={"sweep": True, "pda": True, "volume": True},
        setup_level=100.0,
    )


def _broker(tmp_path, **overrides) -> PaperBroker:
    cfg = Settings(symbols=["BTCUSDT"], intervals=["15m"], mode="demo",
                   data_dir=tmp_path, **overrides)
    return PaperBroker(cfg, Metrics())


def _setup_args():
    return setup_api._parser().parse_args(["--csv", "x.csv"])


def _cli_args():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    cli_module._add_setup_args(sub.add_parser("backtest"))
    return ap.parse_args(["backtest", "--csv", "x.csv"])


# --------------------------------------------------------------------------- #
# 1. one net_rr formula and one cost basis
# --------------------------------------------------------------------------- #
def test_one_net_rr_formula_is_shared_and_direction_invariant():
    expected = net_rr(100.0, 97.5, 108.0, "long")
    assert expected == DEFAULT_COST_MODEL.net_rr(100.0, 97.5, 108.0, "long")
    assert expected == net_rr(100.0, 97.5, 108.0, "bull")
    assert expected == CostModel(DEFAULT_COMMISSION_BPS,
                                 DEFAULT_SLIPPAGE_BPS,
                                 DEFAULT_SELL_TAX_BPS).net_rr(
                                     100.0, 97.5, 108.0, "long")
    assert expected[0] > expected[1] > 0


def test_cost_basis_is_one_declared_default():
    model = CostModel(DEFAULT_COMMISSION_BPS, 0.0, 0.0)
    # commission_bps is ONE round trip: half on each leg.
    assert model.cost(100.0, 102.5) == pytest.approx(
        100.0 * 0.0005 + 102.5 * 0.0005)
    for name, value in (
            ("commission_bps", DEFAULT_COMMISSION_BPS),
            ("slippage_bps", DEFAULT_SLIPPAGE_BPS),
            ("sell_tax_bps", DEFAULT_SELL_TAX_BPS)):
        assert getattr(DEFAULT_COST_MODEL, name) == value
        assert getattr(declared(name), "value") == value
        assert getattr(StrategyConfig(), name) == value
        assert getattr(SetupConfig(), name) == value


def test_risk_plan_net_rr_is_the_shared_formula(tmp_path):
    plan = build_risk_plan("long", entry=100.0, atr=2.0,
                           sweep={"level": 98.0, "extreme": 98.0},
                           structure=None, pools=[108.0])
    nearest = 108.0 - 0.1 * 2.0  # the repo fill-before-level offset
    gross, net = net_rr(100.0, plan.stop, nearest, "long")
    assert plan.blockers == ()
    assert plan.gross_rr == pytest.approx(gross)
    assert plan.net_rr == pytest.approx(net)
    assert plan.net_rr >= NET_RR_FLOOR
    assert plan.quantity == pytest.approx(
        100_000.0 * DEFAULT_RISK_PCT / abs(100.0 - plan.stop))


def test_risk_plan_blocks_on_the_declared_net_floor_not_a_private_number():
    plan = build_risk_plan("long", entry=100.0, atr=2.0,
                           sweep={"level": 98.0, "extreme": 98.0},
                           structure=None, pools=[100.9])
    assert plan.blockers == (NET_RR_BLOCKER,)
    assert NET_RR_BLOCKER.endswith(f"{NET_RR_FLOOR:g}")


def test_simulator_realized_r_uses_the_shared_cost_model():
    df = _ohlc([
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 99, 100),   # fill bar; neither stop nor target
        (110, 116, 109, 115),  # target touched
    ])
    cfg = StrategyConfig(min_grade=0, min_confluence=0, entry_delay=1,
                         max_bars=4, min_rr=0.0,
                         commission_bps=10.0, slippage_bps=2.0,
                         sell_tax_bps=0.0)
    r = simulate_cisd(df, _one_cisd(df), cfg, "test")
    assert len(r.trades) == 1
    t = r.trades[0]
    model = CostModel(10.0, 2.0, 0.0).for_filled_prices()
    assert t.r_multiple == pytest.approx(
        model.realized_r(t.entry, t.sl, t.exit, t.direction))
    assert t.features["entry_rr"] == pytest.approx(
        net_rr(t.entry, t.sl, t.planned_tp, t.direction)[0])


# --------------------------------------------------------------------------- #
# 2. one same-bar tie-break helper
# --------------------------------------------------------------------------- #
def test_same_bar_helper_declares_stop_first_both_vocabularies():
    assert same_bar_exit("long", high=116.0, low=94.0,
                         stop=95.0, target=115.0) == "stop"
    assert same_bar_exit("bull", high=116.0, low=94.0,
                         stop=95.0, target=115.0) == "stop"
    assert same_bar_exit("short", high=106.0, low=94.0,
                         stop=105.0, target=95.0) == "stop"
    assert same_bar_exit("long", high=101.0, low=99.0,
                         stop=95.0, target=115.0) is None
    with pytest.raises(ValueError):
        same_bar_exit("sideways", 1.0, 1.0, 1.0, 1.0)


def test_backtest_and_broker_take_the_same_stop_first_bar(tmp_path):
    # Both the backtest and the paper broker see one bar that touches the stop
    # and the target.  They must resolve it through the same declaration.
    df = _ohlc([
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 116, 94, 100),
        (100, 101, 99, 100),
    ])
    cfg = StrategyConfig(min_grade=0, min_confluence=0, entry_delay=1,
                         max_bars=3, min_rr=0.0)
    r = simulate_cisd(df, _one_cisd(df), cfg, "test")
    assert len(r.trades) == 1
    assert r.trades[0].reason == "sl"
    assert r.trades[0].exit == pytest.approx(95.0)

    broker = _broker(tmp_path)
    result = asyncio.run(broker.execute(_signal(), 100.0))
    assert result.accepted
    asyncio.run(broker.on_bar_closed("BTCUSDT", high=116.0, low=94.0,
                                     close=100.0))
    assert len(broker.trades) == 1
    assert broker.trades[0].exit_reason == "stop"


# --------------------------------------------------------------------------- #
# 3. one risk_pct default / register entry
# --------------------------------------------------------------------------- #
def test_risk_pct_is_one_declared_register_entry(tmp_path):
    settings = Settings(symbols=["BTCUSDT"], intervals=["15m"], mode="demo",
                        data_dir=tmp_path)
    assert declared("risk_pct").value == DEFAULT_RISK_PCT
    assert declared("risk_pct").label == "repo"
    assert StrategyConfig().risk_pct == DEFAULT_RISK_PCT
    assert SetupConfig().risk_pct == DEFAULT_RISK_PCT
    assert settings.risk_pct == DEFAULT_RISK_PCT
    with pytest.raises(KeyError):
        declared("risk_pct_typo")


# --------------------------------------------------------------------------- #
# 4. one floor per declared RR mode
# --------------------------------------------------------------------------- #
def test_one_rr_floor_per_declared_mode():
    assert rr_floor() == RR_MODE_FLOORS[DEFAULT_RR_MODE] == MIN_RR
    for mode, floor in RR_MODE_FLOORS.items():
        assert rr_floor(mode) == floor
    assert MIN_RR == NET_RR_FLOOR  # both declared gates read the same floor
    with pytest.raises(ValueError):
        rr_floor("floor_2_0_typo")


def test_every_rr_surface_reads_the_declared_floor():
    assert StrategyConfig().rr_floor() == MIN_RR
    assert StrategyConfig(rr_mode="floor_3_1").rr_floor() == 3.0
    assert StrategyConfig(min_rr=1.25).rr_floor() == 1.25
    assert SetupConfig().min_target_rr == MIN_RR
    assert _setup_args().min_target_rr == MIN_RR
    cli = _cli_args()
    assert cli.min_target_rr == MIN_RR
    assert cli.min_net_rr == NET_RR_FLOOR
    # No private 1.2 / 2.5 gate constants survive in the owned surfaces.
    assert MIN_RR not in {1.2, 2.5}
    with pytest.raises(ValueError):
        StrategyConfig(rr_mode="floor_2_0_typo")


def test_broker_uses_the_declared_rr_floor(tmp_path):
    below = _broker(tmp_path / "below")
    result = asyncio.run(below.execute(_signal(target=109.5), 100.0))
    assert not result.accepted
    assert "below floor" in result.reason

    above = _broker(tmp_path / "above")
    result = asyncio.run(above.execute(_signal(target=110.6), 100.0))
    assert result.accepted


# --------------------------------------------------------------------------- #
# 5. one min_1d_score value across config / setup / cli
# --------------------------------------------------------------------------- #
def test_bias_score_defaults_agree_across_all_parsers():
    lib = SetupConfig()
    setup_args = _setup_args()
    cli_args = _cli_args()
    for name, declared_value in (
            ("min_1d_score", setup_api.DEFAULT_MIN_1D_SCORE),
            ("min_4h_score", setup_api.DEFAULT_MIN_4H_SCORE),
            ("min_15m_score", setup_api.DEFAULT_MIN_15M_SCORE)):
        assert getattr(lib, name) == declared_value
        assert getattr(setup_args, name) == declared_value
        assert getattr(cli_args, name) == declared_value


# --------------------------------------------------------------------------- #
# 6. one trades/day value
# --------------------------------------------------------------------------- #
def test_trades_per_day_is_one_declared_value(tmp_path):
    settings = Settings(symbols=["BTCUSDT"], intervals=["15m"], mode="demo",
                        data_dir=tmp_path)
    assert declared("max_trades_per_day").value == DEFAULT_MAX_TRADES_PER_DAY
    assert StrategyConfig().max_trades_per_day == DEFAULT_MAX_TRADES_PER_DAY
    assert SetupConfig().max_trades_per_day == DEFAULT_MAX_TRADES_PER_DAY
    assert settings.max_trades_per_day == DEFAULT_MAX_TRADES_PER_DAY
    assert _setup_args().max_trades_per_day == DEFAULT_MAX_TRADES_PER_DAY
    assert _cli_args().max_trades_per_day == DEFAULT_MAX_TRADES_PER_DAY


# --------------------------------------------------------------------------- #
# register hygiene
# --------------------------------------------------------------------------- #
def test_accounting_register_entries_are_labeled_repo():
    for name, entry in ACCOUNTING_REGISTER.items():
        assert entry.name == name
        assert entry.label == "repo"
        assert entry.source
