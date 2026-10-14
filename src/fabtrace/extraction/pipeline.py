from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from fabtrace.extraction.rules import (
    SUPPORTED_EXTRACTOR_VERSIONS,
    CandidateClaim,
    extract_claims,
)
from fabtrace.simulation.models import ProviderMessage
from fabtrace.storage import StorageSchemaError, initialize_database

EXTRACTION_VERSION = "rules-v2"


class ExtractionError(RuntimeError):
    """Raised when a bounded provider-message extraction cannot complete."""


@dataclass(frozen=True)
class ExtractionResult:
    extraction_run_id: str
    database_path: Path
    scenario_id: str
    extractor_version: str
    selected_count: int
    extracted_count: int
    abstained_count: int
    review_count: int
    failed_count: int
    skipped_count: int
    claim_count: int


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _claim_id(message_id: str, extractor_version: str, claim_index: int) -> str:
    material = f"{message_id}|{extractor_version}|{claim_index}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _store_attempt(
    connection: sqlite3.Connection,
    *,
    attempt_id: str,
    extraction_run_id: str,
    message_id: str,
    status: str,
    claimed_order_id: str | None,
    resolved_order_id: str | None,
    resolution_status: str,
    detail: str | None,
    extractor_version: str,
) -> None:
    connection.execute(
        """
        INSERT INTO extraction_attempts (
            attempt_id, extraction_run_id, message_id, extractor_version,
            attempted_at, status, claimed_order_id, resolved_order_id,
            resolution_status, detail
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            attempt_id,
            extraction_run_id,
            message_id,
            extractor_version,
            _utc_now(),
            status,
            claimed_order_id,
            resolved_order_id,
            resolution_status,
            detail,
        ),
    )


def _store_claim(
    connection: sqlite3.Connection,
    *,
    extraction_run_id: str,
    attempt_id: str,
    message_id: str,
    claim_index: int,
    claim: CandidateClaim,
    claimed_order_id: str,
    resolved_order_id: str,
    extractor_version: str,
) -> None:
    connection.execute(
        """
        INSERT INTO extracted_claims (
            claim_id, attempt_id, extraction_run_id, message_id, claim_index,
            claim_type, value_text, value_number, value_date, unit,
            source_field, span_start, span_end, source_text, confidence,
            validation_status, extractor_version, claimed_order_id,
            resolved_order_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _claim_id(message_id, extractor_version, claim_index),
            attempt_id,
            extraction_run_id,
            message_id,
            claim_index,
            claim.claim_type,
            claim.value_text,
            claim.value_number,
            claim.value_date,
            claim.unit,
            claim.source_field,
            claim.span_start,
            claim.span_end,
            claim.source_text,
            1.0,
            "valid",
            extractor_version,
            claimed_order_id,
            resolved_order_id,
        ),
    )


def extract_provider_updates(
    database_path: Path,
    scenario_id: str,
    extractor_version: str = EXTRACTION_VERSION,
) -> ExtractionResult:
    database_path = Path(database_path).resolve()
    if not database_path.is_file():
        raise ExtractionError(f"ingestion database does not exist: {database_path}")
    if not scenario_id.strip():
        raise ExtractionError("scenario_id must not be blank")
    if extractor_version not in SUPPORTED_EXTRACTOR_VERSIONS:
        raise ExtractionError(
            f"unsupported extractor version {extractor_version!r}; expected one of "
            + ", ".join(SUPPORTED_EXTRACTOR_VERSIONS)
        )

    extraction_run_id = str(uuid4())
    selected_count = 0
    extracted_count = 0
    abstained_count = 0
    review_count = 0
    failed_count = 0
    skipped_count = 0
    claim_count = 0

    try:
        connection = sqlite3.connect(database_path)
    except sqlite3.Error as exc:
        raise ExtractionError(
            f"cannot open extraction database {database_path}: {exc}"
        ) from exc

    try:
        initialize_database(connection)
        messages = connection.execute(
            """
            SELECT message_id, raw_payload
            FROM raw_provider_messages
            WHERE scenario_id = ?
            ORDER BY received_at, rowid
            """,
            (scenario_id,),
        ).fetchall()
        if not messages:
            raise ExtractionError(
                f"no accepted provider messages found for scenario {scenario_id!r}"
            )
        selected_count = len(messages)
        connection.execute(
            """
            INSERT INTO extraction_runs (
                extraction_run_id, scenario_id, extractor_version,
                started_at, status, selected_count
            ) VALUES (?, ?, ?, ?, 'started', ?)
            """,
            (
                extraction_run_id,
                scenario_id,
                extractor_version,
                _utc_now(),
                selected_count,
            ),
        )

        for message_id, raw_payload in messages:
            completed = connection.execute(
                """
                SELECT 1 FROM extraction_attempts
                WHERE message_id = ? AND extractor_version = ?
                  AND status IN ('extracted', 'abstained', 'review_required')
                """,
                (message_id, extractor_version),
            ).fetchone()
            if completed is not None:
                skipped_count += 1
                continue

            attempt_id = str(uuid4())
            try:
                message = ProviderMessage.model_validate_json(raw_payload)
                outcome = extract_claims(message, extractor_version)
            except (ValidationError, ValueError, TypeError) as exc:
                _store_attempt(
                    connection,
                    attempt_id=attempt_id,
                    extraction_run_id=extraction_run_id,
                    message_id=message_id,
                    status="failed",
                    claimed_order_id=None,
                    resolved_order_id=None,
                    resolution_status="missing",
                    detail=str(exc),
                    extractor_version=extractor_version,
                )
                failed_count += 1
                continue

            _store_attempt(
                connection,
                attempt_id=attempt_id,
                extraction_run_id=extraction_run_id,
                message_id=message_id,
                status=outcome.status,
                claimed_order_id=outcome.claimed_order_id,
                resolved_order_id=outcome.resolved_order_id,
                resolution_status=outcome.resolution_status,
                detail=outcome.detail,
                extractor_version=extractor_version,
            )
            if outcome.status == "abstained":
                abstained_count += 1
                continue
            if outcome.status == "review_required":
                review_count += 1
                continue

            if outcome.claimed_order_id is None or outcome.resolved_order_id is None:
                raise ExtractionError(
                    "an extracted outcome must contain resolved order provenance"
                )
            for claim_index, claim in enumerate(outcome.claims):
                _store_claim(
                    connection,
                    extraction_run_id=extraction_run_id,
                    attempt_id=attempt_id,
                    message_id=message_id,
                    claim_index=claim_index,
                    claim=claim,
                    claimed_order_id=outcome.claimed_order_id,
                    resolved_order_id=outcome.resolved_order_id,
                    extractor_version=extractor_version,
                )
                claim_count += 1
            extracted_count += 1

        connection.execute(
            """
            UPDATE extraction_runs
            SET completed_at = ?, status = 'completed', extracted_count = ?,
                abstained_count = ?, review_count = ?, failed_count = ?,
                skipped_count = ?, claim_count = ?
            WHERE extraction_run_id = ?
            """,
            (
                _utc_now(),
                extracted_count,
                abstained_count,
                review_count,
                failed_count,
                skipped_count,
                claim_count,
                extraction_run_id,
            ),
        )
        connection.commit()
    except (sqlite3.Error, StorageSchemaError) as exc:
        connection.rollback()
        raise ExtractionError(f"provider-update extraction failed: {exc}") from exc
    except ExtractionError:
        connection.rollback()
        raise
    finally:
        connection.close()

    return ExtractionResult(
        extraction_run_id=extraction_run_id,
        database_path=database_path,
        scenario_id=scenario_id,
        extractor_version=extractor_version,
        selected_count=selected_count,
        extracted_count=extracted_count,
        abstained_count=abstained_count,
        review_count=review_count,
        failed_count=failed_count,
        skipped_count=skipped_count,
        claim_count=claim_count,
    )
