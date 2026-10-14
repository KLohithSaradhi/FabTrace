from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from fabtrace.extraction import (
    EvaluationError,
    ExtractionError,
    evaluate_extraction,
    extract_provider_updates,
)
from fabtrace.ingestion import ingest_provider_updates
from fabtrace.simulation.runner import run_simulation

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "Data"
SCENARIO_PATH = ROOT / ".fabtrace/config/scenarios/stochastic_disruption.json"
HAPPY_SCENARIO_PATH = ROOT / ".fabtrace/config/scenarios/happy_path.json"
FAILURE_SCENARIO_PATH = ROOT / ".fabtrace/config/scenarios/equipment_failure.json"


def _run_pipeline(
    tmp_path: Path,
    scenario_path: Path = SCENARIO_PATH,
) -> tuple[Path, Path, Path]:
    simulation = run_simulation(
        scenario_path,
        DATA_DIR,
        tmp_path / "runs",
        ("fabtrace", "simulate"),
    )
    updates_path = simulation.run_directory / "provider_updates.jsonl"
    truth_path = simulation.run_directory / "ground_truth.jsonl"
    database_path = tmp_path / "fabtrace.db"
    ingest_provider_updates(updates_path, database_path)
    return database_path, updates_path, truth_path


def _message(message_id: str, subject: str, body: str) -> dict:
    return {
        "schema_version": "1.0",
        "generator_version": "0.1.0",
        "synthetic": True,
        "scenario_id": "edge-cases",
        "message_id": message_id,
        "source_event_id": f"event-{message_id}",
        "provider_profile_id": "P208-synthetic-v1",
        "source_provider_id": "P208",
        "purchase_order_id": "PO-0001",
        "channel": "email",
        "sent_at": "2026-01-05T00:00:00Z",
        "received_at": "2026-01-05T00:00:00Z",
        "subject": subject,
        "body": body,
    }


def test_rules_v2_extracts_versioned_claims_with_exact_spans(tmp_path: Path) -> None:
    database_path, _, _ = _run_pipeline(tmp_path)

    result = extract_provider_updates(database_path, "stochastic-disruption")

    assert result.selected_count == 13
    assert result.extracted_count == 13
    assert result.abstained_count == 0
    assert result.review_count == 0
    assert result.failed_count == 0
    assert result.skipped_count == 0
    assert result.claim_count == 42

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        claims = connection.execute(
            """
            SELECT c.*, m.raw_payload
            FROM extracted_claims AS c
            JOIN raw_provider_messages AS m ON m.message_id = c.message_id
            ORDER BY c.message_id, c.claim_index
            """
        ).fetchall()
    assert len(claims) == 42
    for claim in claims:
        message = json.loads(claim["raw_payload"])
        source = message[claim["source_field"]]
        assert source[claim["span_start"] : claim["span_end"]] == claim["source_text"]
        assert claim["confidence"] == 1.0
        assert claim["validation_status"] == "valid"
        assert claim["extractor_version"] == "rules-v2"
        assert sum(
            value is not None
            for value in (
                claim["value_text"],
                claim["value_number"],
                claim["value_date"],
            )
        ) == 1


def test_rules_v1_remains_available_as_an_append_only_baseline(
    tmp_path: Path,
) -> None:
    database_path, _, _ = _run_pipeline(tmp_path)
    extract_provider_updates(database_path, "stochastic-disruption")

    result = extract_provider_updates(
        database_path,
        "stochastic-disruption",
        "rules-v1",
    )

    assert result.extractor_version == "rules-v1"
    assert result.extracted_count == 13
    with sqlite3.connect(database_path) as connection:
        versions = connection.execute(
            "SELECT extractor_version, COUNT(*) FROM extracted_claims "
            "GROUP BY extractor_version ORDER BY extractor_version"
        ).fetchall()
    assert versions == [("rules-v1", 42), ("rules-v2", 42)]


def test_rules_v2_replay_is_idempotent(tmp_path: Path) -> None:
    database_path, _, _ = _run_pipeline(tmp_path)
    first = extract_provider_updates(database_path, "stochastic-disruption")
    second = extract_provider_updates(database_path, "stochastic-disruption")

    assert first.claim_count == 42
    assert second.selected_count == 13
    assert second.extracted_count == 0
    assert second.skipped_count == 13
    assert second.claim_count == 0
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM extracted_claims"
        ).fetchone()[0] == 42
        assert connection.execute(
            "SELECT COUNT(*) FROM extraction_runs"
        ).fetchone()[0] == 2


def test_rules_abstain_or_require_review_for_unsafe_text(tmp_path: Path) -> None:
    messages = [
        _message(
            "MSG-MULTIPLE",
            "Production progress",
            "PO-0001 and PO-0002 are both 25% complete.",
        ),
        _message(
            "MSG-CONFLICT",
            "Production progress",
            "PO-0002 is 25% complete (25 of 100 wafers).",
        ),
        _message(
            "MSG-INVALID",
            "Production progress",
            "PO-0001 is 125% complete (125 of 100 wafers).",
        ),
        _message(
            "MSG-VAGUE",
            "Production resumed",
            "Production for PO-0001 should resume early next week.",
        ),
        _message(
            "MSG-NEGATED",
            "Quality check passed",
            "PO-0001 did not pass quality checks and is not ready to ship.",
        ),
        _message(
            "MSG-MISSING",
            "Order delivered",
            "Delivery was completed today.",
        ),
    ]
    input_path = tmp_path / "provider_updates.jsonl"
    input_path.write_text(
        "".join(json.dumps(message) + "\n" for message in messages),
        encoding="utf-8",
    )
    database_path = tmp_path / "fabtrace.db"
    ingest_provider_updates(input_path, database_path)

    result = extract_provider_updates(database_path, "edge-cases")

    assert result.review_count == 3
    assert result.abstained_count == 3
    assert result.extracted_count == 0
    assert result.claim_count == 0
    with sqlite3.connect(database_path) as connection:
        attempts = dict(
            connection.execute(
                "SELECT message_id, status FROM extraction_attempts"
            ).fetchall()
        )
        resolutions = dict(
            connection.execute(
                "SELECT message_id, resolution_status FROM extraction_attempts"
            ).fetchall()
        )
    assert attempts["MSG-MULTIPLE"] == "review_required"
    assert attempts["MSG-CONFLICT"] == "review_required"
    assert attempts["MSG-INVALID"] == "review_required"
    assert attempts["MSG-VAGUE"] == "abstained"
    assert attempts["MSG-NEGATED"] == "abstained"
    assert attempts["MSG-MISSING"] == "abstained"
    assert resolutions["MSG-MULTIPLE"] == "ambiguous"
    assert resolutions["MSG-CONFLICT"] == "conflict"
    assert resolutions["MSG-INVALID"] == "resolved"
    assert resolutions["MSG-MISSING"] == "missing"


def test_evaluation_is_separate_and_reports_exact_field_metrics(
    tmp_path: Path,
) -> None:
    database_path, _, truth_path = _run_pipeline(tmp_path)
    extract_provider_updates(database_path, "stochastic-disruption")

    result = evaluate_extraction(
        database_path,
        truth_path,
        "stochastic-disruption",
    )

    assert result.message_count == 13
    assert result.abstained_count == 0
    assert result.review_count == 0
    assert result.expected_count == 42
    assert result.correct_count == 42
    assert all(metric.precision == 1.0 for metric in result.metrics)
    assert all(metric.recall == 1.0 for metric in result.metrics)


@pytest.mark.parametrize(
    ("scenario_path", "scenario_id", "message_count", "claim_count"),
    [
        (HAPPY_SCENARIO_PATH, "happy-path", 11, 35),
        (FAILURE_SCENARIO_PATH, "equipment-failure", 13, 42),
    ],
)
def test_rules_cover_other_committed_scenarios(
    tmp_path: Path,
    scenario_path: Path,
    scenario_id: str,
    message_count: int,
    claim_count: int,
) -> None:
    database_path, _, truth_path = _run_pipeline(tmp_path, scenario_path)

    extraction = extract_provider_updates(database_path, scenario_id)
    evaluation = evaluate_extraction(database_path, truth_path, scenario_id)

    assert extraction.extracted_count == message_count
    assert extraction.claim_count == claim_count
    assert evaluation.correct_count == claim_count
    assert evaluation.expected_count == claim_count


def test_evaluation_detects_an_incorrect_extracted_value(tmp_path: Path) -> None:
    database_path, _, truth_path = _run_pipeline(tmp_path)
    extract_provider_updates(database_path, "stochastic-disruption")
    altered_path = tmp_path / "altered_ground_truth.jsonl"
    altered_events = []
    changed = False
    for line in truth_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event["event_type"] == "progress_updated" and not changed:
            event["progress_pct"] = 20.0
            changed = True
        altered_events.append(event)
    altered_path.write_text(
        "".join(json.dumps(event) + "\n" for event in altered_events),
        encoding="utf-8",
    )

    result = evaluate_extraction(
        database_path,
        altered_path,
        "stochastic-disruption",
    )
    progress = next(
        metric for metric in result.metrics if metric.claim_type == "progress_pct"
    )
    assert progress.expected_count == 4
    assert progress.correct_count == 3
    assert progress.incorrect_count == 1
    assert result.correct_count == 41


def test_extraction_tables_are_append_only(tmp_path: Path) -> None:
    database_path, _, _ = _run_pipeline(tmp_path)
    extract_provider_updates(database_path, "stochastic-disruption")

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE extracted_claims SET confidence = 0.5")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM extraction_attempts")


def test_schema_version_one_database_is_migrated(tmp_path: Path) -> None:
    database_path, _, _ = _run_pipeline(tmp_path)
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            DROP TABLE extracted_claims;
            DROP TABLE extraction_attempts;
            DROP TABLE extraction_runs;
            PRAGMA user_version = 1;
            """
        )

    result = extract_provider_updates(database_path, "stochastic-disruption")

    assert result.extracted_count == 13
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6


def test_extraction_requires_an_existing_bounded_scenario(tmp_path: Path) -> None:
    with pytest.raises(ExtractionError, match="does not exist"):
        extract_provider_updates(tmp_path / "missing.db", "scenario")

    database_path, _, truth_path = _run_pipeline(tmp_path)
    with pytest.raises(ExtractionError, match="no accepted provider messages"):
        extract_provider_updates(database_path, "missing-scenario")
    with pytest.raises(EvaluationError, match="without completed"):
        evaluate_extraction(database_path, truth_path, "stochastic-disruption")
