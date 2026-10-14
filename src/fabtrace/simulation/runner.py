from __future__ import annotations

import hashlib
import json
import platform
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from fabtrace import __version__
from fabtrace.simulation.config import (
    resolve_effective_config,
    resolve_plan_effective_config,
)
from fabtrace.simulation.engine import ProviderSimulation
from fabtrace.simulation.plan_engine import PlanProviderSimulation
from fabtrace.simulation.models import RunManifest, SimulationModel
from fabtrace.source import load_source_catalog, validate_source_catalog


class SimulationRunError(RuntimeError):
    """Raised when a simulation run cannot safely start or finish."""


@dataclass(frozen=True)
class SimulationResult:
    run_directory: Path
    event_count: int
    message_count: int


def _json_bytes(model: SimulationModel) -> bytes:
    return (model.model_dump_json(indent=2) + "\n").encode("utf-8")


def _write_json(path: Path, model: SimulationModel) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(_json_bytes(model))
    temporary.replace(path)


def _write_jsonl(path: Path, records: tuple[SimulationModel, ...]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as output:
        for record in records:
            output.write(record.model_dump_json(exclude_none=True))
            output.write("\n")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_hashes(data_dir: Path) -> dict[str, str]:
    return {
        name: _sha256(data_dir / name)
        for name in (
            "inputs.csv",
            "providers.csv",
            "provision.csv",
            "sequence.csv",
            "stages.csv",
        )
    }


def reuse_plan_simulation(
    plan_path: Path,
    scenario_path: Path,
    data_dir: Path,
    runs_dir: Path,
) -> SimulationResult | None:
    """Return a verified completed plan run, or None when no run exists."""
    plan_path = Path(plan_path).resolve()
    scenario_path = Path(scenario_path).resolve()
    data_dir = Path(data_dir).resolve()
    runs_dir = Path(runs_dir).resolve()
    catalog = load_source_catalog(data_dir)
    validation = validate_source_catalog(catalog)
    if not validation.is_valid:
        raise SimulationRunError(
            "source data failed validation: " + "; ".join(validation.errors)
        )
    effective = resolve_plan_effective_config(plan_path, scenario_path, catalog)
    run_id = (
        f"{effective.plan.order_id}-{effective.scenario.scenario_id}-"
        f"seed-{effective.scenario.seed}"
    )
    run_directory = runs_dir / run_id
    if not run_directory.exists():
        return None
    if not run_directory.is_dir():
        raise SimulationRunError(
            f"expected simulation run directory is not a directory: {run_directory}"
        )
    manifest_path = run_directory / "run_manifest.json"
    effective_path = run_directory / "effective_config.json"
    plan_snapshot_path = run_directory / "source_plan.json"
    events_path = run_directory / "ground_truth.jsonl"
    messages_path = run_directory / "provider_updates.jsonl"
    required = (
        manifest_path,
        effective_path,
        plan_snapshot_path,
        events_path,
        messages_path,
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise SimulationRunError(
            "existing simulation run is incomplete and will not be overwritten; "
            "missing: " + ", ".join(missing)
        )
    try:
        manifest = RunManifest.model_validate_json(manifest_path.read_bytes())
    except (OSError, ValidationError) as exc:
        raise SimulationRunError(
            f"existing simulation manifest is invalid: {exc}"
        ) from exc
    expected_effective_hash = hashlib.sha256(_json_bytes(effective)).hexdigest()
    expected_plan_hash = _sha256(plan_path)
    checks = {
        "run ID": (manifest.run_id, run_id),
        "status": (manifest.status, "completed"),
        "plan fingerprint": (manifest.plan_sha256, expected_plan_hash),
        "plan snapshot fingerprint": (_sha256(plan_snapshot_path), expected_plan_hash),
        "effective configuration fingerprint": (
            manifest.effective_config_sha256,
            expected_effective_hash,
        ),
        "effective configuration file": (
            _sha256(effective_path),
            expected_effective_hash,
        ),
        "source fingerprints": (manifest.source_files, _source_hashes(data_dir)),
    }
    mismatches = [
        label for label, (actual, expected) in checks.items() if actual != expected
    ]
    event_count = sum(1 for line in events_path.read_bytes().splitlines() if line)
    message_count = sum(1 for line in messages_path.read_bytes().splitlines() if line)
    if event_count != manifest.event_count:
        mismatches.append("ground-truth record count")
    if message_count != manifest.message_count:
        mismatches.append("provider-message record count")
    if mismatches:
        raise SimulationRunError(
            "existing simulation run does not match the requested inputs and will "
            "not be overwritten: " + ", ".join(mismatches)
        )
    return SimulationResult(run_directory, event_count, message_count)


def run_simulation(
    scenario_path: Path,
    data_dir: Path,
    runs_dir: Path,
    command: tuple[str, ...],
) -> SimulationResult:
    scenario_path = Path(scenario_path).resolve()
    data_dir = Path(data_dir).resolve()
    runs_dir = Path(runs_dir).resolve()

    catalog = load_source_catalog(data_dir)
    validation = validate_source_catalog(catalog)
    if not validation.is_valid:
        raise SimulationRunError(
            "source data failed validation: " + "; ".join(validation.errors)
        )
    effective = resolve_effective_config(scenario_path, catalog)

    run_id = f"{effective.scenario.scenario_id}-seed-{effective.scenario.seed}"
    run_directory = runs_dir / run_id
    try:
        run_directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise SimulationRunError(
            f"run directory already exists and will not be overwritten: {run_directory}"
        ) from exc

    effective_path = run_directory / "effective_config.json"
    effective_bytes = _json_bytes(effective)
    effective_path.write_bytes(effective_bytes)
    effective_hash = hashlib.sha256(effective_bytes).hexdigest()

    created_at = datetime.now(UTC)
    manifest = RunManifest(
        run_id=run_id,
        status="started",
        created_at=created_at,
        generator_version=__version__,
        python_version=platform.python_version(),
        command=command,
        scenario_path=str(scenario_path),
        data_directory=str(data_dir),
        source_files=_source_hashes(data_dir),
        effective_config_sha256=effective_hash,
        event_count=0,
        message_count=0,
    )
    manifest_path = run_directory / "run_manifest.json"
    _write_json(manifest_path, manifest)

    try:
        events, messages = ProviderSimulation(effective).run()
        _write_jsonl(run_directory / "ground_truth.jsonl", events)
        _write_jsonl(run_directory / "provider_updates.jsonl", messages)
    except Exception as exc:
        failed = manifest.model_copy(
            update={
                "status": "failed",
                "completed_at": datetime.now(UTC),
                "error": str(exc),
            }
        )
        _write_json(manifest_path, failed)
        raise

    completed = manifest.model_copy(
        update={
            "status": "completed",
            "completed_at": datetime.now(UTC),
            "event_count": len(events),
            "message_count": len(messages),
        }
    )
    _write_json(manifest_path, completed)
    return SimulationResult(
        run_directory=run_directory,
        event_count=len(events),
        message_count=len(messages),
    )


def run_plan_simulation(
    plan_path: Path,
    scenario_path: Path,
    data_dir: Path,
    runs_dir: Path,
    command: tuple[str, ...],
) -> SimulationResult:
    plan_path = Path(plan_path).resolve()
    scenario_path = Path(scenario_path).resolve()
    data_dir = Path(data_dir).resolve()
    runs_dir = Path(runs_dir).resolve()

    catalog = load_source_catalog(data_dir)
    validation = validate_source_catalog(catalog)
    if not validation.is_valid:
        raise SimulationRunError(
            "source data failed validation: " + "; ".join(validation.errors)
        )
    effective = resolve_plan_effective_config(plan_path, scenario_path, catalog)
    run_id = (
        f"{effective.plan.order_id}-{effective.scenario.scenario_id}-"
        f"seed-{effective.scenario.seed}"
    )
    run_directory = runs_dir / run_id
    try:
        run_directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise SimulationRunError(
            f"run directory already exists and will not be overwritten: {run_directory}"
        ) from exc

    try:
        plan_bytes = plan_path.read_bytes()
        if hashlib.sha256(plan_bytes).hexdigest() != effective.plan.sha256:
            raise SimulationRunError(
                "plan artifact changed while the simulation run was starting"
            )
        plan_snapshot_path = run_directory / "source_plan.json"
        plan_snapshot_path.write_bytes(plan_bytes)
        effective_path = run_directory / "effective_config.json"
        effective_bytes = _json_bytes(effective)
        effective_path.write_bytes(effective_bytes)
    except SimulationRunError:
        raise
    except OSError as exc:
        raise SimulationRunError(f"cannot create plan-run inputs: {exc}") from exc
    effective_hash = hashlib.sha256(effective_bytes).hexdigest()

    created_at = datetime.now(UTC)
    manifest = RunManifest(
        run_id=run_id,
        status="started",
        created_at=created_at,
        generator_version=__version__,
        python_version=platform.python_version(),
        command=command,
        scenario_path=str(scenario_path),
        plan_path=str(plan_path),
        plan_sha256=effective.plan.sha256,
        plan_order_id=effective.plan.order_id,
        data_directory=str(data_dir),
        source_files=_source_hashes(data_dir),
        effective_config_sha256=effective_hash,
        event_count=0,
        message_count=0,
    )
    manifest_path = run_directory / "run_manifest.json"
    _write_json(manifest_path, manifest)

    try:
        events, messages = PlanProviderSimulation(effective).run()
        _write_jsonl(run_directory / "ground_truth.jsonl", events)
        _write_jsonl(run_directory / "provider_updates.jsonl", messages)
    except Exception as exc:
        failed = manifest.model_copy(
            update={
                "status": "failed",
                "completed_at": datetime.now(UTC),
                "error": str(exc),
            }
        )
        _write_json(manifest_path, failed)
        raise

    completed = manifest.model_copy(
        update={
            "status": "completed",
            "completed_at": datetime.now(UTC),
            "event_count": len(events),
            "message_count": len(messages),
        }
    )
    _write_json(manifest_path, completed)
    return SimulationResult(
        run_directory=run_directory,
        event_count=len(events),
        message_count=len(messages),
    )
