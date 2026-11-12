"""Environment-driven configuration for the realtime paper-trading stack.

The defaults are tuned for a 4 GB local Docker Desktop / t4g.medium class box:
six Binance Spot symbols, one strategy interval (15m), Redis + ClickHouse in
containers, and roughly 1.2 GB for the Python process.

All values can be overridden from `.env` or the process environment.  The
dashboard never receives secrets (API keys are reported as configured / not
configured only).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..strategy import (
    DEFAULT_COMMISSION_BPS,
    DEFAULT_FIXED_R,
    DEFAULT_MAX_TRADES_PER_DAY,
    DEFAULT_RISK_PCT,
    DEFAULT_RR_MODE,
    DEFAULT_SLIPPAGE_BPS,
    RR_MODE_FLOORS,
)

_PACKAGE_ROOT = Path(__file__).resolve().parents[3]
# In a source checkout the package root is the repo; in an installed Docker
# image it lives under site-packages, while /app (or the caller's cwd) is the
# writable project root. Prefer a directory that actually has pyproject.toml.
ROOT = _PACKAGE_ROOT if (_PACKAGE_ROOT / "pyproject.toml").exists() else Path.cwd()
DEFAULT_DATA_DIR = ROOT / "data" / "realtime"

SPOT_WS = "wss://data-stream.binance.vision/stream"
SPOT_REST = "https://data-api.binance.vision"
FUTURES_WS = "wss://fstream.binance.com/stream"
FUTURES_REST = "https://fapi.binance.com"

VALID_INTERVALS = {
    "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d",
}


def load_env_file(path: Path | None = None) -> None:
    """Minimal .env loader (does not override real process env)."""
    env_path = path or (ROOT / ".env")
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value and value[0] not in {'"', "'"}:
            # Support `KEY=value  # comment` in the shipped example.
            value = value.split(" #", 1)[0].strip()
        value = value.strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value is not None and value.strip() else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env_str(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env_str(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return list(default)
    return [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]


def validate_ict_settings(s: "Settings") -> None:
    if s.ict_setup_enabled and not s.ict_setup_shadow:
        if s.ict_entry_policy == "legacy":
            raise RuntimeError(
                "ICT_SETUP_ENABLED=true requires a non-legacy entry policy"
            )
    if s.ict_structure_exits and not s.ict_setup_enabled:
        raise RuntimeError("ICT_STRUCTURE_EXITS requires ICT_SETUP_ENABLED")


@dataclass(slots=True)
class Settings:
    # ---- runtime ---------------------------------------------------------
    mode: str = "live"                    # live | demo (explicit local-only)
    market: str = "spot"                  # spot | futures
    symbols: list[str] = field(default_factory=lambda: [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
    ])
    symbol_mode: str = "static"          # static | top10
    top_symbols_count: int = 10
    intervals: list[str] = field(default_factory=lambda: ["15m", "1h", "4h", "1d"])
    strategy_interval: str = "15m"
    ws_base_url: str = SPOT_WS
    rest_base_url: str = SPOT_REST
    max_streams_per_conn: int = 100
    ping_interval: float = 20.0
    ping_timeout: float = 20.0
    reconnect_max_seconds: float = 30.0

    # ---- data bootstrap / demo ------------------------------------------
    bootstrap_bars: int = 8000            # strategy interval
    bootstrap_bars_fast: int = 320        # non-strategy intervals
    bootstrap_concurrency: int = 3
    demo_speed: float = 15.0              # logical bars are emitted every N seconds
    demo_subticks: int = 6
    demo_seed_source: str = "binance"     # binance | synthetic

    # ---- Redis -----------------------------------------------------------
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_socket_timeout: float = 5.0
    redis_closed_keep: int = 1000        # hot window; 50x3 budget guard
    redis_latest_ttl: int = 172800        # 48h cache (not the system of record)
    redis_closed_ttl: int = 604800        # 7d hot cache

    # ---- ClickHouse ------------------------------------------------------
    ch_host: str = "127.0.0.1"
    ch_port: int = 8123
    ch_user: str = "default"
    ch_password: str = ""
    ch_database: str = "ict"
    ch_secure: bool = False
    ch_batch_size: int = 500
    ch_flush_seconds: float = 2.0
    ch_retry_seconds: float = 5.0
    ch_retention_days: int = 365

    # ---- backfill / gap --------------------------------------------------
    backfill_max_retries: int = 6
    backfill_rate_per_sec: float = 4.0
    backfill_outer_retry_seconds: float = 60.0
    backfill_max_bars: int = 20000

    # ---- strategy / paper broker ----------------------------------------
    stack: str = "4h_15m"
    strategy_eval_window: int = 0      # 0 => auto (1800 for 4H stacks, 8000 for 1D stacks)
    quality_profile: str = "standard"
    require_sweep: bool = True
    allow_short: bool = False
    min_grade: int = 3
    min_confluence: int = 2
    use_key_targets: bool = False
    target_mode: str = "fixed"
    rr: float = DEFAULT_FIXED_R
    #: Declared RR gate mode (RE:RM-15 / V28); the broker reads its floor.
    rr_mode: str = DEFAULT_RR_MODE
    setup_min_confidence: float = 0.65
    max_bars: int = 40
    commission_bps: float = DEFAULT_COMMISSION_BPS  # round trip (5 bps/side)
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS      # per fill
    initial_equity: float = 100_000.0
    risk_pct: float = DEFAULT_RISK_PCT      # 0.5% equity risked per trade
    max_position_pct: float = 0.30      # spot notional cap per position
    max_open_positions: int = 5
    #: Declared daily budget (fills per calendar date); one value everywhere.
    max_trades_per_day: int = DEFAULT_MAX_TRADES_PER_DAY
    max_daily_loss_pct: float = 0.03
    paper_trade: bool = True             # hard safety: execution is simulated

    # ---- dashboard / observability --------------------------------------
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8080
    broadcast_seconds: float = 1.0
    log_level: str = "INFO"
    data_dir: Path = DEFAULT_DATA_DIR

    # ---- optional AI layer (intentionally unused by default) ------------
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    deepseek_timeout: float = 90.0
    deepseek_max_tokens: int = 8000
    deepseek_effort: str = "low"
    ai_auto: bool = False
    ai_auto_min_confidence: float = 0.65
    ai_auto_cooldown_seconds: float = 300.0
    ai_scan_seconds: float = 300.0
    analysis_scan_seconds: float = 300.0
    ict_setup_enabled: bool = False
    require_primary_setup: bool = True
    ict_setup_shadow: bool = True
    ict_entry_policy: str = "legacy"
    ict_structure_exits: bool = False
    setup_engine_ids: str = "ict,ichimoku,wyckoff,chan"
    setup_primary_engine: str = "ict"
    ai_market_scan: bool = False
    ai_execution_enabled: bool = False
    ai_market_scan_seconds: float = 300.0
    ai_market_scan_max_symbols: int = 10

    def __post_init__(self) -> None:
        validate_ict_settings(self)
        if self.symbol_mode not in {"static", "top10"}:
            raise ValueError("symbol_mode must be 'static' or 'top10'")
        self.top_symbols_count = max(1, min(int(self.top_symbols_count), 20))
        self.symbols = [s.upper() for s in self.symbols]
        self.intervals = list(dict.fromkeys(self.intervals))
        unknown = [i for i in self.intervals if i not in VALID_INTERVALS]
        if unknown:
            raise ValueError(f"unsupported intervals: {unknown}")
        if self.strategy_interval not in self.intervals:
            self.intervals.append(self.strategy_interval)
        if self.mode not in {"demo", "live"}:
            raise ValueError("mode must be 'demo' or 'live'")
        if self.market not in {"spot", "futures"}:
            raise ValueError("market must be 'spot' or 'futures'")
        if not self.paper_trade:
            raise ValueError(
                "live order routing is intentionally not implemented; this build is paper-only"
            )
        if self.deepseek_effort not in {"low", "high", "max"}:
            raise ValueError("DEEPSEEK_EFFORT must be low, high or max")
        if self.rr_mode not in RR_MODE_FLOORS:
            raise ValueError(
                f"rr_mode must be one of {tuple(RR_MODE_FLOORS)}")
        self.deepseek_max_tokens = max(256, int(self.deepseek_max_tokens))
        self.deepseek_timeout = max(10.0, float(self.deepseek_timeout))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.market == "futures":
            # The data layer only differs by URL and field mapping; kline fields
            # are identical for USD-M futures.
            self.ws_base_url = _env_str("BINANCE_WS_URL", FUTURES_WS)
            self.rest_base_url = _env_str("BINANCE_REST_URL", FUTURES_REST)

    # ------------------------------------------------------------------
    @property
    def wal_path(self) -> Path:
        return self.data_dir / "wal.sqlite3"

    @property
    def demo_state_path(self) -> Path:
        return self.data_dir / "demo_state.json"

    @property
    def streams(self) -> list[tuple[str, str]]:
        return [(s, i) for s in self.symbols for i in self.intervals]

    def stream_groups(self) -> list[list[tuple[str, str]]]:
        """Group by interval, then chunk symbols into <= max_streams_per_conn.

        Grouping by interval keeps a slow/volatile interval from starving the
        others and makes reconnect blast-radius predictable.  The hard cap is
        the Binance "100-150 streams per connection" latency guardrail.
        """
        groups: list[list[tuple[str, str]]] = []
        cap = max(10, min(int(self.max_streams_per_conn), 140))
        for interval in self.intervals:
            chunk: list[tuple[str, str]] = []
            for symbol in self.symbols:
                chunk.append((symbol, interval))
                if len(chunk) >= cap:
                    groups.append(chunk)
                    chunk = []
            if chunk:
                groups.append(chunk)
        return groups

    def bootstrap_limit(self, interval: str) -> int:
        return self.bootstrap_bars if interval == self.strategy_interval else self.bootstrap_bars_fast

    def public_dict(self) -> dict[str, Any]:
        """Dashboard-safe view. Never include Redis URLs with credentials."""
        return {
            "mode": self.mode,
            "market": self.market.upper(),
            "venue": "BINANCE SPOT" if self.market == "spot" else "BINANCE USD-M",
            "symbols": self.symbols,
            "symbol_mode": self.symbol_mode,
            "top_symbols_count": self.top_symbols_count,
            "intervals": self.intervals,
            "strategy_interval": self.strategy_interval,
            "stack": self.stack,
            "strategy": "ICT frozen multi-timeframe setup",
            "paper_trade": self.paper_trade,
            "bootstrap_bars": self.bootstrap_bars,
            "risk_pct": self.risk_pct,
            "max_trades_per_day": self.max_trades_per_day,
            "rr_mode": self.rr_mode,
            "setup_min_confidence": self.setup_min_confidence,
            "analysis_scan_seconds": self.analysis_scan_seconds,
            "ict_setup_enabled": self.ict_setup_enabled,
            "ict_setup_shadow": self.ict_setup_shadow,
            "ict_entry_policy": self.ict_entry_policy,
            "ict_structure_exits": self.ict_structure_exits,
            "setup_engine_ids": self.setup_engine_ids,
            "setup_primary_engine": self.setup_primary_engine,
            "ai_market_scan": self.ai_market_scan,
            "ai_execution_enabled": self.ai_execution_enabled,
            "ai_market_scan_seconds": self.ai_market_scan_seconds,
            "max_position_pct": self.max_position_pct,
            "max_open_positions": self.max_open_positions,
            "commission_bps": self.commission_bps,
            "slippage_bps": self.slippage_bps,
            "ai": {
                "provider": "deepseek",
                "configured": bool(self.deepseek_api_key),
                "enabled": bool(self.deepseek_api_key),
                "model": self.deepseek_model,
                "model_name": "DeepSeek-V4.1-Flash",
                "mode": "on-demand risk review (never executes orders)",
            },
        }

    @classmethod
    def from_env(cls, **overrides: Any) -> "Settings":
        market = _env_str("BINANCE_MARKET", "spot").lower()
        ws_default = FUTURES_WS if market == "futures" else SPOT_WS
        rest_default = FUTURES_REST if market == "futures" else SPOT_REST
        data_dir = Path(_env_str("REALTIME_DATA_DIR", str(DEFAULT_DATA_DIR)))
        cfg = cls(
            mode=_env_str("REALTIME_MODE", "live").lower(),
            market=market,
            symbols=_env_list("SYMBOLS", ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"]),
            symbol_mode=_env_str("SYMBOL_MODE", "static").lower(),
            top_symbols_count=_env_int("TOP_SYMBOLS_COUNT", 10),
            intervals=_env_list("INTERVALS", ["15m", "1h", "4h", "1d"]),
            strategy_interval=_env_str("STRATEGY_INTERVAL", "15m"),
            ws_base_url=_env_str("BINANCE_WS_URL", ws_default),
            rest_base_url=_env_str("BINANCE_REST_URL", rest_default),
            max_streams_per_conn=_env_int("MAX_STREAMS_PER_CONN", 100),
            ping_interval=_env_float("WS_PING_INTERVAL", 20.0),
            ping_timeout=_env_float("WS_PING_TIMEOUT", 20.0),
            reconnect_max_seconds=_env_float("WS_RECONNECT_MAX", 30.0),
            bootstrap_bars=_env_int("BOOTSTRAP_BARS", 8000),
            bootstrap_bars_fast=_env_int("BOOTSTRAP_BARS_FAST", 320),
            bootstrap_concurrency=_env_int("BOOTSTRAP_CONCURRENCY", 3),
            demo_speed=_env_float("DEMO_SPEED", 15.0),
            demo_subticks=_env_int("DEMO_SUBTICKS", 6),
            demo_seed_source=_env_str("DEMO_SEED_SOURCE", "binance").lower(),
            redis_url=_env_str("REDIS_URL", "redis://127.0.0.1:6379/0"),
            redis_socket_timeout=_env_float("REDIS_SOCKET_TIMEOUT", 5.0),
            redis_closed_keep=_env_int("REDIS_CLOSED_KEEP", 1000),
            redis_latest_ttl=_env_int("REDIS_LATEST_TTL", 172800),
            redis_closed_ttl=_env_int("REDIS_CLOSED_TTL", 604800),
            ch_host=_env_str("CLICKHOUSE_HOST", "127.0.0.1"),
            ch_port=_env_int("CLICKHOUSE_PORT", 8123),
            ch_user=_env_str("CLICKHOUSE_USER", "default"),
            ch_password=_env_str("CLICKHOUSE_PASSWORD", ""),
            ch_database=_env_str("CLICKHOUSE_DATABASE", "ict"),
            ch_secure=_env_bool("CLICKHOUSE_SECURE", False),
            ch_batch_size=_env_int("CLICKHOUSE_BATCH_SIZE", 500),
            ch_flush_seconds=_env_float("CLICKHOUSE_FLUSH_SECONDS", 2.0),
            ch_retry_seconds=_env_float("CLICKHOUSE_RETRY_SECONDS", 5.0),
            ch_retention_days=_env_int("CLICKHOUSE_RETENTION_DAYS", 365),
            backfill_max_retries=_env_int("BACKFILL_MAX_RETRIES", 6),
            backfill_rate_per_sec=_env_float("BACKFILL_RATE_PER_SEC", 4.0),
            backfill_outer_retry_seconds=_env_float("BACKFILL_OUTER_RETRY_SECONDS", 60.0),
            backfill_max_bars=_env_int("BACKFILL_MAX_BARS", 20000),
            stack=_env_str("ICT_STACK", "4h_15m"),
            strategy_eval_window=_env_int("STRATEGY_EVAL_WINDOW", 0),
            quality_profile=_env_str("ICT_QUALITY_PROFILE", "standard"),
            require_sweep=_env_bool("ICT_REQUIRE_SWEEP", True),
            allow_short=_env_bool("ICT_ALLOW_SHORT", False),
            min_grade=_env_int("ICT_MIN_GRADE", 3),
            min_confluence=_env_int("ICT_MIN_CONFLUENCE", 2),
            use_key_targets=_env_bool("ICT_USE_KEY_TARGETS", False),
            target_mode=_env_str("ICT_TARGET_MODE", "fixed"),
            rr=_env_float("ICT_RR", DEFAULT_FIXED_R),
            rr_mode=_env_str("ICT_RR_MODE", DEFAULT_RR_MODE).lower(),
            setup_min_confidence=_env_float("SETUP_MIN_CONFIDENCE", 0.65),
            max_bars=_env_int("ICT_MAX_BARS", 40),
            commission_bps=_env_float("COMMISSION_BPS", DEFAULT_COMMISSION_BPS),
            slippage_bps=_env_float("SLIPPAGE_BPS", DEFAULT_SLIPPAGE_BPS),
            initial_equity=_env_float("INITIAL_EQUITY", 100_000.0),
            risk_pct=_env_float("RISK_PCT", DEFAULT_RISK_PCT),
            max_position_pct=_env_float("MAX_POSITION_PCT", 0.30),
            max_open_positions=_env_int("MAX_OPEN_POSITIONS", 5),
            max_trades_per_day=_env_int("MAX_TRADES_PER_DAY",
                                        DEFAULT_MAX_TRADES_PER_DAY),
            max_daily_loss_pct=_env_float("MAX_DAILY_LOSS_PCT", 0.03),
            paper_trade=_env_bool("PAPER_TRADE", True),
            dashboard_host=_env_str("DASHBOARD_HOST", "127.0.0.1"),
            dashboard_port=_env_int("DASHBOARD_PORT", 8080),
            broadcast_seconds=_env_float("BROADCAST_SECONDS", 1.0),
            log_level=_env_str("LOG_LEVEL", "INFO").upper(),
            data_dir=data_dir,
            deepseek_api_key=_env_str("DEEPSEEK_API_KEY", ""),
            deepseek_base_url=_env_str("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            deepseek_model=_env_str("DEEPSEEK_MODEL", "deepseek-flash"),
            deepseek_timeout=_env_float("DEEPSEEK_TIMEOUT", 90.0),
            deepseek_max_tokens=_env_int("DEEPSEEK_MAX_TOKENS", 8000),
            deepseek_effort=_env_str("DEEPSEEK_EFFORT", "low"),
            ai_auto=_env_bool("AI_AUTO_DECISION", False),
            ai_auto_min_confidence=_env_float("AI_AUTO_MIN_CONFIDENCE", 0.65),
            ai_auto_cooldown_seconds=_env_float("AI_AUTO_COOLDOWN_SECONDS", 300.0),
            ai_scan_seconds=_env_float("AI_SCAN_SECONDS", 300.0),
            analysis_scan_seconds=_env_float("ANALYSIS_SCAN_SECONDS", 300.0),
            ict_setup_enabled=_env_bool("ICT_SETUP_ENABLED", False),
            require_primary_setup=_env_bool("REQUIRE_PRIMARY_SETUP", True),
            ict_setup_shadow=_env_bool("ICT_SETUP_SHADOW", True),
            ict_entry_policy=_env_str("ICT_ENTRY_POLICY", "legacy").lower(),
            ict_structure_exits=_env_bool("ICT_STRUCTURE_EXITS", False),
            setup_engine_ids=_env_str("SETUP_ENGINE_IDS", "ict,ichimoku,wyckoff,chan"),
            setup_primary_engine=_env_str("SETUP_PRIMARY_ENGINE", "ict").lower(),
            ai_market_scan=_env_bool("AI_MARKET_SCAN", False),
            ai_execution_enabled=_env_bool("AI_EXECUTION_ENABLED", False),
            ai_market_scan_seconds=_env_float("AI_MARKET_SCAN_SECONDS", 300.0),
            ai_market_scan_max_symbols=_env_int("AI_MARKET_SCAN_MAX_SYMBOLS", 10),
        )
        for key, value in overrides.items():
            if value is not None and hasattr(cfg, key):
                setattr(cfg, key, value)
        # Re-run validation after CLI overrides.
        cfg.__post_init__()
        return cfg
