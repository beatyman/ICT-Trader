from ict_trader.realtime.analysis.auxiliary import build_auxiliary_context


def test_resonance_counts_matching_factors():
    ctx = build_auxiliary_context(
        "long", htf_bias="bullish", chan_bias="bullish",
        ichimoku_bias="bearish", wyckoff_bias="bullish",
    )
    assert ctx.resonance_score == 3
    assert ctx.breakdown["matched"] == ["htf", "chan", "wyckoff"]
    assert ctx.breakdown["max"] == 4
    assert ctx.to_public()["resonance_max"] == 4


def test_resonance_does_not_count_neutral():
    ctx = build_auxiliary_context("short", htf_bias="neutral",
                                  chan_bias="bearish", ichimoku_bias="neutral",
                                  wyckoff_bias="bullish")
    assert ctx.resonance_score == 1
    assert ctx.direction == "bearish"
