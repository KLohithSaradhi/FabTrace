from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

from fabtrace.analytics import METRIC_VERSION, build_customer_order_projection
from fabtrace.reconciliation import (
    RECONCILIATION_VERSION,
    SUPPORTED_RECONCILIATION_VERSIONS,
)


class DashboardDataError(RuntimeError):
    """Raised when dashboard data cannot be read safely."""


class OrderNotFoundError(DashboardDataError):
    """Raised when a requested scenario/order pair does not exist."""


class CustomerOrderNotFoundError(DashboardDataError):
    """Raised when a registered customer order does not exist."""


class CustomerOrderAmbiguousError(DashboardDataError):
    """Raised when a customer order has several scenario projections."""


class DashboardRepository:
    """Read-only queries over versioned FabTrace state and metrics."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path).resolve()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if not self.database_path.is_file():
            raise DashboardDataError(
                f"FabTrace database does not exist: {self.database_path}"
            )
        uri = f"file:{quote(str(self.database_path))}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            yield connection
        except sqlite3.Error as exc:
            raise DashboardDataError(f"cannot read FabTrace database: {exc}") from exc
        finally:
            if "connection" in locals():
                connection.close()

    @staticmethod
    def _rows(rows: list[sqlite3.Row]) -> list[dict[str, object]]:
        return [dict(row) for row in rows]

    def list_scenarios(self) -> list[str]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT scenario_id
                FROM current_order_state
                WHERE reconciliation_version IN (?, ?)
                ORDER BY scenario_id
                """,
                SUPPORTED_RECONCILIATION_VERSIONS,
            ).fetchall()
        return [str(row["scenario_id"]) for row in rows]

    def list_orders(
        self,
        scenario_id: str | None = None,
    ) -> list[dict[str, object]]:
        where = ""
        parameters: list[object] = [
            RECONCILIATION_VERSION,
            *SUPPORTED_RECONCILIATION_VERSIONS,
            METRIC_VERSION,
        ]
        if scenario_id:
            where = "AND state.scenario_id = ?"
            parameters.append(scenario_id)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                WITH preferred_state AS (
                    SELECT
                        source_state.*,
                        ROW_NUMBER() OVER (
                            PARTITION BY scenario_id, purchase_order_id
                            ORDER BY
                                CASE WHEN reconciliation_version = ? THEN 0 ELSE 1 END,
                                reconciliation_version DESC
                        ) AS preference_rank
                    FROM current_order_state AS source_state
                    WHERE reconciliation_version IN (?, ?)
                )
                SELECT
                    state.scenario_id,
                    state.purchase_order_id,
                    state.reconciliation_version,
                    state.provider_profile_id,
                    state.source_provider_id,
                    state.as_of_received_at AS state_as_of_time,
                    state.order_status,
                    state.progress_pct,
                    state.quantity_completed,
                    state.quantity_ordered,
                    state.quantity_unit,
                    state.forecast_delivery_date,
                    state.delay_days,
                    state.incident_reason,
                    metric.as_of_time AS metric_as_of_time,
                    metric.initial_forecast_date,
                    metric.current_forecast_date,
                    metric.forecast_drift_days,
                    metric.forecast_revision_count,
                    metric.days_to_forecast,
                    metric.reported_delay_days,
                    metric.has_reported_delay,
                    metric.progress_velocity_pct_per_day,
                    metric.latest_update_age_days,
                    metric.communication_lag_days,
                    metric.applied_message_count,
                    metric.stale_message_count,
                    metric.conflict_message_count
                FROM preferred_state AS state
                LEFT JOIN current_order_metrics AS metric
                  ON metric.scenario_id = state.scenario_id
                 AND metric.purchase_order_id = state.purchase_order_id
                 AND metric.reconciliation_version = state.reconciliation_version
                 AND metric.metric_version = ?
                WHERE state.preference_rank = 1
                {where}
                ORDER BY state.scenario_id, state.purchase_order_id
                """,
                parameters,
            ).fetchall()
        return self._rows(rows)

    def get_order(
        self,
        scenario_id: str,
        purchase_order_id: str,
    ) -> dict[str, object]:
        orders = self.list_orders(scenario_id)
        order = next(
            (
                candidate
                for candidate in orders
                if candidate["purchase_order_id"] == purchase_order_id
            ),
            None,
        )
        if order is None:
            raise OrderNotFoundError(
                f"order {purchase_order_id!r} does not exist in "
                f"scenario {scenario_id!r}"
            )
        reconciliation_version = str(order["reconciliation_version"])

        with self._connection() as connection:
            state_history = connection.execute(
                """
                SELECT
                    snapshot_id, source_snapshot.decision_id,
                    snapshot_sequence, as_of_received_at,
                    effective_through_sent_at, last_message_id,
                    order_status, progress_pct, quantity_completed,
                    quantity_ordered, quantity_unit, forecast_delivery_date,
                    delay_days, incident_reason, milestone_date,
                    order_status_source_message_id,
                    progress_pct_source_message_id,
                    forecast_delivery_date_source_message_id,
                    delay_days_source_message_id,
                    incident_reason_source_message_id
                FROM order_state_snapshots AS source_snapshot
                WHERE scenario_id = ? AND purchase_order_id = ?
                  AND reconciliation_version = ?
                ORDER BY snapshot_sequence
                """,
                (scenario_id, purchase_order_id, reconciliation_version),
            ).fetchall()
            metric_history = connection.execute(
                """
                SELECT
                    metric_snapshot_id, source_snapshot_id, as_of_time,
                    computed_at, order_status, progress_pct,
                    initial_forecast_date, current_forecast_date,
                    forecast_drift_days, forecast_revision_count,
                    days_to_forecast, reported_delay_days,
                    has_reported_delay, progress_velocity_pct_per_day,
                    latest_update_age_days, communication_lag_days,
                    applied_message_count, stale_message_count,
                    conflict_message_count
                FROM order_metric_snapshots
                WHERE scenario_id = ? AND purchase_order_id = ?
                  AND reconciliation_version = ? AND metric_version = ?
                ORDER BY as_of_time
                """,
                (
                    scenario_id,
                    purchase_order_id,
                    reconciliation_version,
                    METRIC_VERSION,
                ),
            ).fetchall()
            decisions = connection.execute(
                """
                SELECT
                    decision_id, message_id, processing_sequence,
                    sent_at, received_at, decision, detail, decided_at
                FROM reconciliation_decisions
                WHERE scenario_id = ? AND purchase_order_id = ?
                  AND reconciliation_version = ?
                ORDER BY processing_sequence
                """,
                (scenario_id, purchase_order_id, reconciliation_version),
            ).fetchall()
        return {
            "order": order,
            "state_history": self._rows(state_history),
            "metric_history": self._rows(metric_history),
            "decisions": self._rows(decisions),
        }

    def list_providers(
        self,
        scenario_id: str | None = None,
    ) -> list[dict[str, object]]:
        groups: dict[tuple[str, str], list[dict[str, object]]] = {}
        for order in self.list_orders(scenario_id):
            key = (
                str(order["provider_profile_id"]),
                str(order["source_provider_id"]),
            )
            groups.setdefault(key, []).append(order)
        return [
            {
                "provider_profile_id": profile_id,
                "source_provider_id": source_id,
                "orders": orders,
            }
            for (profile_id, source_id), orders in sorted(groups.items())
        ]

    def list_customer_orders(
        self,
        scenario_id: str | None = None,
    ) -> list[dict[str, object]]:
        provider_states = self.list_orders(scenario_id)
        with self._connection() as connection:
            has_registry = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'registered_plans'"
            ).fetchone()
            if has_registry is None:
                return []
            plans = self._rows(
                connection.execute(
                    "SELECT * FROM registered_plans ORDER BY order_id"
                ).fetchall()
            )
            planned_orders = self._rows(
                connection.execute(
                    """
                    SELECT * FROM planned_provider_orders
                    ORDER BY customer_order_id, provider_order_id
                    """
                ).fetchall()
            )
            dependencies = self._rows(
                connection.execute(
                    """
                    SELECT dependency.provider_order_id,
                           dependency.depends_on_provider_order_id,
                           planned.customer_order_id
                    FROM planned_provider_order_dependencies AS dependency
                    JOIN planned_provider_orders AS planned
                      ON planned.provider_order_id = dependency.provider_order_id
                    ORDER BY planned.customer_order_id,
                             dependency.provider_order_id,
                             dependency.depends_on_provider_order_id
                    """
                ).fetchall()
            )
        state_by_provider: dict[str, list[dict[str, object]]] = {}
        for state in provider_states:
            state_by_provider.setdefault(
                str(state["purchase_order_id"]), []
            ).append(state)
        summaries: list[dict[str, object]] = []
        for plan in plans:
            order_id = str(plan["order_id"])
            plan_orders = [
                order
                for order in planned_orders
                if order["customer_order_id"] == order_id
            ]
            plan_dependencies = [
                dependency
                for dependency in dependencies
                if dependency["customer_order_id"] == order_id
            ]
            states = [
                state
                for order in plan_orders
                for state in state_by_provider.get(
                    str(order["provider_order_id"]), []
                )
            ]
            scenarios = sorted(
                {str(state["scenario_id"]) for state in states}
            )
            if scenario_id:
                scenarios = [scenario_id] if scenario_id in scenarios else []
            elif not scenarios:
                scenarios = [None]
            for projection_scenario in scenarios:
                scenario_states = [
                    state
                    for state in states
                    if state["scenario_id"] == projection_scenario
                ]
                summaries.append(
                    build_customer_order_projection(
                        plan,
                        plan_orders,
                        plan_dependencies,
                        scenario_states,
                        projection_scenario,
                    )
                )
        return sorted(
            summaries,
            key=lambda item: (
                str(item["order_id"]),
                str(item["scenario_id"] or ""),
            ),
        )

    def get_customer_order(
        self,
        order_id: str,
        scenario_id: str | None = None,
    ) -> dict[str, object]:
        matches = [
            summary
            for summary in self.list_customer_orders(scenario_id)
            if summary["order_id"] == order_id
        ]
        if not matches:
            raise CustomerOrderNotFoundError(
                f"customer order {order_id!r} does not exist"
            )
        if len(matches) > 1:
            raise CustomerOrderAmbiguousError(
                f"customer order {order_id!r} has multiple execution scenarios; "
                "provide scenario_id"
            )
        summary = matches[0]
        selected_scenario = summary["scenario_id"]
        states = {
            str(state["purchase_order_id"]): state
            for state in self.list_orders(
                str(selected_scenario) if selected_scenario is not None else None
            )
        }
        with self._connection() as connection:
            planned_orders = self._rows(
                connection.execute(
                    """
                    SELECT * FROM planned_provider_orders
                    WHERE customer_order_id = ?
                    ORDER BY provider_order_id
                    """,
                    (order_id,),
                ).fetchall()
            )
            dependencies = self._rows(
                connection.execute(
                    """
                    SELECT provider_order_id, depends_on_provider_order_id
                    FROM planned_provider_order_dependencies
                    WHERE provider_order_id IN (
                        SELECT provider_order_id FROM planned_provider_orders
                        WHERE customer_order_id = ?
                    )
                    ORDER BY provider_order_id, depends_on_provider_order_id
                    """,
                    (order_id,),
                ).fetchall()
            )
            has_pipeline = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'pipeline_runs'"
            ).fetchone()
            pipeline_runs: list[dict[str, object]] = []
            if has_pipeline is not None:
                run_rows = self._rows(
                    connection.execute(
                        """
                        SELECT run.*, event.status, event.recorded_at AS latest_event_at,
                               event.error
                        FROM pipeline_runs AS run
                        JOIN pipeline_run_events AS event
                          ON event.pipeline_run_id = run.pipeline_run_id
                         AND event.event_sequence = (
                            SELECT MAX(candidate.event_sequence)
                            FROM pipeline_run_events AS candidate
                            WHERE candidate.pipeline_run_id = run.pipeline_run_id
                         )
                        WHERE run.customer_order_id = ?
                          AND (? IS NULL OR run.scenario_id = ?)
                        ORDER BY run.created_at DESC
                        """,
                        (order_id, selected_scenario, selected_scenario),
                    ).fetchall()
                )
                for run in run_rows:
                    stages = self._rows(
                        connection.execute(
                            """
                            WITH latest_attempt AS (
                                SELECT stage_name, MAX(attempt_number) AS attempt_number
                                FROM pipeline_stage_attempts
                                WHERE pipeline_run_id = ?
                                GROUP BY stage_name
                            )
                            SELECT attempt.stage_name, attempt.attempt_number,
                                   COALESCE(event.status, 'RUNNING') AS status,
                                   attempt.started_at,
                                   event.recorded_at AS completed_at,
                                   event.duration_ms, event.error
                            FROM latest_attempt
                            JOIN pipeline_stage_attempts AS attempt
                              ON attempt.pipeline_run_id = ?
                             AND attempt.stage_name = latest_attempt.stage_name
                             AND attempt.attempt_number = latest_attempt.attempt_number
                            LEFT JOIN pipeline_stage_events AS event
                              ON event.stage_attempt_id = attempt.stage_attempt_id
                            ORDER BY CASE attempt.stage_name
                                WHEN 'register_plan' THEN 1
                                WHEN 'simulate' THEN 2
                                WHEN 'ingest' THEN 3
                                WHEN 'extract' THEN 4
                                WHEN 'reconcile' THEN 5
                                WHEN 'analyze' THEN 6
                                ELSE 99 END
                            """,
                            (run["pipeline_run_id"], run["pipeline_run_id"]),
                        ).fetchall()
                    )
                    pipeline_runs.append(
                        {
                            "pipeline_run_id": run["pipeline_run_id"],
                            "customer_order_id": run["customer_order_id"],
                            "scenario_id": run["scenario_id"],
                            "status": run["status"],
                            "created_at": run["created_at"],
                            "latest_event_at": run["latest_event_at"],
                            "error": run["error"],
                            "stages": stages,
                        }
                    )
        downstream_ids = {
            str(item["depends_on_provider_order_id"]) for item in dependencies
        }
        dependency_map: dict[str, list[str]] = {}
        for item in dependencies:
            dependency_map.setdefault(str(item["provider_order_id"]), []).append(
                str(item["depends_on_provider_order_id"])
            )
        provider_orders = []
        for planned in planned_orders:
            provider_order_id = str(planned["provider_order_id"])
            provider_orders.append(
                {
                    **planned,
                    "terminal": provider_order_id not in downstream_ids,
                    "depends_on_provider_order_ids": dependency_map.get(
                        provider_order_id, []
                    ),
                    "execution": states.get(provider_order_id),
                }
            )
        return {
            "customer_order": summary,
            "provider_orders": provider_orders,
            "dependencies": dependencies,
            "pipeline_runs": pipeline_runs,
        }
