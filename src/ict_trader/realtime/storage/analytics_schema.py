"""Versioned ClickHouse analytics schema.

This module owns the *new* analytics tables.  It is intentionally separate from
the K-line table DDL so schema evolution can be reviewed and tested without
touching the hot market-data path.

Legacy migration: the first realtime release had a narrow ``signals`` table
(``bar_time/entry/stop/target/...``).  If that shape is detected, it is renamed
to ``signals_legacy_<utc timestamp>`` before the new contract table is created.
No historical rows are deleted.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2
MIGRATION_NAME = "analytics_contract_v2"

TABLES = (
    "signals", "setups", "tp_sl_events", "ai_decisions", "llm_api_calls",
    "error_events", "engine_performance", "strategy_versions",
)


def _ddl(database: str) -> list[str]:
    db = database
    return [
        f"""
        CREATE TABLE IF NOT EXISTS {db}.schema_migrations
        (
            version UInt32,
            name String,
            checksum String,
            applied_at DateTime64(3, 'UTC')
        )
        ENGINE = ReplacingMergeTree(applied_at)
        ORDER BY (version, name)
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {db}.signals
        (
            signal_id String,
            engine LowCardinality(String),
            engine_version LowCardinality(String),
            symbol LowCardinality(String),
            interval LowCardinality(String),
            direction LowCardinality(String),
            signal_type LowCardinality(String),
            trigger_zone_low Float64,
            trigger_zone_high Float64,
            invalidation_price Float64,
            confidence Float64,
            evidence String CODEC(ZSTD(3)),
            source_kline_open_time DateTime64(3, 'UTC'),
            created_at DateTime64(3, 'UTC'),
            ingested_at DateTime64(3, 'UTC')
        )
        ENGINE = ReplacingMergeTree(ingested_at)
        PARTITION BY toYYYYMM(created_at)
        ORDER BY (symbol, interval, engine, signal_id)
        TTL toDateTime(created_at) + INTERVAL 730 DAY DELETE
        SETTINGS index_granularity = 8192
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {db}.setups
        (
            setup_id String,
            symbol LowCardinality(String),
            interval LowCardinality(String),
            direction LowCardinality(String),
            status LowCardinality(String),
            created_at DateTime64(3, 'UTC'),
            opened_at Nullable(DateTime64(3, 'UTC')),
            closed_at Nullable(DateTime64(3, 'UTC')),
            updated_at DateTime64(3, 'UTC'),
            signal_ids Array(String),
            engine_signals String CODEC(ZSTD(3)),
            ai_decision_id String,
            ai_confidence Float64,
            ai_decision String CODEC(ZSTD(3)),
            entry_price Float64,
            entry_type LowCardinality(String),
            position_size Float64,
            tp_planned String CODEC(ZSTD(3)),
            sl_planned Float64,
            risk_reward_planned Float64,
            tp_actual String CODEC(ZSTD(3)),
            sl_actual Nullable(Float64),
            exit_price Nullable(Float64),
            pnl Float64,
            pnl_pct Float64,
            fees Float64,
            engine_version String,
            strategy_version String,
            prompt_version String,
            ingested_at DateTime64(3, 'UTC')
        )
        ENGINE = ReplacingMergeTree(ingested_at)
        PARTITION BY toYYYYMM(created_at)
        ORDER BY (symbol, setup_id)
        TTL toDateTime(created_at) + INTERVAL 730 DAY DELETE
        SETTINGS index_granularity = 8192
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {db}.tp_sl_events
        (
            event_id String,
            setup_id String,
            symbol LowCardinality(String),
            interval LowCardinality(String),
            event_type LowCardinality(String),
            leg_index UInt8,
            planned_price Nullable(Float64),
            actual_price Nullable(Float64),
            size_pct Float64,
            slippage_bps Float64,
            reason String,
            event_time DateTime64(3, 'UTC'),
            created_at DateTime64(3, 'UTC'),
            ingested_at DateTime64(3, 'UTC')
        )
        ENGINE = ReplacingMergeTree(ingested_at)
        PARTITION BY toYYYYMM(event_time)
        ORDER BY (setup_id, event_time, event_id)
        TTL toDateTime(event_time) + INTERVAL 730 DAY DELETE
        SETTINGS index_granularity = 8192
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {db}.ai_decisions
        (
            decision_id String,
            setup_id String,
            symbol LowCardinality(String),
            interval LowCardinality(String),
            provider LowCardinality(String),
            model LowCardinality(String),
            prompt_version LowCardinality(String),
            input_snapshot String CODEC(ZSTD(3)),
            raw_output String CODEC(ZSTD(3)),
            parsed_decision String CODEC(ZSTD(3)),
            decision_status Enum8(
                'success' = 1,
                'schema_invalid' = 2,
                'api_timeout' = 3,
                'api_error' = 4,
                'degraded_to_rules' = 5
            ),
            latency_ms UInt32,
            prompt_tokens UInt32,
            completion_tokens UInt32,
            total_tokens UInt32,
            cost_usd Float64,
            created_at DateTime64(3, 'UTC'),
            ingested_at DateTime64(3, 'UTC')
        )
        ENGINE = ReplacingMergeTree(ingested_at)
        PARTITION BY toYYYYMM(created_at)
        ORDER BY (symbol, decision_id)
        TTL toDateTime(created_at) + INTERVAL 730 DAY DELETE
        SETTINGS index_granularity = 8192
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {db}.llm_api_calls
        (
            call_id String,
            decision_id String,
            provider LowCardinality(String),
            model LowCardinality(String),
            status_code Int32,
            error_type LowCardinality(String),
            latency_ms UInt32,
            retry_count UInt8,
            called_at DateTime64(3, 'UTC'),
            created_at DateTime64(3, 'UTC'),
            ingested_at DateTime64(3, 'UTC')
        )
        ENGINE = ReplacingMergeTree(ingested_at)
        PARTITION BY toYYYYMM(called_at)
        ORDER BY (provider, call_id)
        TTL toDateTime(called_at) + INTERVAL 365 DAY DELETE
        SETTINGS index_granularity = 8192
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {db}.error_events
        (
            error_id String,
            category LowCardinality(String),
            subtype LowCardinality(String),
            symbol LowCardinality(String),
            interval LowCardinality(String),
            module LowCardinality(String),
            error_message String,
            stack_trace String CODEC(ZSTD(3)),
            context String CODEC(ZSTD(3)),
            occurred_at DateTime64(3, 'UTC'),
            severity LowCardinality(String),
            resolved_at Nullable(DateTime64(3, 'UTC')),
            version LowCardinality(String),
            ingested_at DateTime64(3, 'UTC')
        )
        ENGINE = ReplacingMergeTree(ingested_at)
        PARTITION BY toYYYYMM(occurred_at)
        ORDER BY (module, error_id)
        TTL toDateTime(occurred_at) + INTERVAL 365 DAY DELETE
        SETTINGS index_granularity = 8192
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {db}.engine_performance
        (
            sample_id String,
            engine LowCardinality(String),
            engine_version LowCardinality(String),
            symbol LowCardinality(String),
            interval LowCardinality(String),
            source_kline_open_time DateTime64(3, 'UTC'),
            latency_ms Float64,
            cpu_ms Float64,
            memory_bytes UInt64,
            bar_count UInt32,
            status LowCardinality(String),
            error_type LowCardinality(String),
            occurred_at DateTime64(3, 'UTC'),
            ingested_at DateTime64(3, 'UTC')
        )
        ENGINE = ReplacingMergeTree(ingested_at)
        PARTITION BY toYYYYMM(occurred_at)
        ORDER BY (engine, sample_id)
        TTL toDateTime(occurred_at) + INTERVAL 180 DAY DELETE
        SETTINGS index_granularity = 8192
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {db}.strategy_versions
        (
            strategy_version String,
            status LowCardinality(String),
            config_json String CODEC(ZSTD(3)),
            engine_versions String,
            checksum String,
            notes String,
            created_at DateTime64(3, 'UTC'),
            promoted_at Nullable(DateTime64(3, 'UTC')),
            ingested_at DateTime64(3, 'UTC')
        )
        ENGINE = ReplacingMergeTree(ingested_at)
        PARTITION BY toYYYYMM(created_at)
        ORDER BY (strategy_version)
        TTL toDateTime(created_at) + INTERVAL 730 DAY DELETE
        SETTINGS index_granularity = 8192
        """,
    ]


def schema_checksum() -> str:
    material = "\n".join(_ddl("{database}"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _table_exists(client: Any, database: str, table: str) -> bool:
    result = client.query(
        "SELECT count() FROM system.tables WHERE database = {db:String} AND name = {table:String}",
        parameters={"db": database, "table": table},
    )
    return bool(result.result_rows and int(result.result_rows[0][0]) > 0)


def _columns(client: Any, database: str, table: str) -> set[str]:
    result = client.query(
        "SELECT name FROM system.columns WHERE database = {db:String} AND table = {table:String}",
        parameters={"db": database, "table": table},
    )
    return {str(row[0]) for row in result.result_rows}


def _migrate_legacy_signals(client: Any, database: str) -> str:
    """Rename the old narrow signals table without deleting its rows."""
    if not _table_exists(client, database, "signals"):
        return ""
    cols = _columns(client, database, "signals")
    if "engine" in cols or "bar_time" not in cols:
        return ""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    legacy = f"signals_legacy_{stamp}"
    client.command(f"RENAME TABLE {database}.signals TO {database}.{legacy}")
    log.warning("legacy signals table renamed to %s.%s", database, legacy)
    return legacy


def ensure_analytics_schema(client: Any, database: str) -> dict[str, Any]:
    """Create analytics tables and apply the legacy signal rename.

    The caller is responsible for holding the ClickHouse client lock and for
    running this in a worker thread.
    """
    if not database.replace("_", "").isalnum():
        raise ValueError(f"unsafe ClickHouse database name: {database!r}")
    client.command(f"CREATE DATABASE IF NOT EXISTS {database}")
    legacy = _migrate_legacy_signals(client, database)
    for statement in _ddl(database):
        client.command(statement)

    checksum = schema_checksum()
    client.command(
        f"""
        INSERT INTO {database}.schema_migrations(version, name, checksum, applied_at)
        VALUES ({SCHEMA_VERSION}, {{name:String}}, {{checksum:String}}, now64(3))
        """,
        parameters={"name": MIGRATION_NAME, "checksum": checksum},
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "tables": TABLES,
        "legacy_signals_table": legacy,
        "checksum": checksum,
    }
