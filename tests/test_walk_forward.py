from datetime import timedelta
from types import SimpleNamespace

import pandas as pd

from ict_trader.walk_forward import purged_walk_forward


def test_purged_walk_forward_no_overlap():
    start = pd.Timestamp("2026-01-01")
    trades = []
    for i in range(20):
        entry = start + timedelta(days=i * 3)
        trades.append(SimpleNamespace(
            entry_time=entry, exit_time=entry + timedelta(days=1),
            r_multiple=1.0 if i % 2 == 0 else -1.0, bars=4, direction="bull",
        ))
    folds = purged_walk_forward(trades, folds=4, embargo_days=2)
    assert len(folds) == 4
    for f in folds:
        if f["test"]["trades"]:
            test_start = pd.Timestamp(f["test_start"])
            for t in trades:
                if test_start <= t.entry_time < pd.Timestamp(f["test_end"]):
                    assert t.exit_time > test_start

