from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from fabtrace.analytics import build_customer_order_projection
from fabtrace.cli import main
from fabtrace.extraction import extract_provider_updates
from fabtrace.ingestion import ingest_provider_updates
from fabtrace.planning import PlanRegistrationError, register_plan
from fabtrace.reconciliation import reconcile_order_state
from fabtrace.simulation.runner import run_plan_simulation
from fabtrace.web import create_app
from fabtrace.web.repository import DashboardRepository
from test_plan_simulation import DATA_DIR, _write_plan, _write_scenario
from test_web import ASGITestClient


def test_plan_registration_is_idempotent_and_append_only(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path)
    database_path = tmp_path / "fabtrace.db"

    first = register_plan(plan_path, database_path)
    replay = register_plan(plan_path, database_path)

    assert first.registered is True
    assert replay.registered is False
    assert first.plan_sha256 == replay.plan_sha256
    assert first.provider_order_count == 2
    assert first.dependency_count == 1
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert connection.execute(
            "SELECT COUNT(*) FROM registered_plans"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM planned_provider_orders"
        ).fetchone()[0] == 2
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE registered_plans SET product_id = 'changed'"
            )

    changed = json.loads(plan_path.read_text(encoding="utf-8"))
    changed["requested_quantity"] = 41
    conflict_path = tmp_path / "conflicting-plan.json"
    conflict_path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(PlanRegistrationError, match="different plan fingerprint"):
        register_plan(conflict_path, database_path)


def test_register_plan_cli_reports_registration_and_replay(
    tmp_path: Path,
    capsys,
) -> None:
    plan_path = _write_plan(tmp_path)
    database_path = tmp_path / "fabtrace.db"
    arguments = [
        "register",
        "plan",
        "--plan",
        str(plan_path),
        "--database",
        str(database_path),
    ]

    assert main(arguments) == 0
    assert "Result: registered" in capsys.readouterr().out
    assert main(arguments) == 0
    assert "Result: replay" in capsys.readouterr().out


def test_customer_projection_uses_counts_and_critical_path_state() -> None:
    plan = {
        "order_id": "CUSTOMER-1",
        "product_id": "chip",
        "product_version": 1,
        "requested_quantity": 10.0,
        "output_unit": "chip",
        "requested_at": "2026-01-01T00:00:00+00:00",
        "planned_delivery_at": "2026-01-10T00:00:00+00:00",
        "plan_sha256": "abc",
    }
    orders = [
        {"provider_order_id": "CUSTOMER-1-PO-001", "critical_path": 1},
        {"provider_order_id": "CUSTOMER-1-WO-002", "critical_path": 0},
    ]
    dependencies = [
        {
            "provider_order_id": "CUSTOMER-1-WO-002",
            "depends_on_provider_order_id": "CUSTOMER-1-PO-001",
        }
    ]
    states = [
        {
            "purchase_order_id": "CUSTOMER-1-PO-001",
            "order_status": "BLOCKED",
            "scenario_id": "execution",
            "state_as_of_time": "2026-01-03T00:00:00+00:00",
            "forecast_delivery_date": "2026-01-12",
            "current_forecast_date": None,
        }
    ]

    projection = build_customer_order_projection(
        plan, orders, dependencies, states, "execution"
    )

    assert projection["status"] == "BLOCKED"
    assert projection["completed_provider_order_count"] == 0
    assert projection["provider_order_count"] == 2
    assert projection["critical_path_blocked_count"] == 1
    assert "progress_pct" not in projection


def test_registered_plan_rolls_execution_up_to_customer_order(
    tmp_path: Path,
) -> None:
    plan_path = _write_plan(tmp_path)
    scenario_path = _write_scenario(tmp_path)
    database_path = tmp_path / "fabtrace.db"
    register_plan(plan_path, database_path)
    repository = DashboardRepository(database_path)

    planned = repository.list_customer_orders()
    assert len(planned) == 1
    assert planned[0]["order_id"] == "CHAIN-001"
    assert planned[0]["status"] == "PLANNED"
    assert planned[0]["scenario_id"] is None
    assert planned[0]["delivery_estimate_source"] == "planned_fallback"

    simulation = run_plan_simulation(
        plan_path,
        scenario_path,
        DATA_DIR,
        tmp_path / "runs",
        ("fabtrace", "simulate-plan"),
    )
    ingest_provider_updates(
        simulation.run_directory / "provider_updates.jsonl",
        database_path,
    )
    extract_provider_updates(database_path, "chain-execution")
    reconcile_order_state(database_path, "chain-execution")

    executed = repository.list_customer_orders("chain-execution")
    assert len(executed) == 1
    customer = executed[0]
    assert customer["status"] == "DELIVERED"
    assert customer["completed_provider_order_count"] == 2
    assert customer["reported_provider_order_count"] == 2
    assert customer["delivery_estimate_source"] == "provider_reported"
    assert customer["delivery_drift_days"] > 0
    detail = repository.get_customer_order("CHAIN-001", "chain-execution")
    assert len(detail["provider_orders"]) == 2
    assert all(item["execution"] for item in detail["provider_orders"])
    terminal = [item for item in detail["provider_orders"] if item["terminal"]]
    assert [item["provider_order_id"] for item in terminal] == [
        "CHAIN-001-WO-002"
    ]

    client = ASGITestClient(create_app(database_path))
    api = client.get(
        "/api/v1/customer-orders",
        params={"scenario_id": "chain-execution"},
    )
    page = client.get(
        "/customer-orders/CHAIN-001",
        params={"scenario_id": "chain-execution"},
    )
    dashboard = client.get("/")
    assert api.status_code == 200
    assert api.json()["customer_orders"][0]["order_id"] == "CHAIN-001"
    assert page.status_code == 200
    assert "Provider orders" in page.text
    assert "CHAIN-001-WO-002" in page.text
    assert dashboard.status_code == 200
    assert "CHAIN-001" in dashboard.text
