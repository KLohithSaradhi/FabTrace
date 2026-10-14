from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from fabtrace.analytics import AnalyticsError, analyze_orders
from fabtrace.extraction import extract_provider_updates
from fabtrace.ingestion import ingest_provider_updates
from fabtrace.reconciliation import reconcile_order_state
from fabtrace.simulation.runner import run_simulation

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "Data"
STOCHASTIC_SCENARIO = (
    ROOT / ".fabtrace/config/scenarios/stochastic_disruption.json"
)
HAPPY_SCENARIO = ROOT / ".fabtrace/config/scenarios/happy_path.json"


def _run_pipeline(
    tmp_path: Path,
    scenario_path: Path = STOCHASTIC_SCENARIO,
) -> tuple[Path, str]:
    simulation = run_simulation(
        scenario_path,
        DATA_DIR,
        tmp_path / "runs",
        ("fabtrace", "simulate"),
    )
    database_path = tmp_path / "fabtrace.db"
    ingest_provider_updates(
        simulation.run_directory / "provider_updates.jsonl",
        database_path,
    )
    scenario_id = simulation.run_directory.name.rsplit("-seed-", maxsplit=1)[0]
    extract_provider_updates(database_path, scenario_id)
    reconcile_order_state(database_path, scenario_id)
    return database_path, scenario_id


def _metric_row(database_path: Path, as_of_time: str) -> sqlite3.Row:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM order_metric_snapshots WHERE as_of_time = ?",
            (as_of_time,),
        ).fetchone()
    assert row is not None
    return row


def test_analytics_computes_expected_stochastic_metrics(tmp_path: Path) -> None:
    database_path, scenario_id = _run_pipeline(tmp_path)

    result = analyze_orders(
        database_path,
        scenario_id,
        "2026-01-16T00:00:00Z",
    )

    assert result.selected_order_count == 1
    assert result.computed_count == 1
    assert result.skipped_count == 0
    assert result.as_of_time == "2026-01-16T00:00:00+00:00"
    row = _metric_row(database_path, result.as_of_time)
    assert row["order_status"] == "DELIVERED"
    assert row["progress_pct"] == 100.0
    assert row["initial_forecast_date"] == "2026-01-13"
    assert row["current_forecast_date"] == "2026-01-16"
    assert row["forecast_drift_days"] == 3
    assert row["forecast_revision_count"] == 1
    assert row["days_to_forecast"] == 0
    assert row["reported_delay_days"] == 3.0
    assert row["has_reported_delay"] == 1
    assert row["progress_velocity_pct_per_day"] == 25.0
    assert row["latest_update_age_days"] == 0.0
    assert row["communication_lag_days"] == 0.0
    assert row["applied_message_count"] == 13
    assert row["stale_message_count"] == 0
    assert row["conflict_message_count"] == 0


def test_historical_as_of_excludes_future_updates(tmp_path: Path) -> None:
    database_path, scenario_id = _run_pipeline(tmp_path)

    result = analyze_orders(
        database_path,
        scenario_id,
        "2026-01-07T00:00:00Z",
    )

    row = _metric_row(database_path, result.as_of_time)
    assert row["order_status"] == "IN_PRODUCTION"
    assert row["progress_pct"] == 25.0
    assert row["initial_forecast_date"] == "2026-01-13"
    assert row["current_forecast_date"] == "2026-01-13"
    assert row["forecast_drift_days"] == 0
    assert row["forecast_revision_count"] == 0
    assert row["days_to_forecast"] == 6
    assert row["reported_delay_days"] is None
    assert row["has_reported_delay"] == 0
    assert row["progress_velocity_pct_per_day"] is None
    assert row["applied_message_count"] == 4


def test_analytics_replay_is_idempotent_and_timezone_normalized(
    tmp_path: Path,
) -> None:
    database_path, scenario_id = _run_pipeline(tmp_path)
    first = analyze_orders(
        database_path,
        scenario_id,
        "2026-01-16T00:00:00Z",
    )
    second = analyze_orders(
        database_path,
        scenario_id,
        "2026-01-15T16:00:00-08:00",
    )

    assert first.computed_count == 1
    assert second.as_of_time == first.as_of_time
    assert second.computed_count == 0
    assert second.skipped_count == 1
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM order_metric_snapshots"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM analytics_runs"
        ).fetchone()[0] == 2


def test_current_metrics_view_selects_latest_as_of(tmp_path: Path) -> None:
    database_path, scenario_id = _run_pipeline(tmp_path)
    analyze_orders(database_path, scenario_id, "2026-01-07T00:00:00Z")
    latest = analyze_orders(
        database_path,
        scenario_id,
        "2026-01-16T00:00:00Z",
    )

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT as_of_time, order_status FROM current_order_metrics"
        ).fetchone()
    assert row == (latest.as_of_time, "DELIVERED")


def test_missing_delay_stays_null_on_happy_path(tmp_path: Path) -> None:
    database_path, scenario_id = _run_pipeline(tmp_path, HAPPY_SCENARIO)
    result = analyze_orders(
        database_path,
        scenario_id,
        "2026-01-13T00:00:00Z",
    )

    row = _metric_row(database_path, result.as_of_time)
    assert row["forecast_drift_days"] == 0
    assert row["forecast_revision_count"] == 0
    assert row["reported_delay_days"] is None
    assert row["has_reported_delay"] == 0


@pytest.mark.parametrize(
    "as_of",
    ["2026-01-16T00:00:00", "not-a-timestamp"],
)
def test_analytics_rejects_invalid_as_of(tmp_path: Path, as_of: str) -> None:
    database_path, scenario_id = _run_pipeline(tmp_path)

    with pytest.raises(AnalyticsError, match="timestamp"):
        analyze_orders(database_path, scenario_id, as_of)


def test_analytics_requires_state_at_or_before_as_of(tmp_path: Path) -> None:
    database_path, scenario_id = _run_pipeline(tmp_path)

    with pytest.raises(AnalyticsError, match="no reconciled order state"):
        analyze_orders(
            database_path,
            scenario_id,
            "2026-01-04T00:00:00Z",
        )


def test_metric_snapshots_are_append_only(tmp_path: Path) -> None:
    database_path, scenario_id = _run_pipeline(tmp_path)
    result = analyze_orders(
        database_path,
        scenario_id,
        "2026-01-16T00:00:00Z",
    )

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                """
                UPDATE order_metric_snapshots SET order_status = 'SHIPPED'
                WHERE as_of_time = ?
                """,
                (result.as_of_time,),
            )


def test_schema_version_three_database_is_migrated(tmp_path: Path) -> None:
    database_path, scenario_id = _run_pipeline(tmp_path)
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            DROP VIEW current_order_metrics;
            DROP TABLE order_metric_snapshots;
            DROP TABLE analytics_runs;
            PRAGMA user_version = 3;
            """
        )

    result = analyze_orders(
        database_path,
        scenario_id,
        "2026-01-16T00:00:00Z",
    )

    assert result.computed_count == 1
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
