import pandas as pd

from ict_trader.quality import measure_displacement, score_setup


def test_displacement_and_score():
    df = pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=30, freq="15min"),
        "open": [100.0] * 30, "high": [101.0] * 30, "low": [99.0] * 30,
        "close": [100.0] * 29 + [102.0], "volume": [1000] * 29 + [2000],
    })
    d = measure_displacement(df, 29)
    assert d.body_ratio > 0.5
    assert d.body_multiple > 1.3
    assert d.volume_ratio > 1.0
    assert 0 <= d.score <= 1

    s, parts = score_setup(d1_score=3, h4_score=2, cisd_grade=6, sweep_strength=3,
                           pda=True, displacement=d.score, volume=True, session=True)
    assert 0 <= s <= 1
    assert set(parts) == {"htf", "mtf", "cisd", "sweep", "pda", "displacement", "volume", "session"}

