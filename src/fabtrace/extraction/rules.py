from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

from fabtrace.simulation.models import ProviderMessage

ClaimType = Literal[
    "order_status",
    "progress_pct",
    "quantity_completed",
    "quantity_ordered",
    "unit",
    "forecast_delivery_date",
    "delay_days",
    "incident_reason",
    "milestone_date",
]
AttemptStatus = Literal["extracted", "abstained", "review_required"]
ResolutionStatus = Literal["resolved", "missing", "conflict", "ambiguous"]

LEGACY_ORDER_PATTERN = r"PO-[A-Za-z0-9][A-Za-z0-9-]*"
PLAN_ORDER_PATTERN = (
    r"[A-Za-z0-9][A-Za-z0-9-]*-(?:PO|WO)-[A-Za-z0-9][A-Za-z0-9-]*"
)
DATE_PATTERN = r"\d{4}-\d{2}-\d{2}"
SUPPORTED_EXTRACTOR_VERSIONS = ("rules-v1", "rules-v2")


@dataclass(frozen=True)
class CandidateClaim:
    claim_type: ClaimType
    source_field: Literal["subject", "body"]
    span_start: int
    span_end: int
    source_text: str
    value_text: str | None = None
    value_number: float | None = None
    value_date: str | None = None
    unit: str | None = None


@dataclass(frozen=True)
class RuleOutcome:
    status: AttemptStatus
    claimed_order_id: str | None
    resolved_order_id: str | None
    resolution_status: ResolutionStatus
    detail: str | None
    claims: tuple[CandidateClaim, ...]


def _text_claim(
    claim_type: ClaimType,
    value: str,
    source_field: Literal["subject", "body"],
    source: str,
    span: tuple[int, int],
    *,
    unit: str | None = None,
) -> CandidateClaim:
    return CandidateClaim(
        claim_type=claim_type,
        value_text=value,
        source_field=source_field,
        span_start=span[0],
        span_end=span[1],
        source_text=source[span[0] : span[1]],
        unit=unit,
    )


def _number_claim(
    claim_type: ClaimType,
    value: float,
    source: str,
    span: tuple[int, int],
    *,
    unit: str,
) -> CandidateClaim:
    return CandidateClaim(
        claim_type=claim_type,
        value_number=value,
        source_field="body",
        span_start=span[0],
        span_end=span[1],
        source_text=source[span[0] : span[1]],
        unit=unit,
    )


def _date_claim(
    claim_type: ClaimType,
    value: str,
    source: str,
    span: tuple[int, int],
) -> CandidateClaim:
    date.fromisoformat(value)
    return CandidateClaim(
        claim_type=claim_type,
        value_date=value,
        source_field="body",
        span_start=span[0],
        span_end=span[1],
        source_text=source[span[0] : span[1]],
    )


def _status_claim(subject: str, status: str) -> CandidateClaim:
    return _text_claim(
        "order_status", status, "subject", subject, (0, len(subject))
    )


def _normalized_unit(raw_unit: str) -> str:
    return {"wafers": "wafer", "wafer": "wafer"}.get(raw_unit.lower(), raw_unit)


def _normalized_reason(raw_reason: str) -> str:
    normalized = raw_reason.lower().strip().replace(" ", "_")
    return {
        "calibration": "equipment_calibration",
        "equipment_calibration": "equipment_calibration",
        "unplanned_maintenance": "unplanned_maintenance",
    }.get(normalized, normalized)


def _acknowledged(
    message: ProviderMessage,
    order_pattern: str,
) -> tuple[str, list[CandidateClaim]] | None:
    match = re.fullmatch(
        rf"We have acknowledged (?P<order>{order_pattern}) for "
        rf"(?P<quantity>\d+) (?P<unit>[A-Za-z]+)\. Expected delivery remains "
        rf"(?P<delivery>{DATE_PATTERN})\.",
        message.body,
    )
    if match is None:
        return None
    unit = _normalized_unit(match.group("unit"))
    claims = [
        _status_claim(message.subject, "ACKNOWLEDGED"),
        _number_claim(
            "quantity_ordered",
            float(match.group("quantity")),
            message.body,
            match.span("quantity"),
            unit=unit,
        ),
        _text_claim("unit", unit, "body", message.body, match.span("unit")),
        _date_claim(
            "forecast_delivery_date",
            match.group("delivery"),
            message.body,
            match.span("delivery"),
        ),
    ]
    return match.group("order"), claims


def _dated_status(
    message: ProviderMessage,
    *,
    pattern: str,
    status: str,
) -> tuple[str, list[CandidateClaim]] | None:
    match = re.fullmatch(pattern, message.body)
    if match is None:
        return None
    return match.group("order"), [
        _status_claim(message.subject, status),
        _date_claim(
            "milestone_date",
            match.group("date"),
            message.body,
            match.span("date"),
        ),
    ]


def _progress(
    message: ProviderMessage,
    order_pattern: str,
) -> tuple[str, list[CandidateClaim]] | None:
    match = re.fullmatch(
        rf"(?P<order>{order_pattern}) is (?P<progress>\d+(?:\.\d+)?)% complete "
        rf"\((?P<completed>\d+) of (?P<ordered>\d+) (?P<unit>[A-Za-z]+)\)\.",
        message.body,
    )
    if match is None:
        return None
    progress = float(match.group("progress"))
    completed = float(match.group("completed"))
    ordered = float(match.group("ordered"))
    if not 0 <= progress <= 100 or completed < 0 or ordered <= 0 or completed > ordered:
        raise ValueError("progress or quantity values violate domain bounds")
    unit = _normalized_unit(match.group("unit"))
    return match.group("order"), [
        _status_claim(message.subject, "IN_PRODUCTION"),
        _number_claim(
            "progress_pct",
            progress,
            message.body,
            match.span("progress"),
            unit="percent",
        ),
        _number_claim(
            "quantity_completed",
            completed,
            message.body,
            match.span("completed"),
            unit=unit,
        ),
        _number_claim(
            "quantity_ordered",
            ordered,
            message.body,
            match.span("ordered"),
            unit=unit,
        ),
        _text_claim("unit", unit, "body", message.body, match.span("unit")),
    ]


def _delay(
    message: ProviderMessage,
    order_pattern: str,
) -> tuple[str, list[CandidateClaim]] | None:
    match = re.fullmatch(
        rf"An? (?P<reason>[A-Za-z ]+) issue has paused production for "
        rf"(?P<order>{order_pattern})\. Delivery is now expected on "
        rf"(?P<delivery>{DATE_PATTERN}), (?P<delay>\d+) days? later than "
        rf"previously forecast\.",
        message.body,
    )
    if match is None:
        return None
    delay = float(match.group("delay"))
    if delay <= 0:
        raise ValueError("delay must be positive")
    return match.group("order"), [
        _status_claim(message.subject, "BLOCKED"),
        _text_claim(
            "incident_reason",
            _normalized_reason(match.group("reason")),
            "body",
            message.body,
            match.span("reason"),
        ),
        _date_claim(
            "forecast_delivery_date",
            match.group("delivery"),
            message.body,
            match.span("delivery"),
        ),
        _number_claim(
            "delay_days",
            delay,
            message.body,
            match.span("delay"),
            unit="day",
        ),
    ]


def _resumed(
    message: ProviderMessage,
    order_pattern: str,
) -> tuple[str, list[CandidateClaim]] | None:
    match = re.fullmatch(
        rf"Production for (?P<order>{order_pattern}) resumed on "
        rf"(?P<date>{DATE_PATTERN})\. Expected delivery remains "
        rf"(?P<delivery>{DATE_PATTERN})\.",
        message.body,
    )
    if match is None:
        return None
    return match.group("order"), [
        _status_claim(message.subject, "IN_PRODUCTION"),
        _date_claim(
            "milestone_date",
            match.group("date"),
            message.body,
            match.span("date"),
        ),
        _date_claim(
            "forecast_delivery_date",
            match.group("delivery"),
            message.body,
            match.span("delivery"),
        ),
    ]


def _status_only(
    message: ProviderMessage,
    *,
    pattern: str,
    status: str,
) -> tuple[str, list[CandidateClaim]] | None:
    match = re.fullmatch(pattern, message.body)
    if match is None:
        return None
    return match.group("order"), [_status_claim(message.subject, status)]


def _shipment(
    message: ProviderMessage,
    order_pattern: str,
) -> tuple[str, list[CandidateClaim]] | None:
    match = re.fullmatch(
        rf"(?P<order>{order_pattern}) shipped on (?P<date>{DATE_PATTERN}); "
        rf"delivery is expected on (?P<delivery>{DATE_PATTERN})\.",
        message.body,
    )
    if match is None:
        return None
    return match.group("order"), [
        _status_claim(message.subject, "SHIPPED"),
        _date_claim(
            "milestone_date",
            match.group("date"),
            message.body,
            match.span("date"),
        ),
        _date_claim(
            "forecast_delivery_date",
            match.group("delivery"),
            message.body,
            match.span("delivery"),
        ),
    ]


def _work_handoff(
    message: ProviderMessage,
    order_pattern: str,
) -> tuple[str, list[CandidateClaim]] | None:
    match = re.fullmatch(
        rf"Output for (?P<order>{order_pattern}) was transferred on "
        rf"(?P<date>{DATE_PATTERN}); handoff is expected on "
        rf"(?P<delivery>{DATE_PATTERN})\.",
        message.body,
    )
    if match is None:
        return None
    return match.group("order"), [
        _status_claim(message.subject, "SHIPPED"),
        _date_claim(
            "milestone_date",
            match.group("date"),
            message.body,
            match.span("date"),
        ),
        _date_claim(
            "forecast_delivery_date",
            match.group("delivery"),
            message.body,
            match.span("delivery"),
        ),
    ]


def _apply_subject_rule(
    message: ProviderMessage,
    order_pattern: str,
    *,
    include_plan_messages: bool,
) -> tuple[str, list[CandidateClaim]] | None:
    if message.subject == "Order acknowledged":
        return _acknowledged(message, order_pattern)
    if message.subject == "Order queued":
        return _dated_status(
            message,
            pattern=(
                rf"(?P<order>{order_pattern}) entered our production queue on "
                rf"(?P<date>{DATE_PATTERN})\."
            ),
            status="QUEUED",
        )
    if message.subject == "Production started":
        return _dated_status(
            message,
            pattern=(
                rf"Production for (?P<order>{order_pattern}) started on "
                rf"(?P<date>{DATE_PATTERN})\."
            ),
            status="IN_PRODUCTION",
        )
    if message.subject == "Production progress":
        return _progress(message, order_pattern)
    if message.subject == "Production delay":
        return _delay(message, order_pattern)
    if message.subject == "Production resumed":
        return _resumed(message, order_pattern)
    if message.subject == "Quality check started":
        return _status_only(
            message,
            pattern=(
                rf"Production for (?P<order>{order_pattern}) is complete and "
                rf"quality checks have begun\."
            ),
            status="QUALITY_CHECK",
        )
    if message.subject == "Quality check passed":
        return _status_only(
            message,
            pattern=(
                rf"(?P<order>{order_pattern}) passed quality checks and is ready "
                rf"(?:to ship|for handoff)\."
                if include_plan_messages
                else rf"(?P<order>{order_pattern}) passed quality checks and is ready to ship\."
            ),
            status="READY_TO_SHIP",
        )
    if message.subject == "Shipment dispatched":
        return _shipment(message, order_pattern)
    if include_plan_messages and message.subject == "Output dispatched":
        return _work_handoff(message, order_pattern)
    if message.subject == "Order delivered":
        return _dated_status(
            message,
            pattern=(
                rf"Delivery of (?P<order>{order_pattern}) was completed on "
                rf"(?P<date>{DATE_PATTERN})\."
            ),
            status="DELIVERED",
        )
    if include_plan_messages and message.subject == "Work output delivered":
        return _dated_status(
            message,
            pattern=(
                rf"Output handoff for (?P<order>{order_pattern}) was completed on "
                rf"(?P<date>{DATE_PATTERN})\."
            ),
            status="DELIVERED",
        )
    return None


def extract_claims(
    message: ProviderMessage,
    extractor_version: str = "rules-v2",
) -> RuleOutcome:
    if extractor_version not in SUPPORTED_EXTRACTOR_VERSIONS:
        raise ValueError(f"unsupported extractor version: {extractor_version}")
    include_plan_messages = extractor_version == "rules-v2"
    order_pattern = (
        rf"(?:{PLAN_ORDER_PATTERN}|{LEGACY_ORDER_PATTERN})"
        if include_plan_messages
        else LEGACY_ORDER_PATTERN
    )
    order_reference = re.compile(rf"\b{order_pattern}\b")
    order_references = tuple(
        dict.fromkeys(order_reference.findall(message.body))
    )
    if len(order_references) > 1:
        return RuleOutcome(
            status="review_required",
            claimed_order_id=None,
            resolved_order_id=None,
            resolution_status="ambiguous",
            detail="message body contains multiple order references",
            claims=(),
        )

    try:
        extracted = _apply_subject_rule(
            message,
            order_pattern,
            include_plan_messages=include_plan_messages,
        )
    except ValueError as exc:
        claimed = order_references[0] if order_references else None
        resolved = (
            message.purchase_order_id
            if claimed == message.purchase_order_id
            else None
        )
        return RuleOutcome(
            status="review_required",
            claimed_order_id=claimed,
            resolved_order_id=resolved,
            resolution_status=(
                "resolved" if resolved else "conflict" if claimed else "missing"
            ),
            detail=str(exc),
            claims=(),
        )

    if extracted is None:
        claimed = order_references[0] if order_references else None
        return RuleOutcome(
            status="abstained",
            claimed_order_id=claimed,
            resolved_order_id=(
                message.purchase_order_id
                if claimed == message.purchase_order_id
                else None
            ),
            resolution_status=(
                "resolved"
                if claimed == message.purchase_order_id
                else "conflict" if claimed else "missing"
            ),
            detail=f"no exact {extractor_version} pattern matched",
            claims=(),
        )

    claimed_order_id, claims = extracted
    if claimed_order_id != message.purchase_order_id:
        return RuleOutcome(
            status="review_required",
            claimed_order_id=claimed_order_id,
            resolved_order_id=None,
            resolution_status="conflict",
            detail=(
                f"body order {claimed_order_id!r} conflicts with envelope order "
                f"{message.purchase_order_id!r}"
            ),
            claims=(),
        )

    return RuleOutcome(
        status="extracted",
        claimed_order_id=claimed_order_id,
        resolved_order_id=message.purchase_order_id,
        resolution_status="resolved",
        detail=None,
        claims=tuple(claims),
    )
