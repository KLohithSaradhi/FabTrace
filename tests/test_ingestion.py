from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import fabtrace.ingestion.pipeline as ingestion_pipeline
from fabtrace.ingestion import IngestionError, ingest_provider_updates


def _message(message_id: str, *, sent_at: str = "2026-01-05T00:00:00Z") -> dict:
    return {
        "schema_version": "1.0",
        "generator_version": "0.1.0",
        "synthetic": True,
        "scenario_id": "ingestion-test",
        "message_id": message_id,
        "source_event_id": f"event-{message_id}",
        "provider_profile_id": "P208-synthetic-v1",
        "source_provider_id": "P208",
        "purchase_order_id": "PO-0001",
        "channel": "email",
        "sent_at": sent_at,
        "received_at": sent_at,
        "subject": "Provider update",
        "body": "Production is progressing normally.",
    }


def _line(payload: object) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"


def _write(path: Path, *lines: bytes) -> None:
    path.write_bytes(b"".join(lines))


def test_ingestion_preserves_exact_payload_and_metadata(tmp_path: Path) -> None:
    input_path = tmp_path / "provider_updates.jsonl"
    raw_payload = json.dumps(_message("MSG-001")).encode("utf-8") + b"\n"
    _write(input_path, raw_payload)
    database_path = tmp_path / "state" / "fabtrace.db"

    result = ingest_provider_updates(input_path, database_path)

    assert result.total_lines == 1
    assert result.accepted_count == 1
    assert result.duplicate_count == 0
    assert result.quarantined_count == 0
    with sqlite3.connect(database_path) as connection:
        stored = connection.execute(
            """
            SELECT message_id, raw_payload, source_path, source_line, first_batch_id
            FROM raw_provider_messages
            """
        ).fetchone()
        batch = connection.execute(
            """
            SELECT status, total_lines, accepted_count, duplicate_count,
                   quarantined_count
            FROM ingestion_batches WHERE batch_id = ?
            """,
            (result.batch_id,),
        ).fetchone()

    assert stored == (
        "MSG-001",
        raw_payload,
        str(input_path.resolve()),
        1,
        result.batch_id,
    )
    assert batch == ("completed", 1, 1, 0, 0)


def test_replay_is_idempotent_by_message_id_and_payload(tmp_path: Path) -> None:
    input_path = tmp_path / "provider_updates.jsonl"
    _write(input_path, _line(_message("MSG-001")), _line(_message("MSG-002")))
    database_path = tmp_path / "fabtrace.db"

    first = ingest_provider_updates(input_path, database_path)
    second = ingest_provider_updates(input_path, database_path)

    assert (first.accepted_count, first.duplicate_count) == (2, 0)
    assert (second.accepted_count, second.duplicate_count) == (0, 2)
    with sqlite3.connect(database_path) as connection:
        message_count = connection.execute(
            "SELECT COUNT(*) FROM raw_provider_messages"
        ).fetchone()[0]
        batch_count = connection.execute(
            "SELECT COUNT(*) FROM ingestion_batches"
        ).fetchone()[0]
    assert message_count == 2
    assert batch_count == 2


def test_invalid_and_unsupported_payloads_are_quarantined(tmp_path: Path) -> None:
    invalid_schema = _message("MSG-SCHEMA")
    invalid_schema["schema_version"] = "2.0"
    missing_body = _message("MSG-BODY")
    del missing_body["body"]
    input_path = tmp_path / "provider_updates.jsonl"
    _write(
        input_path,
        b"not-json\n",
        _line(["not", "an", "envelope"]),
        _line(invalid_schema),
        _line(missing_body),
    )
    database_path = tmp_path / "fabtrace.db"

    result = ingest_provider_updates(input_path, database_path)

    assert result.accepted_count == 0
    assert result.quarantined_count == 4
    with sqlite3.connect(database_path) as connection:
        errors = connection.execute(
            """
            SELECT source_line, error_code, raw_payload
            FROM quarantined_provider_messages ORDER BY source_line
            """
        ).fetchall()
    assert [row[1] for row in errors] == [
        "invalid_json",
        "invalid_envelope",
        "unsupported_schema_version",
        "schema_validation_error",
    ]
    assert errors[0][2] == b"not-json\n"


def test_same_message_id_with_different_bytes_is_quarantined(tmp_path: Path) -> None:
    original = _message("MSG-001")
    conflicting = _message("MSG-001")
    conflicting["body"] = "A conflicting update."
    input_path = tmp_path / "provider_updates.jsonl"
    _write(input_path, _line(original), _line(conflicting))
    database_path = tmp_path / "fabtrace.db"

    result = ingest_provider_updates(input_path, database_path)

    assert result.accepted_count == 1
    assert result.duplicate_count == 0
    assert result.quarantined_count == 1
    with sqlite3.connect(database_path) as connection:
        accepted_body = json.loads(
            connection.execute(
                "SELECT raw_payload FROM raw_provider_messages"
            ).fetchone()[0]
        )["body"]
        error = connection.execute(
            "SELECT error_code FROM quarantined_provider_messages"
        ).fetchone()[0]
    assert accepted_body == original["body"]
    assert error == "message_id_conflict"


def test_out_of_order_event_times_are_retained_in_arrival_order(tmp_path: Path) -> None:
    input_path = tmp_path / "provider_updates.jsonl"
    _write(
        input_path,
        _line(_message("MSG-LATE", sent_at="2026-01-10T00:00:00Z")),
        _line(_message("MSG-EARLY", sent_at="2026-01-05T00:00:00Z")),
    )
    database_path = tmp_path / "fabtrace.db"

    result = ingest_provider_updates(input_path, database_path)

    assert result.accepted_count == 2
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT message_id, sent_at, source_line
            FROM raw_provider_messages ORDER BY rowid
            """
        ).fetchall()
    assert [row[0] for row in rows] == ["MSG-LATE", "MSG-EARLY"]
    assert rows[0][1] > rows[1][1]
    assert [row[2] for row in rows] == [1, 2]


def test_raw_and_quarantine_tables_reject_updates_and_deletes(tmp_path: Path) -> None:
    input_path = tmp_path / "provider_updates.jsonl"
    _write(input_path, _line(_message("MSG-001")), b"invalid\n")
    database_path = tmp_path / "fabtrace.db"
    ingest_provider_updates(input_path, database_path)

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE raw_provider_messages SET subject = 'changed'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM quarantined_provider_messages")


def test_private_ground_truth_is_not_accepted_as_provider_message(
    tmp_path: Path,
) -> None:
    ground_truth = {
        "schema_version": "1.0",
        "generator_version": "0.1.0",
        "synthetic": True,
        "scenario_id": "private-truth",
        "event_id": "event-001",
        "event_type": "progress_updated",
    }
    input_path = tmp_path / "ground_truth.jsonl"
    _write(input_path, _line(ground_truth))

    result = ingest_provider_updates(input_path, tmp_path / "fabtrace.db")

    assert result.accepted_count == 0
    assert result.quarantined_count == 1


def test_failed_batch_rolls_back_and_can_be_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_path = tmp_path / "provider_updates.jsonl"
    _write(input_path, _line(_message("MSG-001")), _line(_message("MSG-002")))
    database_path = tmp_path / "fabtrace.db"
    original_store = ingestion_pipeline._store_message
    call_count = 0

    def fail_on_second_message(*args: object, **kwargs: object) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise sqlite3.OperationalError("injected consumer failure")
        return original_store(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ingestion_pipeline, "_store_message", fail_on_second_message)
    with pytest.raises(IngestionError, match="injected consumer failure"):
        ingest_provider_updates(input_path, database_path)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM raw_provider_messages"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM ingestion_batches"
        ).fetchone()[0] == 0

    monkeypatch.setattr(ingestion_pipeline, "_store_message", original_store)
    retried = ingest_provider_updates(input_path, database_path)
    assert retried.accepted_count == 2


def test_missing_input_is_reported_before_database_creation(tmp_path: Path) -> None:
    database_path = tmp_path / "fabtrace.db"

    with pytest.raises(IngestionError, match="does not exist"):
        ingest_provider_updates(tmp_path / "missing.jsonl", database_path)

    assert not database_path.exists()
