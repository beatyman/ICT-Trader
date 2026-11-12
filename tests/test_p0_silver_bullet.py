"""P0-12 - one Silver Bullet clock.

``CODE_MAPPING`` P0-12 names three Silver Bullet implementations that give
three answers: ``context.py`` (three windows), ``rules.py`` (``10:00-11:00``
only, with 11:00 inside), and ``concepts.py`` (a ``Europe/London`` /
``Asia/Tokyo`` annotation table).  The fix is one canonical table in
``context.py``: ``rules.py`` delegates to its predicate and ``concepts.py``
derives its session annotation from the same windows.

These tests pin:

* the canonical table is the corpus modal set (``03:00-04:00``,
  ``10:00-11:00``, ``14:00-15:00`` America/New_York, 60 minutes each,
  half-open ``[start, end)``);
* the three live predicates - ``context.in_session``,
  ``rules.in_silver_bullet`` and ``setup_rules.in_window(SB)`` - agree on every
  minute of two consecutive days including a US DST transition, and on the
  explicit ``10:00``/``11:00`` boundary;
* ``concepts.detect_killzones`` keeps no timezone table of its own: its spans
  equal the canonical session windows, and the old Tokyo/London windows are
  gone.
"""
from __future__ import annotations

from datetime import time, timedelta

import pandas as pd

from ict_trader import rules
from ict_trader.concepts import ICTConfig, detect_killzones
from ict_trader.context import (SILVER_BULLET_TZ, SILVER_BULLET_WINDOWS,
                                in_session, in_session_at, in_silver_bullet,
                                session_windows)
from ict_trader.setup_rules import in_window as rule_in_window
from ict_trader.setup_rules import rule_by_id

NY = "America/New_York"

#: The rule registry's Silver Bullet playbook: the third live predicate.
SB_RULE = rule_by_id("SB")


def _ny(*stamps: str) -> list[pd.Timestamp]:
    return [pd.Timestamp(stamp, tz=NY) for stamp in stamps]


def _frame(stamps: list[pd.Timestamp]) -> pd.DataFrame:
    return pd.DataFrame({
        "date": stamps,
        "open": [100.0] * len(stamps), "high": [101.0] * len(stamps),
        "low": [99.0] * len(stamps), "close": [100.0] * len(stamps),
    })


def _three_verdicts(df: pd.DataFrame, stamps: list[pd.Timestamp]) -> list[tuple[bool, bool, bool]]:
    """``context`` / ``rules`` / ``setup_rules`` verdicts for each bar."""
    out = []
    for i, stamp in enumerate(stamps):
        out.append((
            in_session(df, i, "crypto", "silver_bullet"),
            rules.in_silver_bullet(stamp),
            rule_in_window(stamp, SB_RULE),
        ))
    return out


def test_the_canonical_table_is_the_modal_three_window_set():
    assert SILVER_BULLET_TZ == NY
    assert SILVER_BULLET_WINDOWS == (
        (time(3, 0), time(4, 0)),
        (time(10, 0), time(11, 0)),
        (time(14, 0), time(15, 0)),
    )
    for start, end in SILVER_BULLET_WINDOWS:
        assert timedelta(hours=end.hour, minutes=end.minute) - \
               timedelta(hours=start.hour, minutes=start.minute) == timedelta(hours=1)

    zone, windows = session_windows("silver_bullet")
    assert zone == NY
    assert windows == SILVER_BULLET_WINDOWS
    # The registry's SB playbook is the third live call site; its table must
    # stay identical to the canonical one (it is pinned by the parity test
    # below as well).
    assert SB_RULE.windows_et == SILVER_BULLET_WINDOWS


def test_all_three_call_sites_agree_on_1000_to_1100_and_11_is_outside():
    stamps = _ny(
        "2026-07-15 09:59", "2026-07-15 10:00", "2026-07-15 10:30",
        "2026-07-15 10:59", "2026-07-15 11:00", "2026-07-15 11:01",
    )
    df = _frame(stamps)
    expected = [False, True, True, True, False, False]

    verdicts = _three_verdicts(df, stamps)
    for i, (ctx, rl, setup) in enumerate(verdicts):
        assert ctx == rl == setup == expected[i], (
            f"{stamps[i]} -> context={ctx} rules={rl} setup_rules={setup}; "
            f"expected {expected[i]} (P0-12)"
        )


def test_all_three_call_sites_agree_on_the_other_two_windows():
    stamps = _ny(
        "2026-07-15 02:59", "2026-07-15 03:00", "2026-07-15 03:30",
        "2026-07-15 03:59", "2026-07-15 04:00",
        "2026-07-15 13:59", "2026-07-15 14:00", "2026-07-15 14:30",
        "2026-07-15 14:59", "2026-07-15 15:00",
        "2026-07-15 09:00",
    )
    df = _frame(stamps)
    expected = [
        False, True, True, True, False,
        False, True, True, True, False,
        False,
    ]

    verdicts = _three_verdicts(df, stamps)
    for i, (ctx, rl, setup) in enumerate(verdicts):
        assert ctx == rl == setup == expected[i], (
            f"{stamps[i]} -> context={ctx} rules={rl} setup_rules={setup}; "
            f"expected {expected[i]}; old rules.py admitted only 10:00-11:00"
        )


def test_all_three_call_sites_agree_minute_by_minute_across_a_dst_transition():
    # 2026-03-08 is the US spring-forward day (02:00 -> 03:00 New York).
    stamps = list(pd.date_range("2026-03-08 00:00", periods=2 * 24 * 60,
                                freq="1min", tz="UTC"))
    df = _frame(stamps)
    mismatches = []
    verdicts = []
    for i, stamp in enumerate(stamps):
        ctx = in_session(df, i, "crypto", "silver_bullet")
        rl = rules.in_silver_bullet(stamp)
        setup = rule_in_window(stamp, SB_RULE)
        if not (ctx == rl == setup):
            mismatches.append((stamp, ctx, rl, setup))
        # The table itself is the oracle: half-open [start, end) in New York.
        local = stamp.tz_convert(NY).time()
        expected = any(start <= local < end for start, end in SILVER_BULLET_WINDOWS)
        assert ctx == expected, f"{stamp}: context={ctx}, table={expected}"
        verdicts.append(ctx)

    assert not mismatches, f"call sites disagree: {mismatches[:5]}"
    # Two calendar days x three 60-minute windows, even across spring-forward.
    assert sum(verdicts) == 2 * 3 * 60


def test_rules_preserves_its_naive_timestamp_api_and_fails_closed():
    # Naive stamps are read in input_tz, exactly as the old signature promised.
    assert rules.in_silver_bullet(pd.Timestamp("2026-07-15 10:30"),
                                  input_tz="America/New_York") is True
    assert rules.in_silver_bullet(pd.Timestamp("2026-07-15 11:00"),
                                  input_tz="America/New_York") is False
    assert rules.in_silver_bullet(pd.Timestamp("2026-07-15 14:30"),
                                  input_tz="UTC") is True   # 10:30 NY -> inside
    assert rules.in_silver_bullet(pd.Timestamp("2026-07-15 15:30"),
                                  input_tz="UTC") is False  # 11:30 NY -> outside
    assert rules.in_silver_bullet(None) is False
    assert rules.in_silver_bullet("not-a-timestamp") is False
    # The canonical predicate is the same object graph: context and rules agree.
    for stamp in _ny("2026-07-15 03:30", "2026-07-15 10:30",
                     "2026-07-15 11:00", "2026-07-15 14:30"):
        assert in_silver_bullet(stamp) is rules.in_silver_bullet(stamp)


def test_concepts_killzones_are_derived_from_the_canonical_table():
    # Two days of New York-local minutes, so each alias's expected span is the
    # canonical membership set for that gate.
    stamps = list(pd.date_range("2026-07-15 00:00", periods=2 * 24 * 60,
                                freq="1min", tz=NY))
    df = _frame(stamps)
    zones = detect_killzones(df, ICTConfig(show_killzones=True))

    aliases = {
        "New York": "ny_am",
        "London Open": "london",
        "London Close": "london_close",
        "Asian": "asian",
    }
    assert set(zones) == set(aliases)
    for name, key in aliases.items():
        expected = {i for i, stamp in enumerate(stamps)
                    if in_session_at(stamp, key)}
        actual = {i for first, last in zones[name] for i in range(first, last + 1)}
        assert actual == expected, f"{name} spans diverge from canonical {key!r}"

    # The removed Europe/London table put "Asian" at 10:00-14:00 Tokyo, i.e.
    # 00:30 New York; the canonical table ends at 00:00 and must reject it.
    assert in_session_at(pd.Timestamp("2026-07-15 00:30", tz=NY), "asian") is False
    # The removed table put "New York" at 07:00-09:00; the canonical NY AM
    # killzone runs to 10:00, so 09:30 is inside.
    assert in_session_at(pd.Timestamp("2026-07-15 09:30", tz=NY), "ny_am") is True
