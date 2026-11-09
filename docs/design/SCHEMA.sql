-- ICT-Trader analytics schema v2
-- Canonical DDL generated from src/ict_trader/realtime/storage/analytics_schema.py.
-- ClickHouse 24.8, database `ict`, all timestamps UTC.
-- Idempotent ReplacingMergeTree tables; TTLs included exactly as deployed.
-- Legacy narrow `signals` is renamed by code before this schema is applied.
-- checksum: 8077c086aebe51f9e24a64b77f87778f645820b84d026537b862a25f0c257aa9


        CREATE TABLE IF NOT EXISTS ict.schema_migrations
        (
            version UInt32,
            name String,
            checksum String,
            applied_at DateTime64(3, 'UTC')
        )
        ENGINE = ReplacingMergeTree(applied_at)
        ORDER BY (version, name)
        


        CREATE TABLE IF NOT EXISTS ict.signals
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
        


        CREATE TABLE IF NOT EXISTS ict.setups
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
        


        CREATE TABLE IF NOT EXISTS ict.tp_sl_events
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
        


        CREATE TABLE IF NOT EXISTS ict.ai_decisions
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
        


        CREATE TABLE IF NOT EXISTS ict.llm_api_calls
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
        


        CREATE TABLE IF NOT EXISTS ict.error_events
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
        


        CREATE TABLE IF NOT EXISTS ict.engine_performance
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
        


        CREATE TABLE IF NOT EXISTS ict.strategy_versions
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
        
