from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from fabtrace.extraction.pipeline import EXTRACTION_VERSION
from fabtrace.simulation.models import EventType, GroundTruthEvent
from fabtrace.storage import StorageSchemaError, initialize_database


class EvaluationError(RuntimeError):
    """Raised when extraction results cannot be evaluated safely."""


@dataclass(frozen=True)
class FieldMetric:
    claim_type: str
    expected_count: int
    extracted_count: int
    correct_count: int
    missing_count: int
    incorrect_count: int
    unexpected_count: int

    @property
    def precision(self) -> float:
        if self.extracted_count == 0:
            return 0.0
        return self.correct_count / self.extracted_count

    @property
    def recall(self) -> float:
        if self.expected_count == 0:
            return 0.0
        return self.correct_count / self.expected_count


@dataclass(frozen=True)
class EvaluationResult:
    scenario_id: str
    extractor_version: str
    message_count: int
    abstained_count: int
    review_count: int
    failed_count: int
    metrics: tuple[FieldMetric, ...]

    @property
    def expected_count(self) -> int:
        return sum(metric.expected_count for metric in self.metrics)

    @property
    def correct_count(self) -> int:
        return sum(metric.correct_count for metric in self.metrics)


ExpectedValue = tuple[str, str | float, str | None]


def _expected_claims(event: GroundTruthEvent) -> dict[str, ExpectedValue]:
    status = ("text", event.state.value, None)
    event_date = event.event_time.date().isoformat()
    delivery_date = event.forecast_delivery_at.date().isoformat()
    expected: dict[str, ExpectedValue] = {"order_status": status}

    if event.event_type == EventType.ORDER_ACKNOWLEDGED:
        expected.update(
            {
                "quantity_ordered": (
                    "number",
                    float(event.quantity_ordered),
                    event.unit,
                ),
                "unit": ("text", event.unit, None),
                "forecast_delivery_date": ("date", delivery_date, None),
            }
        )
    elif event.event_type in {
        EventType.ORDER_QUEUED,
        EventType.PRODUCTION_STARTED,
        EventType.ORDER_DELIVERED,
    }:
        expected["milestone_date"] = ("date", event_date, None)
    elif event.event_type == EventType.PROGRESS_UPDATED:
        expected.update(
            {
                # Provider messages intentionally render progress with no
                # decimal places, so evaluation must compare the observable
                # value rather than inaccessible private precision.
                "progress_pct": (
                    "number",
                    float(f"{event.progress_pct:.0f}"),
                    "percent",
                ),
                "quantity_completed": (
                    "number",
                    float(event.quantity_completed),
                    event.unit,
                ),
                "quantity_ordered": (
                    "number",
                    float(event.quantity_ordered),
                    event.unit,
                ),
                "unit": ("text", event.unit, None),
            }
        )
    elif event.event_type == EventType.EQUIPMENT_FAILURE:
        if event.reason_code is None or event.delay_days is None:
            raise EvaluationError(
                f"ground-truth incident {event.event_id} lacks reason or delay"
            )
        expected.update(
            {
                "incident_reason": ("text", event.reason_code, None),
                "forecast_delivery_date": ("date", delivery_date, None),
                "delay_days": ("number", float(event.delay_days), "day"),
            }
        )
    elif event.event_type == EventType.PRODUCTION_RESUMED:
        expected.update(
            {
                "milestone_date": ("date", event_date, None),
                "forecast_delivery_date": ("date", delivery_date, None),
            }
        )
    elif event.event_type == EventType.SHIPMENT_DISPATCHED:
        expected.update(
            {
                "milestone_date": ("date", event_date, None),
                "forecast_delivery_date": ("date", delivery_date, None),
            }
        )
    elif event.event_type not in {
        EventType.QUALITY_CHECK_STARTED,
        EventType.QUALITY_CHECK_PASSED,
    }:
        raise EvaluationError(
            f"ground-truth event {event.event_id} has no observable message contract"
        )
    return expected


def _load_ground_truth(path: Path, scenario_id: str) -> dict[str, GroundTruthEvent]:
    if not path.is_file():
        raise EvaluationError(f"ground-truth file does not exist: {path}")
    events: dict[str, GroundTruthEvent] = {}
    try:
        with path.open("rb") as source:
            for line_number, raw_payload in enumerate(source, start=1):
                event = GroundTruthEvent.model_validate_json(raw_payload)
                if event.scenario_id != scenario_id:
                    raise EvaluationError(
                        f"ground-truth line {line_number} belongs to scenario "
                        f"{event.scenario_id!r}, expected {scenario_id!r}"
                    )
                if event.event_id in events:
                    raise EvaluationError(
                        f"duplicate ground-truth event ID: {event.event_id}"
                    )
                events[event.event_id] = event
    except (OSError, ValidationError) as exc:
        raise EvaluationError(f"invalid ground-truth input: {exc}") from exc
    return events


def _actual_value(row: sqlite3.Row) -> ExpectedValue:
    if row["value_text"] is not None:
        return "text", row["value_text"], row["unit"]
    if row["value_number"] is not None:
        return "number", float(row["value_number"]), row["unit"]
    return "date", row["value_date"], row["unit"]


def evaluate_extraction(
    database_path: Path,
    ground_truth_path: Path,
    scenario_id: str,
    extractor_version: str = EXTRACTION_VERSION,
) -> EvaluationResult:
    database_path = Path(database_path).resolve()
    ground_truth_path = Path(ground_truth_path).resolve()
    if not database_path.is_file():
        raise EvaluationError(f"extraction database does not exist: {database_path}")

    events = _load_ground_truth(ground_truth_path, scenario_id)
    try:
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise EvaluationError(
            f"cannot open extraction database {database_path}: {exc}"
        ) from exc

    try:
        initialize_database(connection)
        messages = connection.execute(
            """
            SELECT message_id, source_event_id
            FROM raw_provider_messages
            WHERE scenario_id = ?
            ORDER BY rowid
            """,
            (scenario_id,),
        ).fetchall()
        if not messages:
            raise EvaluationError(
                f"no accepted provider messages found for scenario {scenario_id!r}"
            )
        attempts = connection.execute(
            """
            SELECT message_id, status
            FROM extraction_attempts
            WHERE extractor_version = ?
              AND message_id IN (
                  SELECT message_id FROM raw_provider_messages WHERE scenario_id = ?
              )
              AND status IN ('extracted', 'abstained', 'review_required')
            """,
            (extractor_version, scenario_id),
        ).fetchall()
        attempts_by_message = {row["message_id"]: row["status"] for row in attempts}
        missing_attempts = [
            row["message_id"]
            for row in messages
            if row["message_id"] not in attempts_by_message
        ]
        if missing_attempts:
            raise EvaluationError(
                f"scenario has {len(missing_attempts)} messages without completed "
                f"{extractor_version} attempts"
            )

        claim_rows = connection.execute(
            """
            SELECT c.*
            FROM extracted_claims AS c
            JOIN raw_provider_messages AS m ON m.message_id = c.message_id
            WHERE m.scenario_id = ? AND c.extractor_version = ?
            ORDER BY c.message_id, c.claim_index
            """,
            (scenario_id, extractor_version),
        ).fetchall()
    except (sqlite3.Error, StorageSchemaError) as exc:
        raise EvaluationError(f"extraction evaluation failed: {exc}") from exc
    finally:
        connection.close()

    actual: dict[tuple[str, str], list[ExpectedValue]] = defaultdict(list)
    for row in claim_rows:
        actual[(row["message_id"], row["claim_type"])].append(_actual_value(row))

    counters: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "expected": 0,
            "extracted": 0,
            "correct": 0,
            "missing": 0,
            "incorrect": 0,
            "unexpected": 0,
        }
    )
    expected_keys: set[tuple[str, str]] = set()
    for message in messages:
        event = events.get(message["source_event_id"])
        if event is None:
            raise EvaluationError(
                f"message {message['message_id']} references missing ground-truth "
                f"event {message['source_event_id']}"
            )
        for claim_type, expected_value in _expected_claims(event).items():
            key = (message["message_id"], claim_type)
            expected_keys.add(key)
            values = actual.get(key, [])
            counter = counters[claim_type]
            counter["expected"] += 1
            counter["extracted"] += len(values)
            if not values:
                counter["missing"] += 1
            elif expected_value in values:
                counter["correct"] += 1
                counter["unexpected"] += len(values) - 1
            else:
                counter["incorrect"] += 1
                counter["unexpected"] += max(0, len(values) - 1)

    for (message_id, claim_type), values in actual.items():
        if (message_id, claim_type) not in expected_keys:
            counters[claim_type]["extracted"] += len(values)
            counters[claim_type]["unexpected"] += len(values)

    metrics = tuple(
        FieldMetric(
            claim_type=claim_type,
            expected_count=counts["expected"],
            extracted_count=counts["extracted"],
            correct_count=counts["correct"],
            missing_count=counts["missing"],
            incorrect_count=counts["incorrect"],
            unexpected_count=counts["unexpected"],
        )
        for claim_type, counts in sorted(counters.items())
    )
    return EvaluationResult(
        scenario_id=scenario_id,
        extractor_version=extractor_version,
        message_count=len(messages),
        abstained_count=sum(
            status == "abstained" for status in attempts_by_message.values()
        ),
        review_count=sum(
            status == "review_required" for status in attempts_by_message.values()
        ),
        failed_count=sum(status == "failed" for status in attempts_by_message.values()),
        metrics=metrics,
    )
