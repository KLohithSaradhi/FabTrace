from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from fabtrace.analytics import METRIC_VERSION, analyze_orders
from fabtrace.extraction import EXTRACTION_VERSION, extract_provider_updates
from fabtrace.ingestion import ingest_provider_updates
from fabtrace.planning import register_plan
from fabtrace.reconciliation import RECONCILIATION_VERSION, reconcile_order_state
from fabtrace.simulation.config import resolve_plan_effective_config
from fabtrace.simulation.runner import (
    SimulationResult,
    reuse_plan_simulation,
    run_plan_simulation,
)
from fabtrace.source import load_source_catalog, validate_source_catalog
from fabtrace.storage import initialize_database

PIPELINE_VERSION = "execute-plan-v1"
STAGE_ORDER = (
    "register_plan",
    "simulate",
    "ingest",
    "extract",
    "reconcile",
    "analyze",
)
SOURCE_FILES = (
    "inputs.csv",
    "providers.csv",
    "provision.csv",
    "sequence.csv",
    "stages.csv",
)


class PipelineExecutionError(RuntimeError):
    """Raised when a checkpointed plan pipeline cannot complete."""


@dataclass(frozen=True)
class PipelineStageResult:
    stage_name: str
    status: str
    attempt_number: int
    checkpoint_reused: bool
    detail: dict[str, Any]


@dataclass(frozen=True)
class PipelineExecutionResult:
    pipeline_run_id: str
    customer_order_id: str
    scenario_id: str
    status: str
    stages: tuple[PipelineStageResult, ...]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _append_run_event(
    connection: sqlite3.Connection,
    pipeline_run_id: str,
    status: str,
    error: str | None = None,
) -> None:
    sequence = connection.execute(
        "SELECT COALESCE(MAX(event_sequence), 0) + 1 "
        "FROM pipeline_run_events WHERE pipeline_run_id = ?",
        (pipeline_run_id,),
    ).fetchone()[0]
    connection.execute(
        """
        INSERT INTO pipeline_run_events (
            pipeline_event_id, pipeline_run_id, event_sequence,
            status, recorded_at, error
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (str(uuid4()), pipeline_run_id, sequence, status, _utc_now(), error),
    )
    connection.commit()


def _completed_stages(
    connection: sqlite3.Connection,
    pipeline_run_id: str,
) -> dict[str, tuple[int, dict[str, Any]]]:
    rows = connection.execute(
        """
        SELECT attempt.stage_name, attempt.attempt_number, event.detail_json
        FROM pipeline_stage_attempts AS attempt
        JOIN pipeline_stage_events AS event
          ON event.stage_attempt_id = attempt.stage_attempt_id
        WHERE attempt.pipeline_run_id = ? AND event.status = 'COMPLETED'
        ORDER BY attempt.stage_name, attempt.attempt_number DESC
        """,
        (pipeline_run_id,),
    ).fetchall()
    completed: dict[str, tuple[int, dict[str, Any]]] = {}
    for stage_name, attempt_number, detail_json in rows:
        if stage_name not in completed:
            completed[stage_name] = (
                int(attempt_number),
                json.loads(detail_json) if detail_json else {},
            )
    return completed


def _run_stage(
    connection: sqlite3.Connection,
    pipeline_run_id: str,
    stage_name: str,
    operation: Callable[[], Any],
) -> PipelineStageResult:
    attempt_number = connection.execute(
        "SELECT COALESCE(MAX(attempt_number), 0) + 1 "
        "FROM pipeline_stage_attempts "
        "WHERE pipeline_run_id = ? AND stage_name = ?",
        (pipeline_run_id, stage_name),
    ).fetchone()[0]
    attempt_id = str(uuid4())
    started_at = _utc_now()
    connection.execute(
        """
        INSERT INTO pipeline_stage_attempts (
            stage_attempt_id, pipeline_run_id, stage_name,
            attempt_number, started_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (attempt_id, pipeline_run_id, stage_name, attempt_number, started_at),
    )
    connection.commit()
    started = time.monotonic()
    try:
        detail = _jsonable(operation())
        if not isinstance(detail, dict):
            detail = {"result": detail}
        duration_ms = max(0, round((time.monotonic() - started) * 1000))
        connection.execute(
            """
            INSERT INTO pipeline_stage_events (
                stage_event_id, stage_attempt_id, status, recorded_at,
                duration_ms, detail_json, error
            ) VALUES (?, ?, 'COMPLETED', ?, ?, ?, NULL)
            """,
            (
                str(uuid4()),
                attempt_id,
                _utc_now(),
                duration_ms,
                json.dumps(detail, sort_keys=True),
            ),
        )
        connection.commit()
        return PipelineStageResult(
            stage_name, "COMPLETED", int(attempt_number), False, detail
        )
    except Exception as exc:
        duration_ms = max(0, round((time.monotonic() - started) * 1000))
        connection.execute(
            """
            INSERT INTO pipeline_stage_events (
                stage_event_id, stage_attempt_id, status, recorded_at,
                duration_ms, detail_json, error
            ) VALUES (?, ?, 'FAILED', ?, ?, NULL, ?)
            """,
            (str(uuid4()), attempt_id, _utc_now(), duration_ms, str(exc)),
        )
        connection.commit()
        raise


def _latest_received_at(database_path: Path, scenario_id: str) -> str:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT MAX(received_at) FROM raw_provider_messages WHERE scenario_id = ?",
            (scenario_id,),
        ).fetchone()
    if row is None or row[0] is None:
        raise PipelineExecutionError(
            f"no ingested message time exists for scenario {scenario_id!r}"
        )
    return str(row[0])


def execute_plan_pipeline(
    *,
    plan_path: Path,
    scenario_path: Path,
    data_dir: Path,
    database_path: Path,
    output_dir: Path,
    command: tuple[str, ...] = (),
) -> PipelineExecutionResult:
    plan_path = Path(plan_path).resolve()
    scenario_path = Path(scenario_path).resolve()
    data_dir = Path(data_dir).resolve()
    database_path = Path(database_path).resolve()
    output_dir = Path(output_dir).resolve()

    catalog = load_source_catalog(data_dir)
    validation = validate_source_catalog(catalog)
    if not validation.is_valid:
        raise PipelineExecutionError(
            "source data failed validation: " + "; ".join(validation.errors)
        )
    effective = resolve_plan_effective_config(plan_path, scenario_path, catalog)
    source_hashes = {name: _sha256(data_dir / name) for name in SOURCE_FILES}
    effective_bytes = (effective.model_dump_json(indent=2) + "\n").encode("utf-8")
    identity = {
        "pipeline_version": PIPELINE_VERSION,
        "plan_sha256": effective.plan.sha256,
        "scenario_sha256": _sha256(scenario_path),
        "effective_config_sha256": hashlib.sha256(effective_bytes).hexdigest(),
        "source_files": source_hashes,
        "extractor_version": EXTRACTION_VERSION,
        "reconciliation_version": RECONCILIATION_VERSION,
        "metric_version": METRIC_VERSION,
        "database_path": str(database_path),
        "output_directory": str(output_dir),
    }
    identity_json = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    pipeline_run_id = hashlib.sha256(identity_json.encode("utf-8")).hexdigest()[:32]

    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    try:
        initialize_database(connection)
        existing = connection.execute(
            "SELECT input_fingerprint FROM pipeline_runs WHERE pipeline_run_id = ?",
            (pipeline_run_id,),
        ).fetchone()
        if existing is None:
            connection.execute(
                """
                INSERT INTO pipeline_runs (
                    pipeline_run_id, customer_order_id, scenario_id,
                    plan_sha256, scenario_sha256, input_fingerprint,
                    plan_path, scenario_path, data_directory, database_path,
                    output_directory, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pipeline_run_id,
                    effective.plan.order_id,
                    effective.scenario.scenario_id,
                    effective.plan.sha256,
                    identity["scenario_sha256"],
                    identity_json,
                    str(plan_path),
                    str(scenario_path),
                    str(data_dir),
                    str(database_path),
                    str(output_dir),
                    _utc_now(),
                ),
            )
            connection.commit()
        elif existing[0] != identity_json:
            raise PipelineExecutionError("pipeline run fingerprint collision")

        _append_run_event(connection, pipeline_run_id, "RUNNING")
        completed = _completed_stages(connection, pipeline_run_id)
        stage_results: list[PipelineStageResult] = []

        def execute(stage_name: str, operation: Callable[[], Any]) -> None:
            checkpoint = completed.get(stage_name)
            if checkpoint is not None:
                attempt_number, detail = checkpoint
                stage_results.append(
                    PipelineStageResult(
                        stage_name,
                        "COMPLETED",
                        attempt_number,
                        True,
                        detail,
                    )
                )
                return
            stage_results.append(
                _run_stage(connection, pipeline_run_id, stage_name, operation)
            )

        execute("register_plan", lambda: register_plan(plan_path, database_path))

        simulation_holder: dict[str, SimulationResult] = {}

        def simulate() -> dict[str, Any]:
            reusable = reuse_plan_simulation(
                plan_path, scenario_path, data_dir, output_dir
            )
            result = reusable or run_plan_simulation(
                plan_path,
                scenario_path,
                data_dir,
                output_dir,
                command or ("fabtrace", "pipeline", "execute-plan"),
            )
            simulation_holder["result"] = result
            return {**_jsonable(result), "existing_run_reused": reusable is not None}

        execute("simulate", simulate)
        if "result" not in simulation_holder:
            simulation_detail = stage_results[-1].detail
            simulation_holder["result"] = SimulationResult(
                run_directory=Path(str(simulation_detail["run_directory"])),
                event_count=int(simulation_detail["event_count"]),
                message_count=int(simulation_detail["message_count"]),
            )
        updates_path = simulation_holder["result"].run_directory / "provider_updates.jsonl"
        execute("ingest", lambda: ingest_provider_updates(updates_path, database_path))
        execute(
            "extract",
            lambda: extract_provider_updates(
                database_path, effective.scenario.scenario_id, EXTRACTION_VERSION
            ),
        )
        execute(
            "reconcile",
            lambda: reconcile_order_state(
                database_path,
                effective.scenario.scenario_id,
                RECONCILIATION_VERSION,
            ),
        )
        execute(
            "analyze",
            lambda: analyze_orders(
                database_path,
                effective.scenario.scenario_id,
                _latest_received_at(database_path, effective.scenario.scenario_id),
            ),
        )
        _append_run_event(connection, pipeline_run_id, "COMPLETED")
        return PipelineExecutionResult(
            pipeline_run_id,
            effective.plan.order_id,
            effective.scenario.scenario_id,
            "COMPLETED",
            tuple(stage_results),
        )
    except Exception as exc:
        try:
            _append_run_event(connection, pipeline_run_id, "FAILED", str(exc))
        except (sqlite3.Error, UnboundLocalError):
            pass
        if isinstance(exc, PipelineExecutionError):
            raise
        raise PipelineExecutionError(f"plan pipeline failed: {exc}") from exc
    finally:
        connection.close()
