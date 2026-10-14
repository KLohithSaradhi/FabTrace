from __future__ import annotations

import sqlite3

DATABASE_SCHEMA_VERSION = 6


class StorageSchemaError(RuntimeError):
    """Raised when a FabTrace SQLite schema cannot be opened safely."""


def initialize_database(connection: sqlite3.Connection) -> None:
    """Create or migrate the local FabTrace schema to the current version."""
    current_version = connection.execute("PRAGMA user_version").fetchone()[0]
    if current_version not in (0, 1, 2, 3, 4, 5, DATABASE_SCHEMA_VERSION):
        raise StorageSchemaError(
            f"unsupported FabTrace database schema version: {current_version}"
        )

    connection.executescript(
        """
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS ingestion_batches (
            batch_id TEXT PRIMARY KEY,
            source_path TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL CHECK (status IN ('started', 'completed')),
            total_lines INTEGER NOT NULL DEFAULT 0 CHECK (total_lines >= 0),
            accepted_count INTEGER NOT NULL DEFAULT 0 CHECK (accepted_count >= 0),
            duplicate_count INTEGER NOT NULL DEFAULT 0 CHECK (duplicate_count >= 0),
            quarantined_count INTEGER NOT NULL DEFAULT 0 CHECK (quarantined_count >= 0)
        );

        CREATE TABLE IF NOT EXISTS raw_provider_messages (
            message_id TEXT PRIMARY KEY,
            schema_version TEXT NOT NULL,
            generator_version TEXT NOT NULL,
            synthetic INTEGER NOT NULL CHECK (synthetic = 1),
            scenario_id TEXT NOT NULL,
            source_event_id TEXT NOT NULL,
            provider_profile_id TEXT NOT NULL,
            source_provider_id TEXT NOT NULL,
            purchase_order_id TEXT NOT NULL,
            channel TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            received_at TEXT NOT NULL,
            subject TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            raw_payload BLOB NOT NULL,
            ingested_at TEXT NOT NULL,
            source_path TEXT NOT NULL,
            source_line INTEGER NOT NULL CHECK (source_line > 0),
            first_batch_id TEXT NOT NULL REFERENCES ingestion_batches(batch_id)
        );

        CREATE TABLE IF NOT EXISTS registered_plans (
            order_id TEXT PRIMARY KEY,
            plan_sha256 TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            planner_version TEXT NOT NULL,
            synthetic INTEGER NOT NULL CHECK (synthetic = 1),
            product_id TEXT NOT NULL,
            product_version INTEGER NOT NULL CHECK (product_version > 0),
            requested_quantity REAL NOT NULL CHECK (requested_quantity > 0),
            output_unit TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            planned_delivery_at TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            provider_order_count INTEGER NOT NULL CHECK (provider_order_count >= 0),
            source_path TEXT NOT NULL,
            registered_at TEXT NOT NULL,
            raw_plan BLOB NOT NULL
        );

        CREATE TABLE IF NOT EXISTS planned_provider_orders (
            provider_order_id TEXT PRIMARY KEY,
            customer_order_id TEXT NOT NULL
                REFERENCES registered_plans(order_id),
            order_type TEXT NOT NULL CHECK (
                order_type IN ('purchase_order', 'work_order')
            ),
            node_id TEXT NOT NULL,
            node_name TEXT NOT NULL,
            capability_id TEXT,
            provider_id TEXT NOT NULL,
            quantity REAL NOT NULL CHECK (quantity > 0),
            unit TEXT NOT NULL,
            release_at TEXT NOT NULL,
            expected_completion_at TEXT NOT NULL,
            critical_path INTEGER NOT NULL CHECK (critical_path IN (0, 1))
        );

        CREATE TABLE IF NOT EXISTS planned_provider_order_dependencies (
            provider_order_id TEXT NOT NULL
                REFERENCES planned_provider_orders(provider_order_id),
            depends_on_provider_order_id TEXT NOT NULL
                REFERENCES planned_provider_orders(provider_order_id),
            PRIMARY KEY (provider_order_id, depends_on_provider_order_id),
            CHECK (provider_order_id != depends_on_provider_order_id)
        );

        CREATE TABLE IF NOT EXISTS pipeline_runs (
            pipeline_run_id TEXT PRIMARY KEY,
            customer_order_id TEXT NOT NULL,
            scenario_id TEXT NOT NULL,
            plan_sha256 TEXT NOT NULL,
            scenario_sha256 TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL,
            plan_path TEXT NOT NULL,
            scenario_path TEXT NOT NULL,
            data_directory TEXT NOT NULL,
            database_path TEXT NOT NULL,
            output_directory TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pipeline_run_events (
            pipeline_event_id TEXT PRIMARY KEY,
            pipeline_run_id TEXT NOT NULL REFERENCES pipeline_runs(pipeline_run_id),
            event_sequence INTEGER NOT NULL CHECK (event_sequence > 0),
            status TEXT NOT NULL CHECK (status IN ('RUNNING', 'COMPLETED', 'FAILED')),
            recorded_at TEXT NOT NULL,
            error TEXT,
            UNIQUE (pipeline_run_id, event_sequence)
        );

        CREATE TABLE IF NOT EXISTS pipeline_stage_attempts (
            stage_attempt_id TEXT PRIMARY KEY,
            pipeline_run_id TEXT NOT NULL REFERENCES pipeline_runs(pipeline_run_id),
            stage_name TEXT NOT NULL,
            attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
            started_at TEXT NOT NULL,
            UNIQUE (pipeline_run_id, stage_name, attempt_number)
        );

        CREATE TABLE IF NOT EXISTS pipeline_stage_events (
            stage_event_id TEXT PRIMARY KEY,
            stage_attempt_id TEXT NOT NULL
                REFERENCES pipeline_stage_attempts(stage_attempt_id),
            status TEXT NOT NULL CHECK (status IN ('COMPLETED', 'FAILED')),
            recorded_at TEXT NOT NULL,
            duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
            detail_json TEXT,
            error TEXT,
            UNIQUE (stage_attempt_id)
        );

        CREATE TABLE IF NOT EXISTS quarantined_provider_messages (
            quarantine_id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL REFERENCES ingestion_batches(batch_id),
            source_path TEXT NOT NULL,
            source_line INTEGER NOT NULL CHECK (source_line > 0),
            observed_at TEXT NOT NULL,
            error_code TEXT NOT NULL,
            error_detail TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            raw_payload BLOB NOT NULL
        );

        CREATE TABLE IF NOT EXISTS extraction_runs (
            extraction_run_id TEXT PRIMARY KEY,
            scenario_id TEXT NOT NULL,
            extractor_version TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL CHECK (status IN ('started', 'completed')),
            selected_count INTEGER NOT NULL DEFAULT 0 CHECK (selected_count >= 0),
            extracted_count INTEGER NOT NULL DEFAULT 0 CHECK (extracted_count >= 0),
            abstained_count INTEGER NOT NULL DEFAULT 0 CHECK (abstained_count >= 0),
            review_count INTEGER NOT NULL DEFAULT 0 CHECK (review_count >= 0),
            failed_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
            skipped_count INTEGER NOT NULL DEFAULT 0 CHECK (skipped_count >= 0),
            claim_count INTEGER NOT NULL DEFAULT 0 CHECK (claim_count >= 0)
        );

        CREATE TABLE IF NOT EXISTS extraction_attempts (
            attempt_id TEXT PRIMARY KEY,
            extraction_run_id TEXT NOT NULL
                REFERENCES extraction_runs(extraction_run_id),
            message_id TEXT NOT NULL REFERENCES raw_provider_messages(message_id),
            extractor_version TEXT NOT NULL,
            attempted_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('extracted', 'abstained', 'review_required', 'failed')
            ),
            claimed_order_id TEXT,
            resolved_order_id TEXT,
            resolution_status TEXT NOT NULL CHECK (
                resolution_status IN ('resolved', 'missing', 'conflict', 'ambiguous')
            ),
            detail TEXT
        );

        CREATE TABLE IF NOT EXISTS extracted_claims (
            claim_id TEXT PRIMARY KEY,
            attempt_id TEXT NOT NULL REFERENCES extraction_attempts(attempt_id),
            extraction_run_id TEXT NOT NULL
                REFERENCES extraction_runs(extraction_run_id),
            message_id TEXT NOT NULL REFERENCES raw_provider_messages(message_id),
            claim_index INTEGER NOT NULL CHECK (claim_index >= 0),
            claim_type TEXT NOT NULL CHECK (claim_type IN (
                'order_status', 'progress_pct', 'quantity_completed',
                'quantity_ordered', 'unit', 'forecast_delivery_date',
                'delay_days', 'incident_reason', 'milestone_date'
            )),
            value_text TEXT,
            value_number REAL,
            value_date TEXT,
            unit TEXT,
            source_field TEXT NOT NULL CHECK (source_field IN ('subject', 'body')),
            span_start INTEGER NOT NULL CHECK (span_start >= 0),
            span_end INTEGER NOT NULL CHECK (span_end >= span_start),
            source_text TEXT NOT NULL,
            confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
            validation_status TEXT NOT NULL CHECK (
                validation_status IN ('valid', 'review_required')
            ),
            extractor_version TEXT NOT NULL,
            claimed_order_id TEXT,
            resolved_order_id TEXT,
            CHECK (
                (value_text IS NOT NULL) +
                (value_number IS NOT NULL) +
                (value_date IS NOT NULL) = 1
            ),
            UNIQUE (message_id, extractor_version, claim_index)
        );

        CREATE TABLE IF NOT EXISTS reconciliation_runs (
            reconciliation_run_id TEXT PRIMARY KEY,
            scenario_id TEXT NOT NULL,
            extractor_version TEXT NOT NULL,
            reconciliation_version TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL CHECK (status IN ('started', 'completed')),
            selected_count INTEGER NOT NULL DEFAULT 0 CHECK (selected_count >= 0),
            applied_count INTEGER NOT NULL DEFAULT 0 CHECK (applied_count >= 0),
            stale_count INTEGER NOT NULL DEFAULT 0 CHECK (stale_count >= 0),
            conflict_count INTEGER NOT NULL DEFAULT 0 CHECK (conflict_count >= 0),
            skipped_count INTEGER NOT NULL DEFAULT 0 CHECK (skipped_count >= 0),
            snapshot_count INTEGER NOT NULL DEFAULT 0 CHECK (snapshot_count >= 0)
        );

        CREATE TABLE IF NOT EXISTS reconciliation_decisions (
            decision_id TEXT PRIMARY KEY,
            reconciliation_run_id TEXT NOT NULL
                REFERENCES reconciliation_runs(reconciliation_run_id),
            message_id TEXT NOT NULL REFERENCES raw_provider_messages(message_id),
            scenario_id TEXT NOT NULL,
            purchase_order_id TEXT NOT NULL,
            extractor_version TEXT NOT NULL,
            reconciliation_version TEXT NOT NULL,
            processing_sequence INTEGER NOT NULL CHECK (processing_sequence > 0),
            sent_at TEXT NOT NULL,
            received_at TEXT NOT NULL,
            decision TEXT NOT NULL CHECK (
                decision IN ('applied', 'stale', 'conflict')
            ),
            detail TEXT,
            decided_at TEXT NOT NULL,
            UNIQUE (message_id, reconciliation_version)
        );

        CREATE TABLE IF NOT EXISTS order_state_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            decision_id TEXT NOT NULL
                REFERENCES reconciliation_decisions(decision_id),
            reconciliation_run_id TEXT NOT NULL
                REFERENCES reconciliation_runs(reconciliation_run_id),
            reconciliation_version TEXT NOT NULL,
            scenario_id TEXT NOT NULL,
            purchase_order_id TEXT NOT NULL,
            provider_profile_id TEXT NOT NULL,
            source_provider_id TEXT NOT NULL,
            snapshot_sequence INTEGER NOT NULL CHECK (snapshot_sequence > 0),
            as_of_received_at TEXT NOT NULL,
            effective_through_sent_at TEXT NOT NULL,
            last_message_id TEXT NOT NULL
                REFERENCES raw_provider_messages(message_id),
            created_at TEXT NOT NULL,

            order_status TEXT CHECK (order_status IN (
                'ACKNOWLEDGED', 'QUEUED', 'IN_PRODUCTION', 'BLOCKED',
                'QUALITY_CHECK', 'READY_TO_SHIP', 'SHIPPED', 'DELIVERED'
            )),
            order_status_source_message_id TEXT
                REFERENCES raw_provider_messages(message_id),
            order_status_effective_at TEXT,

            progress_pct REAL CHECK (
                progress_pct IS NULL OR (progress_pct >= 0 AND progress_pct <= 100)
            ),
            progress_pct_source_message_id TEXT
                REFERENCES raw_provider_messages(message_id),
            progress_pct_effective_at TEXT,

            quantity_completed REAL CHECK (
                quantity_completed IS NULL OR quantity_completed >= 0
            ),
            quantity_completed_source_message_id TEXT
                REFERENCES raw_provider_messages(message_id),
            quantity_completed_effective_at TEXT,

            quantity_ordered REAL CHECK (
                quantity_ordered IS NULL OR quantity_ordered > 0
            ),
            quantity_ordered_source_message_id TEXT
                REFERENCES raw_provider_messages(message_id),
            quantity_ordered_effective_at TEXT,

            quantity_unit TEXT,
            quantity_unit_source_message_id TEXT
                REFERENCES raw_provider_messages(message_id),
            quantity_unit_effective_at TEXT,

            forecast_delivery_date TEXT,
            forecast_delivery_date_source_message_id TEXT
                REFERENCES raw_provider_messages(message_id),
            forecast_delivery_date_effective_at TEXT,

            delay_days REAL CHECK (delay_days IS NULL OR delay_days > 0),
            delay_days_source_message_id TEXT
                REFERENCES raw_provider_messages(message_id),
            delay_days_effective_at TEXT,

            incident_reason TEXT,
            incident_reason_source_message_id TEXT
                REFERENCES raw_provider_messages(message_id),
            incident_reason_effective_at TEXT,

            milestone_date TEXT,
            milestone_date_source_message_id TEXT
                REFERENCES raw_provider_messages(message_id),
            milestone_date_effective_at TEXT,

            CHECK (
                quantity_completed IS NULL OR quantity_ordered IS NULL OR
                quantity_completed <= quantity_ordered
            ),
            UNIQUE (
                scenario_id, purchase_order_id, reconciliation_version,
                snapshot_sequence
            )
        );

        CREATE TABLE IF NOT EXISTS analytics_runs (
            analytics_run_id TEXT PRIMARY KEY,
            scenario_id TEXT NOT NULL,
            reconciliation_version TEXT NOT NULL,
            metric_version TEXT NOT NULL,
            as_of_time TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL CHECK (status IN ('started', 'completed')),
            selected_order_count INTEGER NOT NULL DEFAULT 0
                CHECK (selected_order_count >= 0),
            computed_count INTEGER NOT NULL DEFAULT 0 CHECK (computed_count >= 0),
            skipped_count INTEGER NOT NULL DEFAULT 0 CHECK (skipped_count >= 0)
        );

        CREATE TABLE IF NOT EXISTS order_metric_snapshots (
            metric_snapshot_id TEXT PRIMARY KEY,
            analytics_run_id TEXT NOT NULL REFERENCES analytics_runs(analytics_run_id),
            source_snapshot_id TEXT NOT NULL
                REFERENCES order_state_snapshots(snapshot_id),
            metric_version TEXT NOT NULL,
            reconciliation_version TEXT NOT NULL,
            scenario_id TEXT NOT NULL,
            purchase_order_id TEXT NOT NULL,
            provider_profile_id TEXT NOT NULL,
            source_provider_id TEXT NOT NULL,
            as_of_time TEXT NOT NULL,
            computed_at TEXT NOT NULL,
            order_status TEXT NOT NULL,
            progress_pct REAL CHECK (
                progress_pct IS NULL OR (progress_pct >= 0 AND progress_pct <= 100)
            ),
            initial_forecast_date TEXT,
            current_forecast_date TEXT,
            forecast_drift_days INTEGER,
            forecast_revision_count INTEGER NOT NULL
                CHECK (forecast_revision_count >= 0),
            days_to_forecast INTEGER,
            reported_delay_days REAL CHECK (
                reported_delay_days IS NULL OR reported_delay_days > 0
            ),
            has_reported_delay INTEGER NOT NULL
                CHECK (has_reported_delay IN (0, 1)),
            progress_velocity_pct_per_day REAL CHECK (
                progress_velocity_pct_per_day IS NULL OR
                progress_velocity_pct_per_day >= 0
            ),
            latest_update_age_days REAL NOT NULL
                CHECK (latest_update_age_days >= 0),
            communication_lag_days REAL NOT NULL
                CHECK (communication_lag_days >= 0),
            applied_message_count INTEGER NOT NULL
                CHECK (applied_message_count >= 0),
            stale_message_count INTEGER NOT NULL
                CHECK (stale_message_count >= 0),
            conflict_message_count INTEGER NOT NULL
                CHECK (conflict_message_count >= 0),
            CHECK (
                (has_reported_delay = 0 AND reported_delay_days IS NULL) OR
                (has_reported_delay = 1 AND reported_delay_days IS NOT NULL)
            ),
            UNIQUE (
                scenario_id, purchase_order_id, metric_version,
                reconciliation_version, as_of_time
            )
        );

        CREATE INDEX IF NOT EXISTS idx_raw_provider_messages_order
            ON raw_provider_messages (scenario_id, purchase_order_id, sent_at);
        CREATE INDEX IF NOT EXISTS idx_planned_provider_orders_customer
            ON planned_provider_orders (customer_order_id, provider_order_id);
        CREATE INDEX IF NOT EXISTS idx_planned_dependencies_upstream
            ON planned_provider_order_dependencies (
                depends_on_provider_order_id, provider_order_id
            );
        CREATE INDEX IF NOT EXISTS idx_pipeline_runs_customer
            ON pipeline_runs (customer_order_id, scenario_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_pipeline_run_events_current
            ON pipeline_run_events (pipeline_run_id, event_sequence);
        CREATE INDEX IF NOT EXISTS idx_pipeline_stage_attempts_run
            ON pipeline_stage_attempts (
                pipeline_run_id, stage_name, attempt_number
            );
        CREATE INDEX IF NOT EXISTS idx_quarantine_batch
            ON quarantined_provider_messages (batch_id, source_line);
        CREATE INDEX IF NOT EXISTS idx_extraction_runs_scenario
            ON extraction_runs (scenario_id, extractor_version, started_at);
        CREATE INDEX IF NOT EXISTS idx_extraction_attempts_message
            ON extraction_attempts (message_id, extractor_version, status);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_extraction_attempts_completed
            ON extraction_attempts (message_id, extractor_version)
            WHERE status IN ('extracted', 'abstained', 'review_required');
        CREATE INDEX IF NOT EXISTS idx_extracted_claims_lookup
            ON extracted_claims (message_id, extractor_version, claim_type);
        CREATE INDEX IF NOT EXISTS idx_reconciliation_runs_scenario
            ON reconciliation_runs (
                scenario_id, reconciliation_version, started_at
            );
        CREATE INDEX IF NOT EXISTS idx_reconciliation_decisions_order
            ON reconciliation_decisions (
                scenario_id, purchase_order_id, reconciliation_version,
                processing_sequence
            );
        CREATE INDEX IF NOT EXISTS idx_order_state_snapshots_current
            ON order_state_snapshots (
                scenario_id, purchase_order_id, reconciliation_version,
                snapshot_sequence
            );
        CREATE INDEX IF NOT EXISTS idx_analytics_runs_scenario
            ON analytics_runs (scenario_id, metric_version, as_of_time);
        CREATE INDEX IF NOT EXISTS idx_order_metric_snapshots_current
            ON order_metric_snapshots (
                scenario_id, purchase_order_id, metric_version,
                reconciliation_version, as_of_time
            );

        CREATE VIEW IF NOT EXISTS current_order_state AS
        SELECT snapshot.*
        FROM order_state_snapshots AS snapshot
        WHERE snapshot.snapshot_sequence = (
            SELECT MAX(candidate.snapshot_sequence)
            FROM order_state_snapshots AS candidate
            WHERE candidate.scenario_id = snapshot.scenario_id
              AND candidate.purchase_order_id = snapshot.purchase_order_id
              AND candidate.reconciliation_version = snapshot.reconciliation_version
        );

        CREATE VIEW IF NOT EXISTS current_order_metrics AS
        SELECT metric.*
        FROM order_metric_snapshots AS metric
        WHERE metric.as_of_time = (
            SELECT MAX(candidate.as_of_time)
            FROM order_metric_snapshots AS candidate
            WHERE candidate.scenario_id = metric.scenario_id
              AND candidate.purchase_order_id = metric.purchase_order_id
              AND candidate.metric_version = metric.metric_version
              AND candidate.reconciliation_version = metric.reconciliation_version
        );

        CREATE TRIGGER IF NOT EXISTS raw_provider_messages_no_update
        BEFORE UPDATE ON raw_provider_messages
        BEGIN
            SELECT RAISE(ABORT, 'raw provider messages are append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS registered_plans_no_update
        BEFORE UPDATE ON registered_plans
        BEGIN
            SELECT RAISE(ABORT, 'registered plans are append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS registered_plans_no_delete
        BEFORE DELETE ON registered_plans
        BEGIN
            SELECT RAISE(ABORT, 'registered plans are append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS planned_provider_orders_no_update
        BEFORE UPDATE ON planned_provider_orders
        BEGIN
            SELECT RAISE(ABORT, 'planned provider orders are append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS planned_provider_orders_no_delete
        BEFORE DELETE ON planned_provider_orders
        BEGIN
            SELECT RAISE(ABORT, 'planned provider orders are append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS planned_provider_dependencies_no_update
        BEFORE UPDATE ON planned_provider_order_dependencies
        BEGIN
            SELECT RAISE(ABORT, 'planned provider dependencies are append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS planned_provider_dependencies_no_delete
        BEFORE DELETE ON planned_provider_order_dependencies
        BEGIN
            SELECT RAISE(ABORT, 'planned provider dependencies are append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS pipeline_runs_no_update
        BEFORE UPDATE ON pipeline_runs
        BEGIN
            SELECT RAISE(ABORT, 'pipeline runs are append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS pipeline_runs_no_delete
        BEFORE DELETE ON pipeline_runs
        BEGIN
            SELECT RAISE(ABORT, 'pipeline runs are append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS pipeline_run_events_no_update
        BEFORE UPDATE ON pipeline_run_events
        BEGIN
            SELECT RAISE(ABORT, 'pipeline run events are append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS pipeline_run_events_no_delete
        BEFORE DELETE ON pipeline_run_events
        BEGIN
            SELECT RAISE(ABORT, 'pipeline run events are append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS pipeline_stage_attempts_no_update
        BEFORE UPDATE ON pipeline_stage_attempts
        BEGIN
            SELECT RAISE(ABORT, 'pipeline stage attempts are append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS pipeline_stage_attempts_no_delete
        BEFORE DELETE ON pipeline_stage_attempts
        BEGIN
            SELECT RAISE(ABORT, 'pipeline stage attempts are append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS pipeline_stage_events_no_update
        BEFORE UPDATE ON pipeline_stage_events
        BEGIN
            SELECT RAISE(ABORT, 'pipeline stage events are append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS pipeline_stage_events_no_delete
        BEFORE DELETE ON pipeline_stage_events
        BEGIN
            SELECT RAISE(ABORT, 'pipeline stage events are append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS raw_provider_messages_no_delete
        BEFORE DELETE ON raw_provider_messages
        BEGIN
            SELECT RAISE(ABORT, 'raw provider messages are append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS quarantined_provider_messages_no_update
        BEFORE UPDATE ON quarantined_provider_messages
        BEGIN
            SELECT RAISE(ABORT, 'quarantined provider messages are append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS quarantined_provider_messages_no_delete
        BEFORE DELETE ON quarantined_provider_messages
        BEGIN
            SELECT RAISE(ABORT, 'quarantined provider messages are append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS extraction_attempts_no_update
        BEFORE UPDATE ON extraction_attempts
        BEGIN
            SELECT RAISE(ABORT, 'extraction attempts are append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS extraction_attempts_no_delete
        BEFORE DELETE ON extraction_attempts
        BEGIN
            SELECT RAISE(ABORT, 'extraction attempts are append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS extracted_claims_no_update
        BEFORE UPDATE ON extracted_claims
        BEGIN
            SELECT RAISE(ABORT, 'extracted claims are append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS extracted_claims_no_delete
        BEFORE DELETE ON extracted_claims
        BEGIN
            SELECT RAISE(ABORT, 'extracted claims are append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS reconciliation_decisions_no_update
        BEFORE UPDATE ON reconciliation_decisions
        BEGIN
            SELECT RAISE(ABORT, 'reconciliation decisions are append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS reconciliation_decisions_no_delete
        BEFORE DELETE ON reconciliation_decisions
        BEGIN
            SELECT RAISE(ABORT, 'reconciliation decisions are append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS order_state_snapshots_no_update
        BEFORE UPDATE ON order_state_snapshots
        BEGIN
            SELECT RAISE(ABORT, 'order state snapshots are append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS order_state_snapshots_no_delete
        BEFORE DELETE ON order_state_snapshots
        BEGIN
            SELECT RAISE(ABORT, 'order state snapshots are append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS order_metric_snapshots_no_update
        BEFORE UPDATE ON order_metric_snapshots
        BEGIN
            SELECT RAISE(ABORT, 'order metric snapshots are append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS order_metric_snapshots_no_delete
        BEFORE DELETE ON order_metric_snapshots
        BEGIN
            SELECT RAISE(ABORT, 'order metric snapshots are append-only');
        END;
        """
    )
    connection.execute(f"PRAGMA user_version = {DATABASE_SCHEMA_VERSION}")
    connection.commit()
