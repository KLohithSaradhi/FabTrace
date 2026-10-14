from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from fabtrace.simulation.models import ProviderMessage
from fabtrace.storage import StorageSchemaError, initialize_database

SUPPORTED_MESSAGE_SCHEMA_VERSION = "1.0"


class IngestionError(RuntimeError):
    """Raised when a provider-update batch cannot be ingested safely."""


@dataclass(frozen=True)
class IngestionResult:
    batch_id: str
    database_path: Path
    source_path: Path
    total_lines: int
    accepted_count: int
    duplicate_count: int
    quarantined_count: int


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _quarantine(
    connection: sqlite3.Connection,
    *,
    batch_id: str,
    source_path: Path,
    source_line: int,
    raw_payload: bytes,
    payload_sha256: str,
    error_code: str,
    error_detail: str,
) -> None:
    connection.execute(
        """
        INSERT INTO quarantined_provider_messages (
            quarantine_id, batch_id, source_path, source_line, observed_at,
            error_code, error_detail, payload_sha256, raw_payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid4()),
            batch_id,
            str(source_path),
            source_line,
            _utc_now(),
            error_code,
            error_detail,
            payload_sha256,
            raw_payload,
        ),
    )


def _parse_message(raw_payload: bytes) -> tuple[ProviderMessage | None, str, str]:
    try:
        payload = json.loads(raw_payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, "invalid_json", str(exc)

    if not isinstance(payload, dict):
        return None, "invalid_envelope", "provider message must be a JSON object"

    schema_version = payload.get("schema_version")
    if schema_version != SUPPORTED_MESSAGE_SCHEMA_VERSION:
        return (
            None,
            "unsupported_schema_version",
            f"supported schema version is {SUPPORTED_MESSAGE_SCHEMA_VERSION!r}; "
            f"received {schema_version!r}",
        )

    try:
        return ProviderMessage.model_validate(payload), "", ""
    except ValidationError as exc:
        return None, "schema_validation_error", str(exc)


def _store_message(
    connection: sqlite3.Connection,
    *,
    message: ProviderMessage,
    raw_payload: bytes,
    payload_sha256: str,
    source_path: Path,
    source_line: int,
    batch_id: str,
) -> str:
    existing = connection.execute(
        "SELECT payload_sha256 FROM raw_provider_messages WHERE message_id = ?",
        (message.message_id,),
    ).fetchone()
    if existing is not None:
        return "duplicate" if existing[0] == payload_sha256 else "conflict"

    connection.execute(
        """
        INSERT INTO raw_provider_messages (
            message_id, schema_version, generator_version, synthetic,
            scenario_id, source_event_id, provider_profile_id,
            source_provider_id, purchase_order_id, channel, sent_at,
            received_at, subject, payload_sha256, raw_payload, ingested_at,
            source_path, source_line, first_batch_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message.message_id,
            message.schema_version,
            message.generator_version,
            int(message.synthetic),
            message.scenario_id,
            message.source_event_id,
            message.provider_profile_id,
            message.source_provider_id,
            message.purchase_order_id,
            message.channel,
            message.sent_at.isoformat(),
            message.received_at.isoformat(),
            message.subject,
            payload_sha256,
            raw_payload,
            _utc_now(),
            str(source_path),
            source_line,
            batch_id,
        ),
    )
    return "accepted"


def ingest_provider_updates(input_path: Path, database_path: Path) -> IngestionResult:
    input_path = Path(input_path).resolve()
    database_path = Path(database_path).resolve()
    if not input_path.is_file():
        raise IngestionError(f"provider-update input does not exist: {input_path}")

    database_path.parent.mkdir(parents=True, exist_ok=True)
    batch_id = str(uuid4())
    started_at = _utc_now()
    total_lines = 0
    accepted_count = 0
    duplicate_count = 0
    quarantined_count = 0

    try:
        connection = sqlite3.connect(database_path)
    except sqlite3.Error as exc:
        raise IngestionError(
            f"cannot open ingestion database {database_path}: {exc}"
        ) from exc

    try:
        initialize_database(connection)
        connection.execute(
            """
            INSERT INTO ingestion_batches (batch_id, source_path, started_at, status)
            VALUES (?, ?, ?, 'started')
            """,
            (batch_id, str(input_path), started_at),
        )

        with input_path.open("rb") as source:
            for source_line, raw_payload in enumerate(source, start=1):
                total_lines += 1
                payload_sha256 = hashlib.sha256(raw_payload).hexdigest()
                message, error_code, error_detail = _parse_message(raw_payload)
                if message is None:
                    _quarantine(
                        connection,
                        batch_id=batch_id,
                        source_path=input_path,
                        source_line=source_line,
                        raw_payload=raw_payload,
                        payload_sha256=payload_sha256,
                        error_code=error_code,
                        error_detail=error_detail,
                    )
                    quarantined_count += 1
                    continue

                outcome = _store_message(
                    connection,
                    message=message,
                    raw_payload=raw_payload,
                    payload_sha256=payload_sha256,
                    source_path=input_path,
                    source_line=source_line,
                    batch_id=batch_id,
                )
                if outcome == "accepted":
                    accepted_count += 1
                elif outcome == "duplicate":
                    duplicate_count += 1
                else:
                    _quarantine(
                        connection,
                        batch_id=batch_id,
                        source_path=input_path,
                        source_line=source_line,
                        raw_payload=raw_payload,
                        payload_sha256=payload_sha256,
                        error_code="message_id_conflict",
                        error_detail=(
                            f"message_id {message.message_id!r} already exists with "
                            "different payload bytes"
                        ),
                    )
                    quarantined_count += 1

        connection.execute(
            """
            UPDATE ingestion_batches
            SET completed_at = ?, status = 'completed', total_lines = ?,
                accepted_count = ?, duplicate_count = ?, quarantined_count = ?
            WHERE batch_id = ?
            """,
            (
                _utc_now(),
                total_lines,
                accepted_count,
                duplicate_count,
                quarantined_count,
                batch_id,
            ),
        )
        connection.commit()
    except (OSError, sqlite3.Error, StorageSchemaError) as exc:
        connection.rollback()
        raise IngestionError(f"provider-update ingestion failed: {exc}") from exc
    finally:
        connection.close()

    return IngestionResult(
        batch_id=batch_id,
        database_path=database_path,
        source_path=input_path,
        total_lines=total_lines,
        accepted_count=accepted_count,
        duplicate_count=duplicate_count,
        quarantined_count=quarantined_count,
    )
