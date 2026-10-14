from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fabtrace.cli import main
from fabtrace.planning import (
    PlanArtifactStore,
    PlanOrderRequest,
    PlanningConflictError,
    build_order_plan,
    plan_order,
)
from fabtrace.planning.storage import PlanAlreadyExistsError, PlanStorageError
from fabtrace.products import ProductConfigStore, ProductRecipe
from fabtrace.web import create_app
from test_web import ASGITestClient

EDITOR_HEADERS = {"X-FabTrace-Editor": "1"}


def _recipe() -> dict:
    return {
        "schema_version": "1.0",
        "product_id": "parallel-product",
        "product_name": "Parallel product",
        "description": "Hand-computable planner fixture",
        "synthetic": True,
        "status": "draft",
        "product_version": None,
        "updated_at": None,
        "output_node_id": "finished-unit",
        "nodes": {
            "material-a": {
                "name": "Material A",
                "kind": "material",
                "unit": "kg",
                "lead_time_days": 2,
                "duration_days": None,
                "eto_reference_id": "I1",
                "provider_candidates": ["P-A-FIRST", "P-A-SECOND"],
                "position": {"x": 0, "y": 0},
                "requires": [],
            },
            "process-a": {
                "name": "Process A",
                "kind": "process",
                "unit": "assembly",
                "lead_time_days": None,
                "duration_days": 4,
                "eto_reference_id": "S1",
                "provider_candidates": ["P-PROCESS"],
                "position": {"x": 200, "y": 0},
                "requires": [
                    {"node_id": "material-a", "quantity": 0.5, "unit": "kg"}
                ],
            },
            "material-b": {
                "name": "Material B",
                "kind": "material",
                "unit": "chiplet",
                "lead_time_days": 3,
                "duration_days": None,
                "eto_reference_id": "I2",
                "provider_candidates": ["P-B"],
                "position": {"x": 0, "y": 200},
                "requires": [],
            },
            "quality-check": {
                "name": "Quality capability",
                "kind": "capability",
                "unit": "check",
                "lead_time_days": None,
                "duration_days": None,
                "eto_reference_id": None,
                "provider_candidates": [],
                "position": {"x": 200, "y": 200},
                "requires": [],
            },
            "finished-unit": {
                "name": "Finished unit",
                "kind": "output",
                "unit": "unit",
                "lead_time_days": None,
                "duration_days": None,
                "eto_reference_id": None,
                "provider_candidates": [],
                "position": {"x": 400, "y": 100},
                "requires": [
                    {"node_id": "process-a", "quantity": 1, "unit": "assembly"},
                    {"node_id": "material-b", "quantity": 2, "unit": "chiplet"},
                    {"node_id": "quality-check", "quantity": 1, "unit": "check"},
                ],
            },
        },
    }


def _published_recipe() -> ProductRecipe:
    return ProductRecipe.model_validate(_recipe()).model_copy(
        update={
            "status": "published",
            "product_version": 1,
            "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
        }
    )


def _request(order_id: str = "ORDER-001") -> PlanOrderRequest:
    return PlanOrderRequest(
        order_id=order_id,
        product_id="parallel-product",
        product_version=1,
        quantity=10,
        requested_at="2026-01-05T00:00:00-08:00",
    )


def _publish(tmp_path: Path) -> tuple[ProductConfigStore, Path]:
    published = tmp_path / "published"
    store = ProductConfigStore(tmp_path / "drafts", published)
    store.save_draft(
        "parallel-product",
        ProductRecipe.model_validate(_recipe()),
    )
    store.publish("parallel-product")
    return store, published


def test_planner_expands_quantities_and_schedules_parallel_branches() -> None:
    plan = build_order_plan(_published_recipe(), "a" * 64, _request())
    nodes = {node.node_id: node for node in plan.node_plans}
    orders = {order.node_id: order for order in plan.provider_orders}

    assert plan.requested_at == datetime(2026, 1, 5, 8, tzinfo=UTC)
    assert plan.expected_delivery_at == datetime(2026, 1, 11, 8, tzinfo=UTC)
    assert plan.critical_path_node_ids == [
        "material-a",
        "process-a",
        "finished-unit",
    ]
    assert nodes["material-a"].demanded_quantity == 5
    assert nodes["material-b"].demanded_quantity == 20
    assert nodes["quality-check"].demanded_quantity == 10
    assert orders["material-a"].provider_id == "P-A-FIRST"
    assert len(plan.provider_orders) == 3
    assert orders["process-a"].depends_on_provider_order_ids == [
        orders["material-a"].provider_order_id
    ]
    assert nodes["material-b"].finish_at == datetime(
        2026, 1, 8, 8, tzinfo=UTC
    )
    assert nodes["process-a"].start_at == datetime(
        2026, 1, 7, 8, tzinfo=UTC
    )


def test_shared_prerequisite_demand_is_summed() -> None:
    payload = _recipe()
    payload["nodes"].pop("quality-check")
    payload["nodes"].pop("material-b")
    payload["nodes"]["process-b"] = {
        "name": "Process B",
        "kind": "process",
        "unit": "subassembly",
        "lead_time_days": None,
        "duration_days": 1,
        "eto_reference_id": None,
        "provider_candidates": ["P-B"],
        "position": {"x": 200, "y": 200},
        "requires": [
            {"node_id": "material-a", "quantity": 3, "unit": "kg"}
        ],
    }
    payload["nodes"]["finished-unit"]["requires"] = [
        {"node_id": "process-a", "quantity": 1, "unit": "assembly"},
        {"node_id": "process-b", "quantity": 1, "unit": "subassembly"},
    ]
    recipe = ProductRecipe.model_validate(payload).model_copy(
        update={"status": "published", "product_version": 1}
    )
    request = _request().model_copy(update={"quantity": 2})

    plan = build_order_plan(recipe, "b" * 64, request)
    nodes = {node.node_id: node for node in plan.node_plans}

    assert nodes["material-a"].demanded_quantity == 7
    assert plan.critical_path_node_ids == [
        "material-a",
        "process-a",
        "finished-unit",
    ]


def test_plan_artifact_is_immutable_and_fingerprinted(tmp_path: Path) -> None:
    product_store, published = _publish(tmp_path)
    plan_store = PlanArtifactStore(tmp_path / "plans")

    result = plan_order(product_store, plan_store, _request())
    original = result.plan_path.read_bytes()

    assert result.plan.product_config_sha256 == hashlib.sha256(
        (published / "parallel-product/v1.json").read_bytes()
    ).hexdigest()
    with pytest.raises(PlanningConflictError, match="already exists"):
        plan_order(product_store, plan_store, _request())
    assert result.plan_path.read_bytes() == original


def test_plan_store_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "plans"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "ORDER-001").symlink_to(outside, target_is_directory=True)
    store = PlanArtifactStore(root)

    with pytest.raises(PlanStorageError, match="escapes"):
        store.save(build_order_plan(_published_recipe(), "c" * 64, _request()))
    assert list(outside.iterdir()) == []


def test_direct_plan_store_refuses_overwrite(tmp_path: Path) -> None:
    store = PlanArtifactStore(tmp_path / "plans")
    plan = build_order_plan(_published_recipe(), "d" * 64, _request())
    store.save(plan)

    with pytest.raises(PlanAlreadyExistsError):
        store.save(plan)


def test_combined_publish_and_plan_web_workflow(tmp_path: Path) -> None:
    published = tmp_path / "published"
    drafts = tmp_path / "drafts"
    plans = tmp_path / "plans"
    client = ASGITestClient(
        create_app(
            tmp_path / "missing.db",
            product_config_directory=published,
            product_draft_directory=drafts,
            plan_directory=plans,
        )
    )
    assert client.put(
        "/api/v1/product-configs/parallel-product/draft",
        json=_recipe(),
        headers=EDITOR_HEADERS,
    ).status_code == 200
    assert client.post(
        "/api/v1/product-configs/parallel-product/publish",
        headers=EDITOR_HEADERS,
    ).status_code == 200

    form = client.get(
        "/plans/new",
        params={"product_id": "parallel-product", "product_version": "1"},
    )
    missing_marker = client.post("/api/v1/plans", json=_request().model_dump(mode="json"))
    created = client.post(
        "/api/v1/plans",
        json=_request().model_dump(mode="json"),
        headers=EDITOR_HEADERS,
    )
    replay = client.post(
        "/api/v1/plans",
        json=_request().model_dump(mode="json"),
        headers=EDITOR_HEADERS,
    )
    detail = client.get("/plans/ORDER-001")
    listing = client.get("/api/v1/plans")

    assert form.status_code == 200
    assert "Generate immutable plan" in form.text
    assert missing_marker.status_code == 403
    assert created.status_code == 201
    assert created.json()["plan"]["expected_delivery_at"] == (
        "2026-01-11T08:00:00Z"
    )
    assert replay.status_code == 409
    assert detail.status_code == 200
    assert "ORDER-001-PO-001" in detail.text
    assert listing.json()["count"] == 1


def test_cli_plans_a_published_product(tmp_path: Path, capsys) -> None:
    _, published = _publish(tmp_path)
    plans = tmp_path / "plans"

    status = main(
        [
            "plan",
            "order",
            "--product-id",
            "parallel-product",
            "--product-version",
            "1",
            "--order-id",
            "CLI-001",
            "--quantity",
            "10",
            "--requested-at",
            "2026-01-05T00:00:00Z",
            "--product-config-dir",
            str(published),
            "--plans-dir",
            str(plans),
        ]
    )

    assert status == 0
    assert "Planning: PASS" in capsys.readouterr().out
    assert (plans / "CLI-001/plan.json").is_file()
