from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fabtrace.cli import main
from fabtrace.extraction import evaluate_extraction, extract_provider_updates
from fabtrace.ingestion import ingest_provider_updates
from fabtrace.planning import PlanArtifactStore, PlanOrderRequest, build_order_plan
from fabtrace.products import ProductRecipe
from fabtrace.reconciliation import reconcile_order_state
from fabtrace.simulation.config import (
    SimulationConfigError,
    resolve_plan_effective_config,
)
from fabtrace.simulation.runner import SimulationRunError, run_plan_simulation
from fabtrace.source import load_source_catalog
from fabtrace.web.repository import DashboardRepository

ROOT = Path(__file__).parents[1]
DATA_DIR = ROOT / "Data"
P208 = ROOT / ".fabtrace/config/providers/P208.json"
P226 = ROOT / ".fabtrace/config/providers/P226.json"


def _published_recipe() -> ProductRecipe:
    return ProductRecipe.model_validate(
        {
            "product_id": "provider-chain",
            "product_name": "Provider chain",
            "status": "published",
            "product_version": 1,
            "updated_at": "2026-01-01T00:00:00Z",
            "output_node_id": "finished-unit",
            "nodes": {
                "wafer-input": {
                    "name": "Wafer input",
                    "kind": "material",
                    "unit": "wafer",
                    "lead_time_days": 2,
                    "eto_reference_id": "N26",
                    "provider_candidates": ["P226"],
                    "position": {"x": 0, "y": 0},
                    "requires": [],
                },
                "wafer-process": {
                    "name": "Wafer process",
                    "kind": "process",
                    "unit": "wafer",
                    "duration_days": 2,
                    "eto_reference_id": "N26",
                    "provider_candidates": ["P208"],
                    "position": {"x": 200, "y": 0},
                    "requires": [
                        {
                            "node_id": "wafer-input",
                            "quantity": 1,
                            "unit": "wafer",
                        }
                    ],
                },
                "finished-unit": {
                    "name": "Finished unit",
                    "kind": "output",
                    "unit": "chip",
                    "provider_candidates": [],
                    "position": {"x": 400, "y": 0},
                    "requires": [
                        {
                            "node_id": "wafer-process",
                            "quantity": 1,
                            "unit": "wafer",
                        }
                    ],
                },
            },
        }
    )


def _write_plan(tmp_path: Path) -> Path:
    plan = build_order_plan(
        _published_recipe(),
        "a" * 64,
        PlanOrderRequest(
            order_id="CHAIN-001",
            product_id="provider-chain",
            product_version=1,
            quantity=40,
            requested_at=datetime(2026, 1, 5, tzinfo=UTC),
        ),
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    return PlanArtifactStore(tmp_path / "plans").save(plan)


def _write_scenario(
    tmp_path: Path,
    *,
    providers: list[Path] | None = None,
    stochastic: bool = False,
) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "plan-execution.json"
    payload = {
        "schema_version": "1.0",
        "scenario_id": "chain-execution",
        "scenario_version": 1,
        "seed": 42,
        "synthetic": True,
        "providers": [
            {"profile": str(profile)}
            for profile in (providers or [P208, P226])
        ],
    }
    if stochastic:
        payload["incident_generation"] = {
            "mode": "seeded_stochastic",
            "model": "provider_profile",
            "random_stream_version": 1,
        }
    path.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    return path


def _jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_plan_config_maps_source_providers_capabilities_and_country(
    tmp_path: Path,
) -> None:
    plan_path = _write_plan(tmp_path)
    scenario_path = _write_scenario(tmp_path)

    effective = resolve_plan_effective_config(
        plan_path,
        scenario_path,
        load_source_catalog(DATA_DIR),
    )

    assert {provider.source.provider_id for provider in effective.providers} == {
        "P208",
        "P226",
    }
    countries = {
        provider.source.provider_id: provider.source.country_name
        for provider in effective.providers
    }
    assert countries == {"P208": "Japan", "P226": "Taiwan"}
    assert {order.capability_id for order in effective.provider_orders} == {"N26"}
    assert effective.plan.sha256


def test_dependency_delays_downstream_actor_and_messages_remain_linked(
    tmp_path: Path,
) -> None:
    plan_path = _write_plan(tmp_path)
    scenario_path = _write_scenario(tmp_path)

    result = run_plan_simulation(
        plan_path,
        scenario_path,
        DATA_DIR,
        tmp_path / "runs",
        ("fabtrace", "simulate-plan"),
    )
    events = _jsonl(result.run_directory / "ground_truth.jsonl")
    messages = _jsonl(result.run_directory / "provider_updates.jsonl")
    created = {
        event["purchase_order_id"]: event
        for event in events
        if event["event_type"] == "order_created"
    }
    delivered = {
        event["purchase_order_id"]: event
        for event in events
        if event["event_type"] == "order_delivered"
    }
    upstream_id = "CHAIN-001-PO-001"
    downstream_id = "CHAIN-001-WO-002"

    assert result.event_count == 20
    assert result.message_count == 18
    assert created[upstream_id]["event_time"] == "2026-01-05T00:00:00Z"
    assert delivered[upstream_id]["event_time"] == "2026-01-12T00:00:00Z"
    assert created[downstream_id]["event_time"] == delivered[upstream_id]["event_time"]
    assert events.index(delivered[upstream_id]) < events.index(created[downstream_id])
    assert delivered[downstream_id]["event_time"] == "2026-01-18T00:00:00Z"
    assert [event["event_time"] for event in events] == sorted(
        event["event_time"] for event in events
    )
    event_ids = {event["event_id"] for event in events}
    assert all(message["source_event_id"] in event_ids for message in messages)
    work_delivery = next(
        message
        for message in messages
        if message["purchase_order_id"] == downstream_id
        and message["subject"] == "Work output delivered"
    )
    assert "handoff" in work_delivery["body"].lower()

    manifest = json.loads(
        (result.run_directory / "run_manifest.json").read_text()
    )
    assert manifest["plan_order_id"] == "CHAIN-001"
    assert manifest["plan_sha256"]
    assert (result.run_directory / "source_plan.json").read_bytes() == (
        plan_path.read_bytes()
    )


def test_plan_simulation_outputs_are_reproducible(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path)
    scenario_path = _write_scenario(tmp_path)
    first = run_plan_simulation(
        plan_path,
        scenario_path,
        DATA_DIR,
        tmp_path / "first",
        ("fabtrace", "simulate-plan"),
    )
    second = run_plan_simulation(
        plan_path,
        scenario_path,
        DATA_DIR,
        tmp_path / "second",
        ("fabtrace", "simulate-plan"),
    )

    for filename in (
        "source_plan.json",
        "effective_config.json",
        "ground_truth.jsonl",
        "provider_updates.jsonl",
    ):
        assert (first.run_directory / filename).read_bytes() == (
            second.run_directory / filename
        ).read_bytes()


def test_plan_messages_reach_versioned_order_state_and_dashboard(
    tmp_path: Path,
) -> None:
    simulation = run_plan_simulation(
        _write_plan(tmp_path),
        _write_scenario(tmp_path),
        DATA_DIR,
        tmp_path / "runs",
        ("fabtrace", "simulate-plan"),
    )
    database_path = tmp_path / "fabtrace.db"
    ingest_provider_updates(
        simulation.run_directory / "provider_updates.jsonl",
        database_path,
    )

    extraction = extract_provider_updates(database_path, "chain-execution")
    evaluation = evaluate_extraction(
        database_path,
        simulation.run_directory / "ground_truth.jsonl",
        "chain-execution",
    )
    reconciliation = reconcile_order_state(database_path, "chain-execution")
    orders = DashboardRepository(database_path).list_orders("chain-execution")

    assert extraction.extractor_version == "rules-v2"
    assert extraction.selected_count == 18
    assert extraction.extracted_count == 18
    assert extraction.abstained_count == 0
    assert extraction.review_count == 0
    assert evaluation.message_count == 18
    assert evaluation.correct_count == evaluation.expected_count, evaluation.metrics
    assert reconciliation.reconciliation_version == "order-state-v2"
    assert reconciliation.applied_count == 18
    assert reconciliation.conflict_count == 0
    assert {order["purchase_order_id"] for order in orders} == {
        "CHAIN-001-PO-001",
        "CHAIN-001-WO-002",
    }
    assert {order["order_status"] for order in orders} == {"DELIVERED"}
    assert {order["reconciliation_version"] for order in orders} == {
        "order-state-v2"
    }


def test_upstream_disruption_causally_delays_downstream_release(
    tmp_path: Path,
) -> None:
    profile = json.loads(P226.read_text(encoding="utf-8"))
    profile["disruption_model"]["daily_failure_probability"] = 1
    profile["disruption_model"]["downtime_distribution"] = [
        {"days": 2, "weight": 1}
    ]
    disrupted_profile = tmp_path / "P226-disrupted.json"
    disrupted_profile.write_text(json.dumps(profile), encoding="utf-8")
    result = run_plan_simulation(
        _write_plan(tmp_path),
        _write_scenario(
            tmp_path,
            providers=[P208, disrupted_profile],
            stochastic=True,
        ),
        DATA_DIR,
        tmp_path / "runs",
        ("fabtrace", "simulate-plan"),
    )
    events = _jsonl(result.run_directory / "ground_truth.jsonl")
    upstream_id = "CHAIN-001-PO-001"
    downstream_id = "CHAIN-001-WO-002"
    upstream_failure = next(
        event
        for event in events
        if event["purchase_order_id"] == upstream_id
        and event["event_type"] == "equipment_failure"
    )
    upstream_delivery = next(
        event
        for event in events
        if event["purchase_order_id"] == upstream_id
        and event["event_type"] == "order_delivered"
    )
    downstream_created = next(
        event
        for event in events
        if event["purchase_order_id"] == downstream_id
        and event["event_type"] == "order_created"
    )

    assert upstream_failure["delay_days"] == 2
    assert upstream_delivery["event_time"] == "2026-01-14T00:00:00Z"
    assert downstream_created["event_time"] == upstream_delivery["event_time"]


def test_plan_messages_are_accepted_by_existing_ingestion(tmp_path: Path) -> None:
    result = run_plan_simulation(
        _write_plan(tmp_path),
        _write_scenario(tmp_path),
        DATA_DIR,
        tmp_path / "runs",
        ("fabtrace", "simulate-plan"),
    )

    ingestion = ingest_provider_updates(
        result.run_directory / "provider_updates.jsonl",
        tmp_path / "fabtrace.db",
    )

    assert ingestion.accepted_count == result.message_count
    assert ingestion.quarantined_count == 0


def test_missing_plan_provider_profile_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SimulationConfigError, match="P226 has no execution profile"):
        resolve_plan_effective_config(
            _write_plan(tmp_path),
            _write_scenario(tmp_path, providers=[P208]),
            load_source_catalog(DATA_DIR),
        )


def test_plan_order_without_eto_capability_is_rejected(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path)
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    payload["provider_orders"][0]["capability_id"] = None
    plan_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SimulationConfigError, match="has no ETO capability ID"):
        resolve_plan_effective_config(
            plan_path,
            _write_scenario(tmp_path),
            load_source_catalog(DATA_DIR),
        )


def test_cyclic_provider_order_dependencies_are_rejected(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path)
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    first, second = payload["provider_orders"]
    first["depends_on_provider_order_ids"] = [second["provider_order_id"]]
    second["depends_on_provider_order_ids"] = [first["provider_order_id"]]
    plan_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SimulationConfigError, match="contain a cycle"):
        resolve_plan_effective_config(
            plan_path,
            _write_scenario(tmp_path),
            load_source_catalog(DATA_DIR),
        )


def test_plan_run_directory_is_immutable(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path)
    scenario_path = _write_scenario(tmp_path)
    first = run_plan_simulation(
        plan_path,
        scenario_path,
        DATA_DIR,
        tmp_path / "runs",
        ("fabtrace", "simulate-plan"),
    )
    before = (first.run_directory / "ground_truth.jsonl").read_bytes()

    with pytest.raises(SimulationRunError, match="will not be overwritten"):
        run_plan_simulation(
            plan_path,
            scenario_path,
            DATA_DIR,
            tmp_path / "runs",
            ("fabtrace", "simulate-plan"),
        )
    assert (first.run_directory / "ground_truth.jsonl").read_bytes() == before


def test_simulate_plan_cli(tmp_path: Path, capsys) -> None:
    output = tmp_path / "cli-runs"
    status = main(
        [
            "simulate-plan",
            "--plan",
            str(_write_plan(tmp_path)),
            "--scenario",
            str(_write_scenario(tmp_path)),
            "--data-dir",
            str(DATA_DIR),
            "--output-dir",
            str(output),
        ]
    )

    assert status == 0
    assert "Plan simulation: PASS" in capsys.readouterr().out
    assert (output / "CHAIN-001-chain-execution-seed-42").is_dir()
