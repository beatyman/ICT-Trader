from ict_trader.realtime.analysis.dealing_range import DealingRange, valid_ob

def test_ce_and_quarters():
    r = DealingRange(high=100, low=0)
    assert r.ce == 50
    assert r.q1 == 25
    assert r.q3 == 75
    assert r.zone(80) == "deep_premium"
    assert r.zone(60) == "premium"
    assert r.zone(40) == "discount"
    assert r.zone(10) == "deep_discount"

def test_ote_band():
    r = DealingRange(high=100, low=0)
    lo, hi = r.ote("long")
    assert 20 < lo < hi < 40

def test_ob_requires_all_three_confirmations():
    assert valid_ob("long", engulf=True, fvg=True, mss=True)[0]
    ok, reasons = valid_ob("long", engulf=True, fvg=False, mss=True)
    assert not ok and "no_fvg" in reasons
