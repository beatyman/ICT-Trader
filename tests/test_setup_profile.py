from ict_trader.setup import SetupConfig, apply_quality_profile


def test_high_value_profile_is_stricter():
    cfg = apply_quality_profile(SetupConfig(quality_profile="high_value"))
    assert cfg.min_grade == 4
    assert cfg.min_confluence == 3
    assert cfg.min_disp_body_ratio == 0.65
    assert cfg.min_target_rr == 2.0


def test_standard_profile_does_not_override_explicit_values():
    cfg = apply_quality_profile(SetupConfig(quality_profile="standard", min_grade=2))
    assert cfg.min_grade == 2
