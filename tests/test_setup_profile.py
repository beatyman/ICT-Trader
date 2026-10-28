from ict_trader.setup import SetupConfig, apply_quality_profile, run_one


def test_high_value_profile_is_stricter():
    cfg = apply_quality_profile(SetupConfig(quality_profile="high_value"))
    assert cfg.min_grade == 4
    assert cfg.min_confluence == 3
    assert cfg.min_disp_body_ratio == 0.65
    assert cfg.min_target_rr == 2.0


def test_standard_profile_does_not_override_explicit_values():
    cfg = apply_quality_profile(SetupConfig(quality_profile="standard", min_grade=2))
    assert cfg.min_grade == 2


def test_alternate_timeframe_stacks_run():
    for stack in ("1d_15m", "4h_15m"):
        trades, metrics = run_one(
            "examples/15m.csv",
            SetupConfig(market="cn", stack=stack, t_plus_1=True),
            daily_path="examples/daily.csv",
        )
        assert "trades" in metrics
        assert metrics["trades"] == len(trades)


def test_fvg_origin_filter_only_keeps_fvg_origin():
    trades, _ = run_one(
        "examples/15m.csv",
        SetupConfig(market="cn", stack="1d_1h_15m", t_plus_1=True,
                    origin_filter="fvg", require_sweep=True),
        daily_path="examples/daily.csv",
    )
    assert all(t.features.get("origin") == "fvg" for t in trades)
