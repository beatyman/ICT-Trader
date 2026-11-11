from ict_trader.realtime.analysis.pda_confluence import (
    annotate_confluence, body_close_invalidated, overlap_ratio,
)

def test_fvg_inside_ob():
    fvg = {"direction": "bullish", "top": 105, "bottom": 100}
    ob = {"direction": "bullish", "top": 106, "bottom": 99}
    assert overlap_ratio(fvg, ob) == 1.0
    out = annotate_confluence([fvg], [ob])[0]
    assert out["pda_confluence"] == "fvg_inside_ob"

def test_direction_mismatch_is_not_confluence():
    fvg = {"direction": "bullish", "top": 105, "bottom": 100}
    ob = {"direction": "bearish", "top": 106, "bottom": 99}
    assert annotate_confluence([fvg], [ob])[0]["ob_overlap"] == 0.0

def test_body_close_invalidates_bullish_fvg():
    fvg = {"direction": "bullish", "top": 105, "bottom": 100}
    assert body_close_invalidated(fvg, {"close": 99.5}) == "INVERTED"
    assert body_close_invalidated(fvg, {"close": 100.5}) is None
