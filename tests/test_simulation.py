from __future__ import annotations

import json
from pathlib import Path

import pytest

from fabtrace.simulation.config import SimulationConfigError, resolve_effective_config
from fabtrace.simulation.engine import (
    InvalidStateTransition,
    OrderStateMachine,
    derive_random_seed,
)
from fabtrace.simulation.models import OrderState
from fabtrace.simulation.runner import SimulationRunError, run_simulation
from fabtrace.source import load_source_catalog


ROOT = Path(__file__).parents[1]
DATA_DIR = ROOT / "Data"
SCENARIO_PATH = ROOT / ".fabtrace/config/scenarios/happy_path.json"
FAILURE_SCENARIO_PATH = ROOT / ".fabtrace/config/scenarios/equipment_failure.json"
STOCHASTIC_SCENARIO_PATH = (
    ROOT / ".fabtrace/config/scenarios/stochastic_disruption.json"
)


def _run(tmp_path: Path, output_name: str = "runs"):
    return run_simulation(
        scenario_path=SCENARIO_PATH,
        data_dir=DATA_DIR,
        runs_dir=tmp_path / output_name,
        command=("fabtrace", "simulate", "--scenario", str(SCENARIO_PATH)),
    )


def _jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _configured_failure_scenario(tmp_path: Path) -> tuple[dict[str, object], Path]:
    scenario = json.loads(FAILURE_SCENARIO_PATH.read_text(encoding="utf-8"))
    scenario["providers"][0]["profile"] = str(
        ROOT / ".fabtrace/config/providers/P208.json"
    )
    scenario_path = tmp_path / "equipment_failure.json"
    return scenario, scenario_path


def _portable_scenario(source: Path, tmp_path: Path) -> tuple[dict[str, object], Path]:
    scenario = json.loads(source.read_text(encoding="utf-8"))
    scenario["providers"][0]["profile"] = str(
        ROOT / ".fabtrace/config/providers/P208.json"
    )
    scenario_path = tmp_path / source.name
    return scenario, scenario_path


def test_effective_config_enriches_source_identity_and_country() -> None:
    catalog = load_source_catalog(DATA_DIR)

    effective = resolve_effective_config(SCENARIO_PATH, catalog)

    provider = effective.providers[0]
    capability = provider.capabilities[0]
    assert provider.profile_id == "P208-synthetic-v1"
    assert provider.source.provider_id == "P208"
    assert provider.source.provider_name == "Shin-Etsu"
    assert provider.source.country_code == "JPN"
    assert provider.source.country_name == "Japan"
    assert capability.input_id == "N26"
    assert capability.input_name == "Wafer"
    assert capability.provision_year == 2019
    assert capability.share_provided is None


def test_happy_path_emits_approved_timeline_and_linked_messages(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path)
    events = _jsonl(result.run_directory / "ground_truth.jsonl")
    messages = _jsonl(result.run_directory / "provider_updates.jsonl")

    assert result.event_count == 12
    assert result.message_count == 11
    assert [event["state"] for event in events] == [
        "CREATED",
        "ACKNOWLEDGED",
        "QUEUED",
        "IN_PRODUCTION",
        "IN_PRODUCTION",
        "IN_PRODUCTION",
        "IN_PRODUCTION",
        "IN_PRODUCTION",
        "QUALITY_CHECK",
        "READY_TO_SHIP",
        "SHIPPED",
        "DELIVERED",
    ]
    assert [event["sim_time_days"] for event in events] == [
        0.0,
        0.0,
        0.0,
        1.0,
        2.0,
        3.0,
        4.0,
        5.0,
        5.0,
        6.0,
        6.0,
        8.0,
    ]
    assert [events[index]["progress_pct"] for index in range(4, 8)] == [
        25.0,
        50.0,
        75.0,
        100.0,
    ]
    assert events[-1]["event_time"] == "2026-01-13T00:00:00Z"

    event_ids = {event["event_id"] for event in events}
    assert all(message["source_event_id"] in event_ids for message in messages)
    assert len({message["message_id"] for message in messages}) == len(messages)

    manifest = json.loads(
        (result.run_directory / "run_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "completed"
    assert manifest["event_count"] == 12
    assert manifest["message_count"] == 11
    assert set(manifest["source_files"]) == {
        "inputs.csv",
        "providers.csv",
        "provision.csv",
        "sequence.csv",
        "stages.csv",
    }


def test_logical_outputs_are_reproducible(tmp_path: Path) -> None:
    first = _run(tmp_path, "first")
    second = _run(tmp_path, "second")

    for filename in (
        "effective_config.json",
        "ground_truth.jsonl",
        "provider_updates.jsonl",
    ):
        assert (first.run_directory / filename).read_bytes() == (
            second.run_directory / filename
        ).read_bytes()


def test_equipment_failure_pauses_production_and_revises_forecast(
    tmp_path: Path,
) -> None:
    result = run_simulation(
        scenario_path=FAILURE_SCENARIO_PATH,
        data_dir=DATA_DIR,
        runs_dir=tmp_path / "runs",
        command=("fabtrace", "simulate", "--scenario", str(FAILURE_SCENARIO_PATH)),
    )
    events = _jsonl(result.run_directory / "ground_truth.jsonl")
    messages = _jsonl(result.run_directory / "provider_updates.jsonl")

    assert result.event_count == 14
    assert result.message_count == 13

    failure = next(event for event in events if event["event_type"] == "equipment_failure")
    resumed = next(event for event in events if event["event_type"] == "production_resumed")
    delivered = events[-1]

    assert failure["state"] == "BLOCKED"
    assert failure["sim_time_days"] == 2.0
    assert failure["quantity_completed"] == 25
    assert failure["progress_pct"] == 25.0
    assert failure["incident_id"] == "INC-0001"
    assert failure["incident_type"] == "equipment_failure"
    assert failure["reason_code"] == "equipment_calibration"
    assert failure["delay_days"] == 2
    assert failure["previous_forecast_delivery_at"] == "2026-01-13T00:00:00Z"
    assert failure["forecast_delivery_at"] == "2026-01-15T00:00:00Z"

    assert resumed["state"] == "IN_PRODUCTION"
    assert resumed["sim_time_days"] == 4.0
    assert resumed["quantity_completed"] == 25
    assert delivered["state"] == "DELIVERED"
    assert delivered["sim_time_days"] == 10.0
    assert delivered["event_time"] == "2026-01-15T00:00:00Z"

    failure_index = events.index(failure)
    assert all(
        event["forecast_delivery_at"] == "2026-01-15T00:00:00Z"
        for event in events[failure_index:]
    )
    assert not any(
        event["event_type"] == "progress_updated"
        and 2.0 < event["sim_time_days"] < 4.0
        for event in events
    )

    failure_message = next(
        message for message in messages if message["source_event_id"] == failure["event_id"]
    )
    assert "2026-01-15" in failure_message["body"]
    assert "2 days later" in failure_message["body"]


def test_equipment_failure_outputs_are_reproducible(tmp_path: Path) -> None:
    first = run_simulation(
        FAILURE_SCENARIO_PATH,
        DATA_DIR,
        tmp_path / "first",
        ("fabtrace", "simulate"),
    )
    second = run_simulation(
        FAILURE_SCENARIO_PATH,
        DATA_DIR,
        tmp_path / "second",
        ("fabtrace", "simulate"),
    )

    for filename in (
        "effective_config.json",
        "ground_truth.jsonl",
        "provider_updates.jsonl",
    ):
        assert (first.run_directory / filename).read_bytes() == (
            second.run_directory / filename
        ).read_bytes()


def test_seeded_stochastic_failure_is_auditable_and_causal(tmp_path: Path) -> None:
    result = run_simulation(
        STOCHASTIC_SCENARIO_PATH,
        DATA_DIR,
        tmp_path / "runs",
        ("fabtrace", "simulate"),
    )
    events = _jsonl(result.run_directory / "ground_truth.jsonl")
    messages = _jsonl(result.run_directory / "provider_updates.jsonl")
    failures = [event for event in events if event["event_type"] == "equipment_failure"]

    assert len(failures) == 1
    failure = failures[0]
    assert result.event_count == 14
    assert result.message_count == 13
    assert failure["state"] == "BLOCKED"
    assert failure["sim_time_days"] == 3.0
    assert failure["quantity_completed"] == 50
    assert failure["incident_source"] == "stochastic"
    assert failure["random_stream_version"] == 1
    assert failure["random_draw"] == pytest.approx(0.4463425103482396)
    assert failure["trigger_probability"] == 0.5
    assert failure["sampled_downtime_days"] == 3
    assert failure["reason_code"] == "equipment_calibration"
    assert failure["previous_forecast_delivery_at"] == "2026-01-13T00:00:00Z"
    assert failure["forecast_delivery_at"] == "2026-01-16T00:00:00Z"
    assert events[-1]["sim_time_days"] == 11.0
    assert events[-1]["event_time"] == "2026-01-16T00:00:00Z"

    failure_message = next(
        message for message in messages if message["source_event_id"] == failure["event_id"]
    )
    assert "2026-01-16" in failure_message["body"]
    assert "0.446" not in failure_message["body"]
    assert "probability" not in failure_message["body"].lower()
    assert "random_draw" not in failure_message


def test_seeded_stochastic_outputs_are_reproducible(tmp_path: Path) -> None:
    first = run_simulation(
        STOCHASTIC_SCENARIO_PATH,
        DATA_DIR,
        tmp_path / "first",
        ("fabtrace", "simulate"),
    )
    second = run_simulation(
        STOCHASTIC_SCENARIO_PATH,
        DATA_DIR,
        tmp_path / "second",
        ("fabtrace", "simulate"),
    )

    for filename in (
        "effective_config.json",
        "ground_truth.jsonl",
        "provider_updates.jsonl",
    ):
        assert (first.run_directory / filename).read_bytes() == (
            second.run_directory / filename
        ).read_bytes()


def test_seed_46_completes_without_a_stochastic_failure(tmp_path: Path) -> None:
    scenario, scenario_path = _portable_scenario(STOCHASTIC_SCENARIO_PATH, tmp_path)
    scenario["seed"] = 46
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")

    result = run_simulation(
        scenario_path,
        DATA_DIR,
        tmp_path / "runs",
        ("fabtrace", "simulate"),
    )
    events = _jsonl(result.run_directory / "ground_truth.jsonl")

    assert result.event_count == 12
    assert result.message_count == 11
    assert not any(event["event_type"] == "equipment_failure" for event in events)
    assert events[-1]["event_time"] == "2026-01-13T00:00:00Z"


def test_random_seed_derivation_is_order_local_and_stable() -> None:
    original = derive_random_seed(
        42,
        "stochastic-disruption",
        "P208-synthetic-v1",
        "PO-0001",
        1,
    )
    unrelated = derive_random_seed(
        42,
        "stochastic-disruption",
        "P208-synthetic-v1",
        "PO-UNRELATED",
        1,
    )

    assert original == 12035851683012372453
    assert original != unrelated
    assert original == derive_random_seed(
        42,
        "stochastic-disruption",
        "P208-synthetic-v1",
        "PO-0001",
        1,
    )


def test_existing_run_directory_is_not_overwritten(tmp_path: Path) -> None:
    first = _run(tmp_path)
    truth_before = (first.run_directory / "ground_truth.jsonl").read_bytes()

    with pytest.raises(SimulationRunError, match="will not be overwritten"):
        _run(tmp_path)

    assert (first.run_directory / "ground_truth.jsonl").read_bytes() == truth_before


def test_unprovided_capability_is_rejected(tmp_path: Path) -> None:
    profile = json.loads(
        (ROOT / ".fabtrace/config/providers/P208.json").read_text(encoding="utf-8")
    )
    profile["capabilities"][0]["source_capability_id"] = "N20"
    profile_path = tmp_path / "provider.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    scenario = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
    scenario["providers"][0]["profile"] = "provider.json"
    scenario["purchase_orders"][0]["capability_id"] = "N20"
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")

    catalog = load_source_catalog(DATA_DIR)
    with pytest.raises(SimulationConfigError, match="does not show P208 providing N20"):
        resolve_effective_config(scenario_path, catalog)


def test_incident_referencing_unknown_order_is_rejected(tmp_path: Path) -> None:
    scenario, scenario_path = _configured_failure_scenario(tmp_path)
    scenario["incidents"][0]["purchase_order_id"] = "PO-MISSING"
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")

    catalog = load_source_catalog(DATA_DIR)
    with pytest.raises(SimulationConfigError, match="references unknown order"):
        resolve_effective_config(scenario_path, catalog)


def test_incident_requires_positive_downtime(tmp_path: Path) -> None:
    scenario, scenario_path = _configured_failure_scenario(tmp_path)
    scenario["incidents"][0]["downtime_days"] = 0
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")

    catalog = load_source_catalog(DATA_DIR)
    with pytest.raises(SimulationConfigError, match="greater than 0"):
        resolve_effective_config(scenario_path, catalog)


def test_explicit_and_stochastic_incidents_are_mutually_exclusive(
    tmp_path: Path,
) -> None:
    scenario, scenario_path = _portable_scenario(STOCHASTIC_SCENARIO_PATH, tmp_path)
    explicit = json.loads(FAILURE_SCENARIO_PATH.read_text(encoding="utf-8"))[
        "incidents"
    ][0]
    scenario["incidents"] = [explicit]
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")

    catalog = load_source_catalog(DATA_DIR)
    with pytest.raises(SimulationConfigError, match="mutually exclusive"):
        resolve_effective_config(scenario_path, catalog)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("daily_failure_probability", 1.1),
        ("downtime_weight", 0),
    ],
)
def test_invalid_disruption_distribution_is_rejected(
    tmp_path: Path,
    field: str,
    value: float,
) -> None:
    profile = json.loads(
        (ROOT / ".fabtrace/config/providers/P208.json").read_text(encoding="utf-8")
    )
    if field == "daily_failure_probability":
        profile["disruption_model"]["daily_failure_probability"] = value
    else:
        profile["disruption_model"]["downtime_distribution"][0]["weight"] = value
    profile_path = tmp_path / "provider.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    scenario = json.loads(STOCHASTIC_SCENARIO_PATH.read_text(encoding="utf-8"))
    scenario["providers"][0]["profile"] = str(profile_path)
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")

    catalog = load_source_catalog(DATA_DIR)
    with pytest.raises(SimulationConfigError):
        resolve_effective_config(scenario_path, catalog)


def test_illegal_state_transition_is_rejected() -> None:
    machine = OrderStateMachine()

    with pytest.raises(InvalidStateTransition, match="cannot transition"):
        machine.transition(OrderState.SHIPPED)
