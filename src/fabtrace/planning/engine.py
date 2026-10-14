from __future__ import annotations

from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fabtrace.planning.models import (
    OrderPlan,
    PlannedNode,
    PlanOrderRequest,
    ProviderOrder,
)
from fabtrace.products.models import ProductRecipe
from fabtrace.products.validation import validate_for_publish

PLANNER_VERSION = "planner-v1"


class PlanningModelError(RuntimeError):
    """Raised when a published recipe cannot produce a deterministic plan."""


def _topological_order(recipe: ProductRecipe) -> list[str]:
    adjacency: dict[str, set[str]] = {
        node_id: set() for node_id in recipe.nodes
    }
    indegree: dict[str, int] = {node_id: 0 for node_id in recipe.nodes}
    for dependent_id, node in recipe.nodes.items():
        for requirement in node.requires:
            adjacency[requirement.node_id].add(dependent_id)
            indegree[dependent_id] += 1
    queue = deque(
        sorted(node_id for node_id, degree in indegree.items() if degree == 0)
    )
    ordered: list[str] = []
    while queue:
        current = queue.popleft()
        ordered.append(current)
        for dependent in sorted(adjacency[current]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                queue.append(dependent)
    if len(ordered) != len(recipe.nodes):
        raise PlanningModelError("published product graph contains a cycle")
    return ordered


def _action_type(kind: str) -> str:
    return {
        "material": "purchase_order",
        "process": "work_order",
        "capability": "capability_check",
        "output": "customer_output",
    }[kind]


def _elapsed_days(recipe: ProductRecipe, node_id: str) -> float:
    node = recipe.nodes[node_id]
    if node.kind == "material":
        return float(node.lead_time_days or 0)
    if node.kind == "process":
        return float(node.duration_days or 0)
    return 0.0


def _critical_path(
    recipe: ProductRecipe,
    finish_times: dict[str, datetime],
) -> list[str]:
    if recipe.output_node_id is None:
        raise PlanningModelError("published product has no output node")
    reverse_path = [recipe.output_node_id]
    current = recipe.output_node_id
    while recipe.nodes[current].requires:
        candidates = [
            requirement.node_id
            for requirement in recipe.nodes[current].requires
        ]
        latest_finish = max(finish_times[node_id] for node_id in candidates)
        current = min(
            node_id
            for node_id in candidates
            if finish_times[node_id] == latest_finish
        )
        reverse_path.append(current)
    return list(reversed(reverse_path))


def build_order_plan(
    recipe: ProductRecipe,
    product_config_sha256: str,
    request: PlanOrderRequest,
    *,
    generated_at: datetime | None = None,
) -> OrderPlan:
    if recipe.status != "published" or recipe.product_version is None:
        raise PlanningModelError("planner requires a published product version")
    if recipe.product_id != request.product_id:
        raise PlanningModelError("request product ID does not match recipe")
    if recipe.product_version != request.product_version:
        raise PlanningModelError("request product version does not match recipe")
    issues = validate_for_publish(recipe)
    if issues:
        detail = "; ".join(issue.message for issue in issues)
        raise PlanningModelError(f"published product is not plannable: {detail}")
    if recipe.output_node_id is None:
        raise PlanningModelError("published product has no output node")

    ordered = _topological_order(recipe)
    requested_at = request.requested_at.astimezone(UTC)
    demand: dict[str, Decimal] = defaultdict(Decimal)
    demand[recipe.output_node_id] = Decimal(str(request.quantity))
    for dependent_id in reversed(ordered):
        dependent_demand = demand[dependent_id]
        for requirement in recipe.nodes[dependent_id].requires:
            demand[requirement.node_id] += (
                dependent_demand * Decimal(str(requirement.quantity))
            )

    start_times: dict[str, datetime] = {}
    finish_times: dict[str, datetime] = {}
    for node_id in ordered:
        prerequisites = [
            requirement.node_id
            for requirement in recipe.nodes[node_id].requires
        ]
        start = max(
            (finish_times[required_id] for required_id in prerequisites),
            default=requested_at,
        )
        start_times[node_id] = start
        finish_times[node_id] = start + timedelta(
            days=_elapsed_days(recipe, node_id)
        )

    critical_path = _critical_path(recipe, finish_times)
    critical_nodes = set(critical_path)
    actionable_ids = [
        node_id
        for node_id in ordered
        if recipe.nodes[node_id].kind in {"material", "process"}
    ]
    provider_order_ids: dict[str, str] = {}
    for sequence, node_id in enumerate(actionable_ids, start=1):
        prefix = "PO" if recipe.nodes[node_id].kind == "material" else "WO"
        provider_order_ids[node_id] = (
            f"{request.order_id}-{prefix}-{sequence:03d}"
        )

    upstream_orders: dict[str, set[str]] = {}
    provider_orders: list[ProviderOrder] = []
    for node_id in ordered:
        dependencies: set[str] = set()
        for requirement in recipe.nodes[node_id].requires:
            required_id = requirement.node_id
            if required_id in provider_order_ids:
                dependencies.add(provider_order_ids[required_id])
            else:
                dependencies.update(upstream_orders[required_id])
        upstream_orders[node_id] = dependencies
        if node_id not in provider_order_ids:
            continue
        node = recipe.nodes[node_id]
        provider_orders.append(
            ProviderOrder(
                provider_order_id=provider_order_ids[node_id],
                order_type=(
                    "purchase_order"
                    if node.kind == "material"
                    else "work_order"
                ),
                node_id=node_id,
                node_name=node.name,
                capability_id=node.eto_reference_id,
                provider_id=node.provider_candidates[0],
                quantity=float(demand[node_id]),
                unit=node.unit or "",
                release_at=start_times[node_id],
                expected_completion_at=finish_times[node_id],
                depends_on_provider_order_ids=sorted(dependencies),
            )
        )

    node_plans = [
        PlannedNode(
            node_id=node_id,
            name=recipe.nodes[node_id].name,
            kind=recipe.nodes[node_id].kind,
            action_type=_action_type(recipe.nodes[node_id].kind),
            demanded_quantity=float(demand[node_id]),
            unit=recipe.nodes[node_id].unit or "",
            selected_provider_id=(
                recipe.nodes[node_id].provider_candidates[0]
                if recipe.nodes[node_id].kind in {"material", "process"}
                else None
            ),
            start_at=start_times[node_id],
            finish_at=finish_times[node_id],
            elapsed_days=_elapsed_days(recipe, node_id),
            critical_path=node_id in critical_nodes,
        )
        for node_id in ordered
    ]
    return OrderPlan(
        generated_at=(generated_at or datetime.now(UTC)).astimezone(UTC),
        order_id=request.order_id,
        product_id=request.product_id,
        product_version=request.product_version,
        product_config_sha256=product_config_sha256,
        requested_quantity=request.quantity,
        output_unit=recipe.nodes[recipe.output_node_id].unit or "",
        requested_at=requested_at,
        expected_delivery_at=finish_times[recipe.output_node_id],
        critical_path_node_ids=critical_path,
        node_plans=node_plans,
        provider_orders=provider_orders,
        assumptions=[
            "Edge quantities are prerequisite units per dependent unit.",
            "Shared prerequisite demand is summed without yield or batch rounding.",
            "The first configured provider candidate is selected.",
            "Material lead times and process durations are synthetic and quantity-independent.",
            "Independent dependency branches run in parallel.",
            "Capacity, queues, inventory, transport, calendars, yield, and rework are excluded.",
        ],
    )
