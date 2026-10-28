import pandas as pd

from ict_trader.journal import journal_frame, process_summary
from ict_trader.strategy import Trade


def test_journal_has_planned_and_actual_fields():
    t = Trade(
        symbol="TEST", direction="bull",
        signal_time=pd.Timestamp("2026-01-01 09:30"),
        entry_time=pd.Timestamp("2026-01-01 09:45"),
        exit_time=pd.Timestamp("2026-01-01 10:30"),
        entry=100.5, exit=104, sl=99, tp=105,
        r_multiple=2.0, reason="tp", grade=4, confluence=3, bars=3,
        planned_entry=100.0, planned_sl=99.0, planned_tp=105.0,
        entry_deviation_r=0.33, quality=0.7,
    )
    frame = journal_frame([t], sample_class="observation")
    assert len(frame) == 1
    assert frame.iloc[0]["planned_entry"] == 100.0
    assert frame.iloc[0]["actual_entry"] == 100.5
    assert frame.iloc[0]["entry_deviation_r"] == 0.33
    assert process_summary(frame)["process_errors"] == 1
