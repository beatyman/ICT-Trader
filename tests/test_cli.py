from pathlib import Path

from ict_trader.cli import main


def test_plan_is_no_trade_for_demo_example(tmp_path: Path):
    out = tmp_path / "plan.json"
    main([
        "plan",
        "--csv", "examples/15m.csv",
        "--daily-csv", "examples/daily.csv",
        "--market", "cn",
        "--symbol", "DEMO",
        "--json-out", str(out),
    ])
    assert out.exists()
    assert '"decision": "NO_TRADE"' in out.read_text(encoding="utf-8")
