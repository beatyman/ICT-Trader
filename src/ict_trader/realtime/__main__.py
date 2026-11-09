"""`python -m ict_trader.realtime` entry point."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import uvicorn

from .api import create_app
from .config import ROOT, Settings, load_env_file
from .engine import RealtimeEngine


def _parse() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="ICT-Trader realtime paper trading stack")
    ap.add_argument("--mode", choices=["demo", "live"], default=None,
                    help="demo: accelerated synthetic tape; live: Binance WS")
    ap.add_argument("--symbols", default=None, help="comma-separated symbols")
    ap.add_argument("--intervals", default=None, help="comma-separated intervals")
    ap.add_argument("--strategy-interval", default=None)
    ap.add_argument("--stack", default=None, help="ICT stack, e.g. 4h_15m or 1d_4h_15m")
    ap.add_argument("--bootstrap-bars", type=int, default=None)
    ap.add_argument("--speed", type=float, default=None, help="demo seconds per bar")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--headless", action="store_true", help="run the engine without the web UI")
    ap.add_argument("--reset-wal", action="store_true", help="delete the local WAL before starting")
    ap.add_argument("--log-level", default=None)
    return ap.parse_args()


def _split(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


async def _run_headless(engine: RealtimeEngine) -> None:
    await engine.start()
    print("engine ready; Ctrl+C to stop")
    try:
        await asyncio.Event().wait()
    finally:
        await engine.stop()


def main() -> None:
    args = _parse()
    load_env_file(ROOT / ".env")
    if args.reset_wal:
        wal = Settings.from_env().wal_path
        if wal.exists():
            wal.unlink()
    overrides = {
        "mode": args.mode,
        "symbols": _split(args.symbols),
        "intervals": _split(args.intervals),
        "strategy_interval": args.strategy_interval,
        "stack": args.stack,
        "bootstrap_bars": args.bootstrap_bars,
        "demo_speed": args.speed,
        "dashboard_host": args.host,
        "dashboard_port": args.port,
        "log_level": args.log_level.upper() if args.log_level else None,
    }
    settings = Settings.from_env(**overrides)
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("websockets", "httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    print()
    print("  ICT // REALTIME PAPER TERMINAL")
    print(f"  mode      : {settings.mode.upper()}  ({'SIMULATED TAPE' if settings.mode == 'demo' else settings.ws_base_url})")
    print(f"  symbols   : {', '.join(settings.symbols)}")
    print(f"  intervals : {', '.join(settings.intervals)}  (strategy {settings.strategy_interval})")
    print(f"  stack     : {settings.stack}   risk {settings.risk_pct:.2%}/trade")
    print(f"  redis     : {settings.redis_url}")
    print(f"  clickhouse: http://{settings.ch_host}:{settings.ch_port}  db={settings.ch_database}")
    print(f"  WAL       : {settings.wal_path}")
    print(f"  engine    : headless" if args.headless else f"  dashboard : http://{settings.dashboard_host}:{settings.dashboard_port}")
    print()

    if args.headless:
        try:
            asyncio.run(_run_headless(RealtimeEngine(settings)))
        except KeyboardInterrupt:
            pass
        return

    engine = RealtimeEngine(settings)
    app = create_app(engine)
    try:
        uvicorn.run(
            app,
            host=settings.dashboard_host,
            port=settings.dashboard_port,
            log_level=settings.log_level.lower(),
            access_log=False,
        )
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
