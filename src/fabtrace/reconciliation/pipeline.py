from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fabtrace.storage import StorageSchemaError, initialize_database

RECONCILIATION_VERSION = "order-state-v2"
SUPPORTED_RECONCILIATION_VERSIONS = ("order-state-v1", "order-state-v2")
EXTRACTOR_BY_RECONCILIATION_VERSION = {
    "order-state-v1": "rules-v1",
    "order-state-v2": "rules-v2",
}

FIELD_COLUMNS = {
    "order_status": "order_status",
    "progress_pct": "progress_pct",
    "quantity_completed": "quantity_completed",
    "quantity_ordered": "quantity_ordered",
    "unit": "quantity_unit",
    "forecast_delivery_date": "forecast_delivery_date",
    "delay_days": "delay_days",
    "incident_reason": "incident_reason",
    "milestone_date": "milestone_date",
}

ALLOWED_STATUS_TRANSITIONS = {
    "ACKNOWLEDGED": {"QUEUED"},
    "QUEUED": {"IN_PRODUCTION"},
    "IN_PRODUCTION": {"BLOCKED", "QUALITY_CHECK"},
    "BLOCKED": {"IN_PRODUCTION"},
    "QUALITY_CHECK": {"READY_TO_SHIP"},
    "READY_TO_SHIP": {"SHIPPED"},
    "SHIPPED": {"DELIVERED"},
    "DELIVERED": set(),
}


class ReconciliationError(RuntimeError):
    """Raised when a bounded order-state reconciliation cannot complete."""


@dataclass(frozen=True)
class ReconciliationResult:
    reconciliation_run_id: str
    database_path: Path
    scenario_id: str
    extractor_version: str
    reconciliation_version: str
    selected_count: int
    applied_count: int
    stale_count: int
    conflict_count: int
    skipped_count: int
    snapshot_count: int


@dataclass(frozen=True)
class FieldState:
    value: str | float
    source_message_id: str
    effective_at: str


@dataclass
class OrderProjection:
    scenario_id: str
    purchase_order_id: str
    provider_profile_id: str
    source_provider_id: str
    snapshot_sequence: int = 0
    fields: dict[str, FieldState] = field(default_factory=dict)


@dataclass(frozen=True)
class MessageRecord:
    rowid: int
    message_id: str
    scenario_id: str
    purchase_order_id: str
    provider_profile_id: str
    source_provider_id: str
    sent_at: str
    received_at: str


@dataclass(frozen=True)
class MergeOutcome:
    decision: str
    detail: str | None
    fields: dict[str, FieldState]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"timestamp must include an offset: {value!r}")
    return parsed


def _snapshot_id(message_id: str, reconciliation_version: str) -> str:
    material = f"{message_id}|{reconciliation_version}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _claim_value(claim: sqlite3.Row) -> str | float:
    if claim["value_text"] is not None:
        return claim["value_text"]
    if claim["value_number"] is not None:
        return float(claim["value_number"])
    if claim["value_date"] is not None:
        return claim["value_date"]
    raise ValueError(f"claim {claim['claim_id']} has no typed value")


def _validate_status_transition(current: str | None, proposed: str) -> str | None:
    if current is None:
        if proposed != "ACKNOWLEDGED":
            return f"initial order status must be ACKNOWLEDGED, received {proposed}"
        return None
    if proposed == current:
        return None
    if proposed not in ALLOWED_STATUS_TRANSITIONS.get(current, set()):
        return f"illegal order status transition {current} -> {proposed}"
    return None


def _validate_candidate(fields: dict[str, FieldState]) -> str | None:
    progress = fields.get("progress_pct")
    if progress is not None and not 0 <= float(progress.value) <= 100:
        return "progress percentage must be between 0 and 100"
    completed = fields.get("quantity_completed")
    ordered = fields.get("quantity_ordered")
    if completed is not None and float(completed.value) < 0:
        return "completed quantity must not be negative"
    if ordered is not None and float(ordered.value) <= 0:
        return "ordered quantity must be positive"
    if completed is not None and ordered is not None:
        if float(completed.value) > float(ordered.value):
            return "completed quantity must not exceed ordered quantity"
    return None


def _merge_claims(
    projection: OrderProjection,
    message: MessageRecord,
    claims: list[sqlite3.Row],
) -> MergeOutcome:
    sent_at = _parse_time(message.sent_at)
    received_at = _parse_time(message.received_at)
    if sent_at > received_at:
        return MergeOutcome(
            decision="conflict",
            detail="provider sent_at is later than received_at",
            fields=projection.fields,
        )

    claim_types = [claim["claim_type"] for claim in claims]
    if len(claim_types) != len(set(claim_types)):
        return MergeOutcome(
            decision="conflict",
            detail="message contains duplicate claim types",
            fields=projection.fields,
        )

    candidate = dict(projection.fields)
    updated_fields: list[str] = []
    stale_fields: list[str] = []
    for claim in claims:
        claim_type = claim["claim_type"]
        field_name = FIELD_COLUMNS.get(claim_type)
        if field_name is None:
            return MergeOutcome(
                decision="conflict",
                detail=f"unsupported claim type {claim_type!r}",
                fields=projection.fields,
            )
        proposed = _claim_value(claim)
        current = candidate.get(field_name)
        if current is not None:
            current_time = _parse_time(current.effective_at)
            if sent_at < current_time:
                stale_fields.append(field_name)
                continue
            if proposed == current.value:
                continue
            if sent_at == current_time and field_name != "order_status":
                return MergeOutcome(
                    decision="conflict",
                    detail=(
                        f"same-time conflict for {field_name}: "
                        f"{current.value!r} versus {proposed!r}"
                    ),
                    fields=projection.fields,
                )

        if field_name == "order_status":
            current_status = str(current.value) if current is not None else None
            transition_error = _validate_status_transition(
                current_status,
                str(proposed),
            )
            if transition_error is not None:
                return MergeOutcome(
                    decision="conflict",
                    detail=transition_error,
                    fields=projection.fields,
                )
        elif field_name in {"progress_pct", "quantity_completed"}:
            if current is not None and float(proposed) < float(current.value):
                return MergeOutcome(
                    decision="conflict",
                    detail=f"{field_name} must not decrease",
                    fields=projection.fields,
                )
        elif field_name in {"quantity_ordered", "quantity_unit"}:
            if current is not None and proposed != current.value:
                return MergeOutcome(
                    decision="conflict",
                    detail=f"{field_name} cannot change without an approved correction",
                    fields=projection.fields,
                )

        candidate[field_name] = FieldState(
            value=proposed,
            source_message_id=message.message_id,
            effective_at=message.sent_at,
        )
        updated_fields.append(field_name)

    validation_error = _validate_candidate(candidate)
    if validation_error is not None:
        return MergeOutcome(
            decision="conflict",
            detail=validation_error,
            fields=projection.fields,
        )
    if not updated_fields:
        detail = "all eligible claims were stale or already known"
        return MergeOutcome(decision="stale", detail=detail, fields=projection.fields)

    detail = None
    if stale_fields:
        detail = "ignored stale fields: " + ", ".join(sorted(stale_fields))
    return MergeOutcome(decision="applied", detail=detail, fields=candidate)


def _load_current_projections(
    connection: sqlite3.Connection,
    scenario_id: str,
    reconciliation_version: str,
) -> dict[str, OrderProjection]:
    rows = connection.execute(
        """
        SELECT * FROM current_order_state
        WHERE scenario_id = ? AND reconciliation_version = ?
        """,
        (scenario_id, reconciliation_version),
    ).fetchall()
    projections: dict[str, OrderProjection] = {}
    for row in rows:
        projection = OrderProjection(
            scenario_id=row["scenario_id"],
            purchase_order_id=row["purchase_order_id"],
            provider_profile_id=row["provider_profile_id"],
            source_provider_id=row["source_provider_id"],
            snapshot_sequence=row["snapshot_sequence"],
        )
        for field_name in FIELD_COLUMNS.values():
            value = row[field_name]
            if value is None:
                continue
            projection.fields[field_name] = FieldState(
                value=value,
                source_message_id=row[f"{field_name}_source_message_id"],
                effective_at=row[f"{field_name}_effective_at"],
            )
        projections[projection.purchase_order_id] = projection
    return projections


def _store_decision(
    connection: sqlite3.Connection,
    *,
    decision_id: str,
    reconciliation_run_id: str,
    message: MessageRecord,
    processing_sequence: int,
    decision: str,
    detail: str | None,
    extractor_version: str,
    reconciliation_version: str,
) -> None:
    connection.execute(
        """
        INSERT INTO reconciliation_decisions (
            decision_id, reconciliation_run_id, message_id, scenario_id,
            purchase_order_id, extractor_version, reconciliation_version,
            processing_sequence, sent_at, received_at, decision, detail,
            decided_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            decision_id,
            reconciliation_run_id,
            message.message_id,
            message.scenario_id,
            message.purchase_order_id,
            extractor_version,
            reconciliation_version,
            processing_sequence,
            message.sent_at,
            message.received_at,
            decision,
            detail,
            _utc_now(),
        ),
    )


def _field_values(
    projection: OrderProjection,
    field_name: str,
) -> tuple[str | float | None, str | None, str | None]:
    state = projection.fields.get(field_name)
    if state is None:
        return None, None, None
    return state.value, state.source_message_id, state.effective_at


def _store_snapshot(
    connection: sqlite3.Connection,
    *,
    reconciliation_run_id: str,
    decision_id: str,
    message: MessageRecord,
    projection: OrderProjection,
    reconciliation_version: str,
) -> None:
    ordered_fields = (
        "order_status",
        "progress_pct",
        "quantity_completed",
        "quantity_ordered",
        "quantity_unit",
        "forecast_delivery_date",
        "delay_days",
        "incident_reason",
        "milestone_date",
    )
    flattened = tuple(
        value
        for field_name in ordered_fields
        for value in _field_values(projection, field_name)
    )
    connection.execute(
        """
        INSERT INTO order_state_snapshots (
            snapshot_id, decision_id, reconciliation_run_id,
            reconciliation_version, scenario_id, purchase_order_id,
            provider_profile_id, source_provider_id, snapshot_sequence,
            as_of_received_at, effective_through_sent_at, last_message_id,
            created_at,
            order_status, order_status_source_message_id,
            order_status_effective_at,
            progress_pct, progress_pct_source_message_id,
            progress_pct_effective_at,
            quantity_completed, quantity_completed_source_message_id,
            quantity_completed_effective_at,
            quantity_ordered, quantity_ordered_source_message_id,
            quantity_ordered_effective_at,
            quantity_unit, quantity_unit_source_message_id,
            quantity_unit_effective_at,
            forecast_delivery_date,
            forecast_delivery_date_source_message_id,
            forecast_delivery_date_effective_at,
            delay_days, delay_days_source_message_id, delay_days_effective_at,
            incident_reason, incident_reason_source_message_id,
            incident_reason_effective_at,
            milestone_date, milestone_date_source_message_id,
            milestone_date_effective_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            _snapshot_id(message.message_id, reconciliation_version),
            decision_id,
            reconciliation_run_id,
            reconciliation_version,
            message.scenario_id,
            message.purchase_order_id,
            message.provider_profile_id,
            message.source_provider_id,
            projection.snapshot_sequence,
            message.received_at,
            message.sent_at,
            message.message_id,
            _utc_now(),
            *flattened,
        ),
    )


def _message_records(
    connection: sqlite3.Connection,
    scenario_id: str,
) -> list[MessageRecord]:
    rows = connection.execute(
        """
        SELECT rowid, message_id, scenario_id, purchase_order_id,
               provider_profile_id, source_provider_id, sent_at, received_at
        FROM raw_provider_messages
        WHERE scenario_id = ?
        """,
        (scenario_id,),
    ).fetchall()
    records = [MessageRecord(**dict(row)) for row in rows]
    return sorted(records, key=lambda item: (_parse_time(item.received_at), item.rowid))


def reconcile_order_state(
    database_path: Path,
    scenario_id: str,
    reconciliation_version: str = RECONCILIATION_VERSION,
) -> ReconciliationResult:
    database_path = Path(database_path).resolve()
    if not database_path.is_file():
        raise ReconciliationError(f"FabTrace database does not exist: {database_path}")
    if not scenario_id.strip():
        raise ReconciliationError("scenario_id must not be blank")
    if reconciliation_version not in SUPPORTED_RECONCILIATION_VERSIONS:
        raise ReconciliationError(
            f"unsupported reconciliation version {reconciliation_version!r}; "
            "expected one of " + ", ".join(SUPPORTED_RECONCILIATION_VERSIONS)
        )
    extractor_version = EXTRACTOR_BY_RECONCILIATION_VERSION[
        reconciliation_version
    ]

    reconciliation_run_id = str(uuid4())
    applied_count = 0
    stale_count = 0
    conflict_count = 0
    skipped_count = 0
    snapshot_count = 0

    try:
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise ReconciliationError(
            f"cannot open reconciliation database {database_path}: {exc}"
        ) from exc

    try:
        initialize_database(connection)
        messages = _message_records(connection, scenario_id)
        if not messages:
            raise ReconciliationError(
                f"no accepted provider messages found for scenario {scenario_id!r}"
            )
        projections = _load_current_projections(
            connection,
            scenario_id,
            reconciliation_version,
        )
        connection.execute(
            """
            INSERT INTO reconciliation_runs (
                reconciliation_run_id, scenario_id, extractor_version,
                reconciliation_version, started_at, status, selected_count
            ) VALUES (?, ?, ?, ?, ?, 'started', ?)
            """,
            (
                reconciliation_run_id,
                scenario_id,
                extractor_version,
                reconciliation_version,
                _utc_now(),
                len(messages),
            ),
        )

        for processing_sequence, message in enumerate(messages, start=1):
            existing = connection.execute(
                """
                SELECT 1 FROM reconciliation_decisions
                WHERE message_id = ? AND reconciliation_version = ?
                """,
                (message.message_id, reconciliation_version),
            ).fetchone()
            if existing is not None:
                skipped_count += 1
                continue

            decision_id = str(uuid4())
            attempt = connection.execute(
                """
                SELECT status, resolved_order_id, resolution_status
                FROM extraction_attempts
                WHERE message_id = ? AND extractor_version = ?
                  AND status IN ('extracted', 'abstained', 'review_required')
                """,
                (message.message_id, extractor_version),
            ).fetchone()
            if (
                attempt is None
                or attempt["status"] != "extracted"
                or attempt["resolution_status"] != "resolved"
                or attempt["resolved_order_id"] != message.purchase_order_id
            ):
                _store_decision(
                    connection,
                    decision_id=decision_id,
                    reconciliation_run_id=reconciliation_run_id,
                    message=message,
                    processing_sequence=processing_sequence,
                    decision="conflict",
                    detail="message has no eligible resolved extraction",
                    extractor_version=extractor_version,
                    reconciliation_version=reconciliation_version,
                )
                conflict_count += 1
                continue

            claims = connection.execute(
                """
                SELECT * FROM extracted_claims
                WHERE message_id = ? AND extractor_version = ?
                  AND confidence = 1.0 AND validation_status = 'valid'
                  AND resolved_order_id = ?
                ORDER BY claim_index
                """,
                (
                    message.message_id,
                    extractor_version,
                    message.purchase_order_id,
                ),
            ).fetchall()
            if not claims:
                _store_decision(
                    connection,
                    decision_id=decision_id,
                    reconciliation_run_id=reconciliation_run_id,
                    message=message,
                    processing_sequence=processing_sequence,
                    decision="conflict",
                    detail="resolved extraction contains no eligible claims",
                    extractor_version=extractor_version,
                    reconciliation_version=reconciliation_version,
                )
                conflict_count += 1
                continue

            projection = projections.get(message.purchase_order_id)
            if projection is None:
                projection = OrderProjection(
                    scenario_id=message.scenario_id,
                    purchase_order_id=message.purchase_order_id,
                    provider_profile_id=message.provider_profile_id,
                    source_provider_id=message.source_provider_id,
                )
                projections[message.purchase_order_id] = projection
            if (
                projection.provider_profile_id != message.provider_profile_id
                or projection.source_provider_id != message.source_provider_id
            ):
                outcome = MergeOutcome(
                    decision="conflict",
                    detail="provider identity changed for an existing order",
                    fields=projection.fields,
                )
            else:
                outcome = _merge_claims(projection, message, claims)

            _store_decision(
                connection,
                decision_id=decision_id,
                reconciliation_run_id=reconciliation_run_id,
                message=message,
                processing_sequence=processing_sequence,
                decision=outcome.decision,
                detail=outcome.detail,
                extractor_version=extractor_version,
                reconciliation_version=reconciliation_version,
            )
            if outcome.decision == "conflict":
                conflict_count += 1
                continue
            if outcome.decision == "stale":
                stale_count += 1
                continue

            projection.fields = outcome.fields
            projection.snapshot_sequence += 1
            _store_snapshot(
                connection,
                reconciliation_run_id=reconciliation_run_id,
                decision_id=decision_id,
                message=message,
                projection=projection,
                reconciliation_version=reconciliation_version,
            )
            applied_count += 1
            snapshot_count += 1

        connection.execute(
            """
            UPDATE reconciliation_runs
            SET completed_at = ?, status = 'completed', applied_count = ?,
                stale_count = ?, conflict_count = ?, skipped_count = ?,
                snapshot_count = ?
            WHERE reconciliation_run_id = ?
            """,
            (
                _utc_now(),
                applied_count,
                stale_count,
                conflict_count,
                skipped_count,
                snapshot_count,
                reconciliation_run_id,
            ),
        )
        connection.commit()
    except (sqlite3.Error, StorageSchemaError, ValueError) as exc:
        connection.rollback()
        raise ReconciliationError(f"order-state reconciliation failed: {exc}") from exc
    except ReconciliationError:
        connection.rollback()
        raise
    finally:
        connection.close()

    return ReconciliationResult(
        reconciliation_run_id=reconciliation_run_id,
        database_path=database_path,
        scenario_id=scenario_id,
        extractor_version=extractor_version,
        reconciliation_version=reconciliation_version,
        selected_count=len(messages),
        applied_count=applied_count,
        stale_count=stale_count,
        conflict_count=conflict_count,
        skipped_count=skipped_count,
        snapshot_count=snapshot_count,
    )
