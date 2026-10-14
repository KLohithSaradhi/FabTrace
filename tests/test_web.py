from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import httpx
import pytest

from fabtrace.analytics import analyze_orders
from fabtrace.extraction import extract_provider_updates
from fabtrace.ingestion import ingest_provider_updates
from fabtrace.reconciliation import reconcile_order_state
from fabtrace.simulation.runner import run_simulation
from fabtrace.web import create_app
from fabtrace.web.repository import DashboardRepository

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "Data"
SCENARIO = ROOT / ".fabtrace/config/scenarios/stochastic_disruption.json"


class ASGITestClient:
    def __init__(self, app) -> None:
        self.app = app

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        json: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        async def request() -> httpx.Response:
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                return await client.request(
                    method,
                    path,
                    params=params,
                    json=json,
                    headers=headers,
                )

        return asyncio.run(request())

    def get(
        self,
        path: str,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        return self.request("GET", path, params=params)

    def post(
        self,
        path: str,
        json: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        return self.request("POST", path, json=json, headers=headers)

    def put(
        self,
        path: str,
        json: dict,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        return self.request("PUT", path, json=json, headers=headers)


@pytest.fixture
def dashboard_database(tmp_path: Path) -> Path:
    simulation = run_simulation(
        SCENARIO,
        DATA_DIR,
        tmp_path / "runs",
        ("fabtrace", "simulate"),
    )
    database_path = tmp_path / "fabtrace.db"
    ingest_provider_updates(
        simulation.run_directory / "provider_updates.jsonl",
        database_path,
    )
    scenario_id = "stochastic-disruption"
    extract_provider_updates(database_path, scenario_id, "rules-v1")
    reconcile_order_state(database_path, scenario_id, "order-state-v1")
    extract_provider_updates(database_path, scenario_id)
    reconcile_order_state(database_path, scenario_id)
    analyze_orders(database_path, scenario_id, "2026-01-07T00:00:00Z")
    analyze_orders(database_path, scenario_id, "2026-01-16T00:00:00Z")
    return database_path


@pytest.fixture
def client(dashboard_database: Path) -> ASGITestClient:
    return ASGITestClient(create_app(dashboard_database))


def test_health_declares_read_only_mode(client: ASGITestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "database_mode": "read-only",
        "product_config_mode": "local-write",
    }


def test_order_api_returns_current_state_and_versioned_metrics(
    client: ASGITestClient,
) -> None:
    response = client.get(
        "/api/v1/orders",
        params={"scenario_id": "stochastic-disruption"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["api_version"] == "v1"
    assert payload["count"] == 1
    order = payload["orders"][0]
    assert order["purchase_order_id"] == "PO-0001"
    assert order["reconciliation_version"] == "order-state-v2"
    assert order["order_status"] == "DELIVERED"
    assert order["progress_pct"] == 100.0
    assert order["current_forecast_date"] == "2026-01-16"
    assert order["forecast_drift_days"] == 3
    assert order["reported_delay_days"] == 3.0
    assert order["metric_as_of_time"] == "2026-01-16T00:00:00+00:00"
    assert order["state_as_of_time"] == "2026-01-16T00:00:00+00:00"


def test_order_detail_api_preserves_history_and_provenance(
    client: ASGITestClient,
) -> None:
    response = client.get(
        "/api/v1/orders/stochastic-disruption/PO-0001"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["api_version"] == "v1"
    assert len(payload["state_history"]) == 13
    assert len(payload["decisions"]) == 13
    assert len(payload["metric_history"]) == 2
    first_state = payload["state_history"][0]
    latest_state = payload["state_history"][-1]
    assert first_state["snapshot_sequence"] == 1
    assert latest_state["snapshot_sequence"] == 13
    assert latest_state["last_message_id"].endswith("MSG-0013")
    assert latest_state["order_status_source_message_id"].endswith("MSG-0013")
    assert latest_state["progress_pct_source_message_id"].endswith("MSG-0009")
    assert payload["metric_history"][0]["progress_pct"] == 25.0
    assert payload["metric_history"][0]["reported_delay_days"] is None
    assert payload["metric_history"][0]["has_reported_delay"] is False
    assert payload["metric_history"][1]["progress_pct"] == 100.0


def test_provider_api_groups_orders_without_score(client: ASGITestClient) -> None:
    response = client.get("/api/v1/providers")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    provider = payload["providers"][0]
    assert provider["provider_profile_id"] == "P208-synthetic-v1"
    assert provider["source_provider_id"] == "P208"
    assert len(provider["orders"]) == 1
    assert "risk_score" not in provider


def test_unknown_order_returns_404(client: ASGITestClient) -> None:
    response = client.get(
        "/api/v1/orders/stochastic-disruption/PO-DOES-NOT-EXIST"
    )

    assert response.status_code == 404
    assert "does not exist" in response.json()["detail"]


def test_dashboard_requires_registered_customer_plans(
    client: ASGITestClient,
) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "Customer orders in motion." in response.text
    assert "No customer orders found." in response.text
    assert 'hx-get="/fragments/orders"' in response.text
    assert "No composite risk score" in response.text


def test_order_page_renders_timeline_and_boundaries(
    client: ASGITestClient,
) -> None:
    response = client.get("/orders/stochastic-disruption/PO-0001")

    assert response.status_code == 200
    assert "Current provenance" in response.text
    assert "13 snapshots" in response.text
    assert "operational-metrics-v1" in response.text
    assert "not an independent predicted ETA" in response.text
    assert "MSG-0013" in response.text


def test_provider_page_renders_evidence_without_ranking(
    client: ASGITestClient,
) -> None:
    response = client.get("/providers")

    assert response.status_code == 200
    assert "Evidence, not rankings." in response.text
    assert "P208-synthetic-v1" in response.text
    assert "+3 d drift" in response.text


def test_fragment_filters_orders(client: ASGITestClient) -> None:
    present = client.get(
        "/fragments/orders",
        params={"scenario_id": "stochastic-disruption"},
    )
    absent = client.get(
        "/fragments/orders",
        params={"scenario_id": "unknown-scenario"},
    )

    assert present.status_code == 200
    assert "No customer orders found." in present.text
    assert absent.status_code == 200
    assert "No customer orders found." in absent.text


def test_openapi_describes_versioned_response_models(
    client: ASGITestClient,
) -> None:
    response = client.get("/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    assert "/api/v1/orders" in schema["paths"]
    assert "/api/v1/orders/{scenario_id}/{order_id}" in schema["paths"]
    assert "/api/v1/customer-orders" in schema["paths"]
    assert "/api/v1/customer-orders/{order_id}" in schema["paths"]
    assert "OrderSummary" in schema["components"]["schemas"]
    properties = schema["components"]["schemas"]["OrderSummary"]["properties"]
    assert "state_as_of_time" in properties
    assert "metric_as_of_time" in properties
    assert "reported_delay_days" in properties


def test_repository_connection_rejects_writes(
    dashboard_database: Path,
) -> None:
    repository = DashboardRepository(dashboard_database)

    with repository._connection() as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute(
                "UPDATE analytics_runs SET status = 'started'"
            )


def test_missing_database_returns_service_unavailable(tmp_path: Path) -> None:
    client = ASGITestClient(create_app(tmp_path / "missing.db"))

    response = client.get("/api/v1/orders")

    assert response.status_code == 503
    assert "does not exist" in response.json()["detail"]
