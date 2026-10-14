from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from fabtrace.planning.models import OrderPlan
from fabtrace.storage import StorageSchemaError, initialize_database


class PlanRegistrationError(RuntimeError):
    """Raised when an immutable plan cannot be registered safely."""


@dataclass(frozen=True)
class PlanRegistrationResult:
    database_path: Path
    plan_path: Path
    order_id: str
    plan_sha256: str
    provider_order_count: int
    dependency_count: int
    registered: bool


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _utc_timestamp(value: datetime, field_name: str) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PlanRegistrationError(f"{field_name} must include a timezone offset")
    return value.astimezone(UTC).isoformat()


def _validate_plan_graph(plan: OrderPlan) -> None:
    node_ids = [node.node_id for node in plan.node_plans]
    if len(node_ids) != len(set(node_ids)):
        raise PlanRegistrationError("plan contains duplicate node IDs")
    provider_ids = [order.provider_order_id for order in plan.provider_orders]
    if len(provider_ids) != len(set(provider_ids)):
        raise PlanRegistrationError("plan contains duplicate provider-order IDs")
    known = set(provider_ids)
    incoming: dict[str, int] = {order_id: 0 for order_id in provider_ids}
    downstream: dict[str, list[str]] = {order_id: [] for order_id in provider_ids}
    for order in plan.provider_orders:
        dependencies = order.depends_on_provider_order_ids
        if len(dependencies) != len(set(dependencies)):
            raise PlanRegistrationError(
                f"provider order {order.provider_order_id!r} repeats a dependency"
            )
        for dependency_id in dependencies:
            if dependency_id not in known:
                raise PlanRegistrationError(
                    f"provider order {order.provider_order_id!r} depends on unknown "
                    f"order {dependency_id!r}"
                )
            if dependency_id == order.provider_order_id:
                raise PlanRegistrationError(
                    f"provider order {order.provider_order_id!r} depends on itself"
                )
            incoming[order.provider_order_id] += 1
            downstream[dependency_id].append(order.provider_order_id)
    ready = sorted(order_id for order_id, count in incoming.items() if count == 0)
    visited = 0
    while ready:
        current = ready.pop(0)
        visited += 1
        for dependent in sorted(downstream[current]):
            incoming[dependent] -= 1
            if incoming[dependent] == 0:
                ready.append(dependent)
                ready.sort()
    if visited != len(provider_ids):
        raise PlanRegistrationError("plan provider-order dependencies contain a cycle")


def register_plan(plan_path: Path, database_path: Path) -> PlanRegistrationResult:
    plan_path = Path(plan_path).resolve()
    database_path = Path(database_path).resolve()
    if not plan_path.is_file():
        raise PlanRegistrationError(f"plan does not exist: {plan_path}")
    try:
        raw_plan = plan_path.read_bytes()
        plan = OrderPlan.model_validate_json(raw_plan)
    except (OSError, ValidationError) as exc:
        raise PlanRegistrationError(f"invalid plan artifact: {exc}") from exc
    _validate_plan_graph(plan)
    plan_sha256 = hashlib.sha256(raw_plan).hexdigest()
    node_critical = {node.node_id: node.critical_path for node in plan.node_plans}
    missing_nodes = sorted(
        order.node_id
        for order in plan.provider_orders
        if order.node_id not in node_critical
    )
    if missing_nodes:
        raise PlanRegistrationError(
            "provider orders reference missing planned nodes: "
            + ", ".join(missing_nodes)
        )
    dependency_count = sum(
        len(order.depends_on_provider_order_ids) for order in plan.provider_orders
    )
    database_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        connection = sqlite3.connect(database_path)
    except sqlite3.Error as exc:
        raise PlanRegistrationError(
            f"cannot open plan registry database {database_path}: {exc}"
        ) from exc
    try:
        initialize_database(connection)
        existing = connection.execute(
            "SELECT plan_sha256 FROM registered_plans WHERE order_id = ?",
            (plan.order_id,),
        ).fetchone()
        if existing is not None:
            if existing[0] != plan_sha256:
                raise PlanRegistrationError(
                    f"order {plan.order_id!r} is already registered with a "
                    "different plan fingerprint"
                )
            return PlanRegistrationResult(
                database_path=database_path,
                plan_path=plan_path,
                order_id=plan.order_id,
                plan_sha256=plan_sha256,
                provider_order_count=len(plan.provider_orders),
                dependency_count=dependency_count,
                registered=False,
            )
        connection.execute(
            """
            INSERT INTO registered_plans (
                order_id, plan_sha256, schema_version, planner_version,
                synthetic, product_id, product_version, requested_quantity,
                output_unit, requested_at, planned_delivery_at, generated_at,
                provider_order_count, source_path, registered_at, raw_plan
            ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan.order_id,
                plan_sha256,
                plan.schema_version,
                plan.planner_version,
                plan.product_id,
                plan.product_version,
                plan.requested_quantity,
                plan.output_unit,
                _utc_timestamp(plan.requested_at, "requested_at"),
                _utc_timestamp(plan.expected_delivery_at, "expected_delivery_at"),
                _utc_timestamp(plan.generated_at, "generated_at"),
                len(plan.provider_orders),
                str(plan_path),
                _utc_now(),
                raw_plan,
            ),
        )
        for order in plan.provider_orders:
            connection.execute(
                """
                INSERT INTO planned_provider_orders (
                    provider_order_id, customer_order_id, order_type, node_id,
                    node_name, capability_id, provider_id, quantity, unit,
                    release_at, expected_completion_at, critical_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order.provider_order_id,
                    plan.order_id,
                    order.order_type,
                    order.node_id,
                    order.node_name,
                    order.capability_id,
                    order.provider_id,
                    order.quantity,
                    order.unit,
                    _utc_timestamp(order.release_at, "release_at"),
                    _utc_timestamp(
                        order.expected_completion_at,
                        "expected_completion_at",
                    ),
                    int(node_critical[order.node_id]),
                ),
            )
        for order in plan.provider_orders:
            for dependency_id in order.depends_on_provider_order_ids:
                connection.execute(
                    """
                    INSERT INTO planned_provider_order_dependencies (
                        provider_order_id, depends_on_provider_order_id
                    ) VALUES (?, ?)
                    """,
                    (order.provider_order_id, dependency_id),
                )
        connection.commit()
    except PlanRegistrationError:
        connection.rollback()
        raise
    except (sqlite3.Error, StorageSchemaError) as exc:
        connection.rollback()
        raise PlanRegistrationError(f"plan registration failed: {exc}") from exc
    finally:
        connection.close()
    return PlanRegistrationResult(
        database_path=database_path,
        plan_path=plan_path,
        order_id=plan.order_id,
        plan_sha256=plan_sha256,
        provider_order_count=len(plan.provider_orders),
        dependency_count=dependency_count,
        registered=True,
    )
