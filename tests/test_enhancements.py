import numpy as np
import pandas as pd

from ict_trader.concepts import ICTConfig, analyze
from ict_trader.enhancements import detect_htf_cisds, detect_session_levels, detect_smt


def sample_df(n=180):
    rng = np.random.default_rng(11)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    open_ = close + rng.normal(0, 0.3, n)
    high = np.maximum(open_, close) + rng.uniform(0.1, 0.7, n)
    low = np.minimum(open_, close) - rng.uniform(0.1, 0.7, n)
    dates = pd.date_range("2026-01-01 09:30", periods=n, freq="15min")
    return pd.DataFrame({
        "date": dates, "open": open_, "high": high, "low": low,
        "close": close, "volume": rng.integers(500, 2000, n),
    })


def test_session_levels():
    levels = detect_session_levels(sample_df())
    assert levels
    assert {x.kind for x in levels}.issubset({"H", "L"})


def test_htf_cisd():
    out = detect_htf_cisds(sample_df(), ("1h", "4h"))
    assert isinstance(out, list)


def test_smt_no_crash():
    df = sample_df()
    out = detect_smt(df, df.copy())
    assert isinstance(out, list)


def test_analyze_enhanced():
    df = sample_df()
    res = analyze(df, ICTConfig(swing_len=3, plot_bars=80, cisd_min_grade=0,
                                cisd_require_origin=False, cisd_max=2,
                                max_htf=1, show_sessions=True, show_smt=True),
                 smt_df=df.copy())
    assert hasattr(res, "htf_cisds")
    assert hasattr(res, "sessions")
    assert hasattr(res, "smts")



def test_session_levels_accept_timezone_aware_dates():
    import pandas as pd
    from datetime import datetime, timezone, timedelta
    from ict_trader.enhancements import detect_session_levels

    start = datetime(2026, 11, 9, tzinfo=timezone.utc)
    rows = []
    for i in range(96 * 3):
        px = 100 + i * 0.01
        rows.append({"date": start + timedelta(minutes=15 * i),
                     "open": px, "high": px + 0.2, "low": px - 0.2,
                     "close": px, "volume": 1.0})
    levels = detect_session_levels(pd.DataFrame(rows))
    assert levels is not None
