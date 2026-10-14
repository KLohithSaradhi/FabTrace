from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from fabtrace.extraction import extract_provider_updates
from fabtrace.ingestion import ingest_provider_updates
from fabtrace.reconciliation import ReconciliationError, reconcile_order_state
from fabtrace.simulation.runner import run_simulation

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "Data"
STOCHASTIC_SCENARIO = ROOT / ".fabtrace/config/scenarios/stochastic_disruption.json"
HAPPY_SCENARIO = ROOT / ".fabtrace/config/scenarios/happy_path.json"
FAILURE_SCENARIO = ROOT / ".fabtrace/config/scenarios/equipment_failure.json"


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
    return database_path, scenario_id


def _message(
    message_id: str,
    *,
    scenario_id: str,
    subject: str,
    body: str,
    sent_at: str,
    received_at: str,
) -> dict:
    return {
        "schema_version": "1.0",
        "generator_version": "0.1.0",
        "synthetic": True,
        "scenario_id": scenario_id,
        "message_id": message_id,
        "source_event_id": f"event-{message_id}",
        "provider_profile_id": "P208-synthetic-v1",
        "source_provider_id": "P208",
        "purchase_order_id": "PO-0001",
        "channel": "email",
        "sent_at": sent_at,
        "received_at": received_at,
        "subject": subject,
        "body": body,
    }


def _ingest_messages(
    database_path: Path,
    input_path: Path,
    messages: list[dict],
) -> None:
    input_path.write_text(
        "".join(json.dumps(message) + "\n" for message in messages),
        encoding="utf-8",
    )
    ingest_provider_updates(input_path, database_path)


def test_reconciliation_builds_expected_current_state_with_provenance(
    tmp_path: Path,
) -> None:
    database_path, scenario_id = _run_pipeline(tmp_path)

    result = reconcile_order_state(database_path, scenario_id)

    assert result.selected_count == 13
    assert result.applied_count == 13
    assert result.stale_count == 0
    assert result.conflict_count == 0
    assert result.snapshot_count == 13
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        current = connection.execute(
            "SELECT * FROM current_order_state"
        ).fetchone()
        sequences = connection.execute(
            """
            SELECT snapshot_sequence FROM order_state_snapshots
            ORDER BY snapshot_sequence
            """
        ).fetchall()

    assert current["purchase_order_id"] == "PO-0001"
    assert current["order_status"] == "DELIVERED"
    assert current["progress_pct"] == 100.0
    assert current["quantity_completed"] == 100.0
    assert current["quantity_ordered"] == 100.0
    assert current["quantity_unit"] == "wafer"
    assert current["forecast_delivery_date"] == "2026-01-16"
    assert current["delay_days"] == 3.0
    assert current["incident_reason"] == "equipment_calibration"
    assert current["milestone_date"] == "2026-01-16"
    assert current["snapshot_sequence"] == 13
    assert current["as_of_received_at"] == "2026-01-16T00:00:00+00:00"
    assert current["order_status_source_message_id"].endswith("MSG-0013")
    assert current["progress_pct_source_message_id"].endswith("MSG-0009")
    assert current["quantity_ordered_source_message_id"].endswith("MSG-0001")
    assert current["forecast_delivery_date_source_message_id"].endswith("MSG-0006")
    assert current["delay_days_source_message_id"].endswith("MSG-0006")
    assert [row[0] for row in sequences] == list(range(1, 14))


def test_reconciliation_replay_is_idempotent(tmp_path: Path) -> None:
    database_path, scenario_id = _run_pipeline(tmp_path)
    first = reconcile_order_state(database_path, scenario_id)
    second = reconcile_order_state(database_path, scenario_id)

    assert first.snapshot_count == 13
    assert second.selected_count == 13
    assert second.skipped_count == 13
    assert second.snapshot_count == 0
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM order_state_snapshots"
        ).fetchone()[0] == 13
        assert connection.execute(
            "SELECT COUNT(*) FROM reconciliation_runs"
        ).fetchone()[0] == 2


@pytest.mark.parametrize(
    ("scenario_path", "status", "forecast", "delay", "snapshots"),
    [
        (HAPPY_SCENARIO, "DELIVERED", "2026-01-13", None, 11),
        (FAILURE_SCENARIO, "DELIVERED", "2026-01-15", 2.0, 13),
    ],
)
def test_reconciliation_covers_other_committed_scenarios(
    tmp_path: Path,
    scenario_path: Path,
    status: str,
    forecast: str,
    delay: float | None,
    snapshots: int,
) -> None:
    database_path, scenario_id = _run_pipeline(tmp_path, scenario_path)

    result = reconcile_order_state(database_path, scenario_id)

    assert result.applied_count == snapshots
    assert result.conflict_count == 0
    with sqlite3.connect(database_path) as connection:
        current = connection.execute(
            """
            SELECT order_status, forecast_delivery_date, delay_days
            FROM current_order_state
            """
        ).fetchone()
    assert current == (status, forecast, delay)


def test_late_older_update_is_recorded_as_stale_without_regression(
    tmp_path: Path,
) -> None:
    database_path, scenario_id = _run_pipeline(tmp_path, HAPPY_SCENARIO)
    reconcile_order_state(database_path, scenario_id)
    late = _message(
        "happy-path:PO-0001:MSG-LATE",
        scenario_id=scenario_id,
        subject="Production progress",
        body="PO-0001 is 50% complete (50 of 100 wafers).",
        sent_at="2026-01-08T00:00:00Z",
        received_at="2026-01-20T00:00:00Z",
    )
    _ingest_messages(database_path, tmp_path / "late.jsonl", [late])
    extract_provider_updates(database_path, scenario_id)

    result = reconcile_order_state(database_path, scenario_id)

    assert result.skipped_count == 11
    assert result.stale_count == 1
    assert result.snapshot_count == 0
    with sqlite3.connect(database_path) as connection:
        decision = connection.execute(
            """
            SELECT decision FROM reconciliation_decisions
            WHERE message_id = ?
            """,
            (late["message_id"],),
        ).fetchone()[0]
        current = connection.execute(
            "SELECT order_status, progress_pct FROM current_order_state"
        ).fetchone()
    assert decision == "stale"
    assert current == ("DELIVERED", 100.0)


def test_illegal_status_transition_is_an_atomic_conflict(tmp_path: Path) -> None:
    database_path, scenario_id = _run_pipeline(tmp_path, HAPPY_SCENARIO)
    reconcile_order_state(database_path, scenario_id)
    regression = _message(
        "happy-path:PO-0001:MSG-REGRESSION",
        scenario_id=scenario_id,
        subject="Production started",
        body="Production for PO-0001 started on 2026-01-20.",
        sent_at="2026-01-20T00:00:00Z",
        received_at="2026-01-20T00:00:00Z",
    )
    _ingest_messages(database_path, tmp_path / "regression.jsonl", [regression])
    extract_provider_updates(database_path, scenario_id)

    result = reconcile_order_state(database_path, scenario_id)

    assert result.conflict_count == 1
    assert result.snapshot_count == 0
    with sqlite3.connect(database_path) as connection:
        decision = connection.execute(
            """
            SELECT decision, detail FROM reconciliation_decisions
            WHERE message_id = ?
            """,
            (regression["message_id"],),
        ).fetchone()
        current = connection.execute(
            "SELECT order_status, milestone_date FROM current_order_state"
        ).fetchone()
    assert decision[0] == "conflict"
    assert "illegal order status transition" in decision[1]
    assert current == ("DELIVERED", "2026-01-13")


def test_same_time_disagreement_is_a_conflict(tmp_path: Path) -> None:
    scenario_id = "same-time-conflict"
    database_path = tmp_path / "fabtrace.db"
    messages = [
        _message(
            f"{scenario_id}:PO-0001:MSG-0001",
            scenario_id=scenario_id,
            subject="Order acknowledged",
            body=(
                "We have acknowledged PO-0001 for 100 wafers. "
                "Expected delivery remains 2026-01-13."
            ),
            sent_at="2026-01-05T00:00:00Z",
            received_at="2026-01-05T00:00:00Z",
        ),
        _message(
            f"{scenario_id}:PO-0001:MSG-0002",
            scenario_id=scenario_id,
            subject="Order acknowledged",
            body=(
                "We have acknowledged PO-0001 for 100 wafers. "
                "Expected delivery remains 2026-01-14."
            ),
            sent_at="2026-01-05T00:00:00Z",
            received_at="2026-01-06T00:00:00Z",
        ),
    ]
    _ingest_messages(database_path, tmp_path / "conflict.jsonl", messages)
    extract_provider_updates(database_path, scenario_id)

    result = reconcile_order_state(database_path, scenario_id)

    assert result.applied_count == 1
    assert result.conflict_count == 1
    with sqlite3.connect(database_path) as connection:
        current = connection.execute(
            "SELECT forecast_delivery_date, snapshot_sequence FROM current_order_state"
        ).fetchone()
    assert current == ("2026-01-13", 1)


def test_progress_regression_is_a_conflict(tmp_path: Path) -> None:
    scenario_id = "progress-regression"
    database_path = tmp_path / "fabtrace.db"
    message_specs = [
        (
            "Order acknowledged",
            "We have acknowledged PO-0001 for 100 wafers. "
            "Expected delivery remains 2026-01-13.",
            "2026-01-05T00:00:00Z",
        ),
        (
            "Order queued",
            "PO-0001 entered our production queue on 2026-01-05.",
            "2026-01-05T00:00:00Z",
        ),
        (
            "Production started",
            "Production for PO-0001 started on 2026-01-06.",
            "2026-01-06T00:00:00Z",
        ),
        (
            "Production progress",
            "PO-0001 is 50% complete (50 of 100 wafers).",
            "2026-01-07T00:00:00Z",
        ),
        (
            "Production progress",
            "PO-0001 is 25% complete (25 of 100 wafers).",
            "2026-01-08T00:00:00Z",
        ),
    ]
    messages = [
        _message(
            f"{scenario_id}:PO-0001:MSG-{index:04d}",
            scenario_id=scenario_id,
            subject=subject,
            body=body,
            sent_at=timestamp,
            received_at=timestamp,
        )
        for index, (subject, body, timestamp) in enumerate(message_specs, start=1)
    ]
    _ingest_messages(database_path, tmp_path / "regression.jsonl", messages)
    extract_provider_updates(database_path, scenario_id)

    result = reconcile_order_state(database_path, scenario_id)

    assert result.applied_count == 4
    assert result.conflict_count == 1
    with sqlite3.connect(database_path) as connection:
        current = connection.execute(
            """
            SELECT order_status, progress_pct, quantity_completed
            FROM current_order_state
            """
        ).fetchone()
    assert current == ("IN_PRODUCTION", 50.0, 50.0)


def test_unresolved_extraction_becomes_a_reconciliation_conflict(
    tmp_path: Path,
) -> None:
    scenario_id = "unresolved"
    database_path = tmp_path / "fabtrace.db"
    vague = _message(
        f"{scenario_id}:PO-0001:MSG-0001",
        scenario_id=scenario_id,
        subject="Production resumed",
        body="Production for PO-0001 should resume sometime next week.",
        sent_at="2026-01-05T00:00:00Z",
        received_at="2026-01-05T00:00:00Z",
    )
    _ingest_messages(database_path, tmp_path / "vague.jsonl", [vague])
    extraction = extract_provider_updates(database_path, scenario_id)
    assert extraction.abstained_count == 1

    result = reconcile_order_state(database_path, scenario_id)

    assert result.conflict_count == 1
    assert result.snapshot_count == 0
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM current_order_state"
        ).fetchone()[0] == 0


def test_reconciliation_tables_are_append_only(tmp_path: Path) -> None:
    database_path, scenario_id = _run_pipeline(tmp_path)
    reconcile_order_state(database_path, scenario_id)

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE reconciliation_decisions SET decision = 'stale'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM order_state_snapshots")


def test_schema_version_two_database_is_migrated(tmp_path: Path) -> None:
    database_path, scenario_id = _run_pipeline(tmp_path)
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            DROP VIEW current_order_state;
            DROP TABLE order_state_snapshots;
            DROP TABLE reconciliation_decisions;
            DROP TABLE reconciliation_runs;
            PRAGMA user_version = 2;
            """
        )

    result = reconcile_order_state(database_path, scenario_id)

    assert result.snapshot_count == 13
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6


def test_reconciliation_requires_existing_bounded_input(tmp_path: Path) -> None:
    with pytest.raises(ReconciliationError, match="does not exist"):
        reconcile_order_state(tmp_path / "missing.db", "scenario")

    database_path, _ = _run_pipeline(tmp_path)
    with pytest.raises(ReconciliationError, match="no accepted provider messages"):
        reconcile_order_state(database_path, "missing-scenario")
