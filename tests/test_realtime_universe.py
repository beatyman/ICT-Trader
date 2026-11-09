from ict_trader.realtime.universe import _eligible


def test_volume_universe_filters_stablecoins_and_leverage():
    assert _eligible("BTCUSDT")
    assert not _eligible("USDCUSDT")
    assert not _eligible("BTCUPUSDT")
    assert not _eligible("RLUSDUSDT")
    assert not _eligible("ETHBTC")
