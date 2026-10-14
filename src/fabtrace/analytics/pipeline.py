from __future__ import annotations

import hashlib
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from fabtrace.reconciliation import RECONCILIATION_VERSION
from fabtrace.storage import StorageSchemaError, initialize_database

METRIC_VERSION = "operational-metrics-v1"
SECONDS_PER_DAY = 86_400


class AnalyticsError(RuntimeError):
    """Raised when a bounded operational-analytics run cannot complete."""


@dataclass(frozen=True)
class AnalyticsResult:
    analytics_run_id: str
    database_path: Path
    scenario_id: str
    reconciliation_version: str
    metric_version: str
    as_of_time: str
    selected_order_count: int
    computed_count: int
    skipped_count: int


@dataclass(frozen=True)
class OrderMetrics:
    source_snapshot_id: str
    scenario_id: str
    purchase_order_id: str
    provider_profile_id: str
    source_provider_id: str
    order_status: str
    progress_pct: float | None
    initial_forecast_date: str | None
    current_forecast_date: str | None
    forecast_drift_days: int | None
    forecast_revision_count: int
    days_to_forecast: int | None
    reported_delay_days: float | None
    has_reported_delay: bool
    progress_velocity_pct_per_day: float | None
    latest_update_age_days: float
    communication_lag_days: float
    applied_message_count: int
    stale_message_count: int
    conflict_message_count: int


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AnalyticsError(f"invalid timestamp {value!r}: {exc}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AnalyticsError(f"timestamp must include an offset: {value!r}")
    return parsed


def _normalize_as_of(value: str | datetime) -> tuple[datetime, str]:
    parsed = _parse_time(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AnalyticsError("as_of must include a timezone offset")
    utc_value = parsed.astimezone(UTC)
    return utc_value, utc_value.isoformat()


def _metric_snapshot_id(
    scenario_id: str,
    purchase_order_id: str,
    as_of_time: str,
) -> str:
    material = "|".join(
        (
            scenario_id,
            purchase_order_id,
            METRIC_VERSION,
            RECONCILIATION_VERSION,
            as_of_time,
        )
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _forecast_metrics(
    snapshots: list[sqlite3.Row],
    as_of: datetime,
) -> tuple[str | None, str | None, int | None, int, int | None]:
    revisions: list[str] = []
    for snapshot in snapshots:
        forecast = snapshot["forecast_delivery_date"]
        if forecast is not None and (not revisions or forecast != revisions[-1]):
            revisions.append(forecast)
    initial = revisions[0] if revisions else None
    current = snapshots[-1]["forecast_delivery_date"]
    drift = None
    if initial is not None and current is not None:
        drift = (date.fromisoformat(current) - date.fromisoformat(initial)).days
    days_to_forecast = None
    if current is not None:
        days_to_forecast = (date.fromisoformat(current) - as_of.date()).days
    return initial, current, drift, max(0, len(revisions) - 1), days_to_forecast


def _progress_velocity(snapshots: list[sqlite3.Row]) -> float | None:
    observations: list[tuple[str, float, datetime]] = []
    seen_sources: set[str] = set()
    for snapshot in snapshots:
        source = snapshot["progress_pct_source_message_id"]
        progress = snapshot["progress_pct"]
        effective_at = snapshot["progress_pct_effective_at"]
        if source is None or progress is None or effective_at is None:
            continue
        if source in seen_sources:
            continue
        seen_sources.add(source)
        observations.append((source, float(progress), _parse_time(effective_at)))
    if len(observations) < 2:
        return None
    _, previous_progress, previous_time = observations[-2]
    _, current_progress, current_time = observations[-1]
    elapsed_days = (current_time - previous_time).total_seconds() / SECONDS_PER_DAY
    if elapsed_days <= 0:
        return None
    return (current_progress - previous_progress) / elapsed_days


def _decision_counts(
    rows: list[sqlite3.Row],
    as_of: datetime,
) -> tuple[int, int, int]:
    counts = {"applied": 0, "stale": 0, "conflict": 0}
    for row in rows:
        if _parse_time(row["received_at"]) <= as_of:
            counts[row["decision"]] += 1
    return counts["applied"], counts["stale"], counts["conflict"]


def _compute_order_metrics(
    snapshots: list[sqlite3.Row],
    decisions: list[sqlite3.Row],
    as_of: datetime,
) -> OrderMetrics:
    current = snapshots[-1]
    current_received_at = _parse_time(current["as_of_received_at"])
    current_sent_at = _parse_time(current["effective_through_sent_at"])
    latest_age = (as_of - current_received_at).total_seconds() / SECONDS_PER_DAY
    communication_lag = (
        current_received_at - current_sent_at
    ).total_seconds() / SECONDS_PER_DAY
    if latest_age < 0 or communication_lag < 0:
        raise AnalyticsError("snapshot time ordering produced a negative age or lag")

    initial, current_forecast, drift, revision_count, days_to_forecast = (
        _forecast_metrics(snapshots, as_of)
    )
    applied, stale, conflict = _decision_counts(decisions, as_of)
    reported_delay = current["delay_days"]
    order_status = current["order_status"]
    if order_status is None:
        raise AnalyticsError(
            f"snapshot {current['snapshot_id']} has no reconciled order status"
        )
    return OrderMetrics(
        source_snapshot_id=current["snapshot_id"],
        scenario_id=current["scenario_id"],
        purchase_order_id=current["purchase_order_id"],
        provider_profile_id=current["provider_profile_id"],
        source_provider_id=current["source_provider_id"],
        order_status=order_status,
        progress_pct=(
            float(current["progress_pct"])
            if current["progress_pct"] is not None
            else None
        ),
        initial_forecast_date=initial,
        current_forecast_date=current_forecast,
        forecast_drift_days=drift,
        forecast_revision_count=revision_count,
        days_to_forecast=days_to_forecast,
        reported_delay_days=(
            float(reported_delay) if reported_delay is not None else None
        ),
        has_reported_delay=reported_delay is not None,
        progress_velocity_pct_per_day=_progress_velocity(snapshots),
        latest_update_age_days=latest_age,
        communication_lag_days=communication_lag,
        applied_message_count=applied,
        stale_message_count=stale,
        conflict_message_count=conflict,
    )


def _store_metric_snapshot(
    connection: sqlite3.Connection,
    *,
    analytics_run_id: str,
    as_of_time: str,
    metrics: OrderMetrics,
) -> None:
    connection.execute(
        """
        INSERT INTO order_metric_snapshots (
            metric_snapshot_id, analytics_run_id, source_snapshot_id,
            metric_version, reconciliation_version, scenario_id,
            purchase_order_id, provider_profile_id, source_provider_id,
            as_of_time, computed_at, order_status, progress_pct,
            initial_forecast_date, current_forecast_date, forecast_drift_days,
            forecast_revision_count, days_to_forecast, reported_delay_days,
            has_reported_delay, progress_velocity_pct_per_day,
            latest_update_age_days, communication_lag_days,
            applied_message_count, stale_message_count, conflict_message_count
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?
        )
        """,
        (
            _metric_snapshot_id(
                metrics.scenario_id,
                metrics.purchase_order_id,
                as_of_time,
            ),
            analytics_run_id,
            metrics.source_snapshot_id,
            METRIC_VERSION,
            RECONCILIATION_VERSION,
            metrics.scenario_id,
            metrics.purchase_order_id,
            metrics.provider_profile_id,
            metrics.source_provider_id,
            as_of_time,
            _utc_now(),
            metrics.order_status,
            metrics.progress_pct,
            metrics.initial_forecast_date,
            metrics.current_forecast_date,
            metrics.forecast_drift_days,
            metrics.forecast_revision_count,
            metrics.days_to_forecast,
            metrics.reported_delay_days,
            int(metrics.has_reported_delay),
            metrics.progress_velocity_pct_per_day,
            metrics.latest_update_age_days,
            metrics.communication_lag_days,
            metrics.applied_message_count,
            metrics.stale_message_count,
            metrics.conflict_message_count,
        ),
    )


def analyze_orders(
    database_path: Path,
    scenario_id: str,
    as_of: str | datetime,
) -> AnalyticsResult:
    database_path = Path(database_path).resolve()
    if not database_path.is_file():
        raise AnalyticsError(f"FabTrace database does not exist: {database_path}")
    if not scenario_id.strip():
        raise AnalyticsError("scenario_id must not be blank")
    as_of_value, as_of_time = _normalize_as_of(as_of)
    analytics_run_id = str(uuid4())
    computed_count = 0
    skipped_count = 0

    try:
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise AnalyticsError(f"cannot open analytics database: {exc}") from exc

    try:
        initialize_database(connection)
        snapshot_rows = connection.execute(
            """
            SELECT * FROM order_state_snapshots
            WHERE scenario_id = ? AND reconciliation_version = ?
            """,
            (scenario_id, RECONCILIATION_VERSION),
        ).fetchall()
        historical_snapshots = [
            row
            for row in snapshot_rows
            if _parse_time(row["as_of_received_at"]) <= as_of_value
        ]
        if not historical_snapshots:
            raise AnalyticsError(
                f"no reconciled order state exists for scenario {scenario_id!r} "
                f"at or before {as_of_time}"
            )

        snapshots_by_order: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in historical_snapshots:
            snapshots_by_order[row["purchase_order_id"]].append(row)
        for rows in snapshots_by_order.values():
            rows.sort(
                key=lambda row: (
                    _parse_time(row["as_of_received_at"]),
                    row["snapshot_sequence"],
                )
            )

        decision_rows = connection.execute(
            """
            SELECT * FROM reconciliation_decisions
            WHERE scenario_id = ? AND reconciliation_version = ?
            """,
            (scenario_id, RECONCILIATION_VERSION),
        ).fetchall()
        decisions_by_order: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in decision_rows:
            decisions_by_order[row["purchase_order_id"]].append(row)

        connection.execute(
            """
            INSERT INTO analytics_runs (
                analytics_run_id, scenario_id, reconciliation_version,
                metric_version, as_of_time, started_at, status,
                selected_order_count
            ) VALUES (?, ?, ?, ?, ?, ?, 'started', ?)
            """,
            (
                analytics_run_id,
                scenario_id,
                RECONCILIATION_VERSION,
                METRIC_VERSION,
                as_of_time,
                _utc_now(),
                len(snapshots_by_order),
            ),
        )

        for purchase_order_id, snapshots in sorted(snapshots_by_order.items()):
            existing = connection.execute(
                """
                SELECT 1 FROM order_metric_snapshots
                WHERE scenario_id = ? AND purchase_order_id = ?
                  AND metric_version = ? AND reconciliation_version = ?
                  AND as_of_time = ?
                """,
                (
                    scenario_id,
                    purchase_order_id,
                    METRIC_VERSION,
                    RECONCILIATION_VERSION,
                    as_of_time,
                ),
            ).fetchone()
            if existing is not None:
                skipped_count += 1
                continue
            metrics = _compute_order_metrics(
                snapshots,
                decisions_by_order[purchase_order_id],
                as_of_value,
            )
            _store_metric_snapshot(
                connection,
                analytics_run_id=analytics_run_id,
                as_of_time=as_of_time,
                metrics=metrics,
            )
            computed_count += 1

        connection.execute(
            """
            UPDATE analytics_runs
            SET completed_at = ?, status = 'completed', computed_count = ?,
                skipped_count = ?
            WHERE analytics_run_id = ?
            """,
            (_utc_now(), computed_count, skipped_count, analytics_run_id),
        )
        connection.commit()
    except (sqlite3.Error, StorageSchemaError) as exc:
        connection.rollback()
        raise AnalyticsError(f"operational analytics failed: {exc}") from exc
    except AnalyticsError:
        connection.rollback()
        raise
    finally:
        connection.close()

    return AnalyticsResult(
        analytics_run_id=analytics_run_id,
        database_path=database_path,
        scenario_id=scenario_id,
        reconciliation_version=RECONCILIATION_VERSION,
        metric_version=METRIC_VERSION,
        as_of_time=as_of_time,
        selected_order_count=len(snapshots_by_order),
        computed_count=computed_count,
        skipped_count=skipped_count,
    )
