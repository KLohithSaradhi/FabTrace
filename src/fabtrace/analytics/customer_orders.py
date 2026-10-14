from __future__ import annotations

from datetime import date, datetime
from typing import Any

CUSTOMER_PROJECTION_VERSION = "customer-order-v1"


def _date_part(value: str) -> date:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date()


def build_customer_order_projection(
    plan: dict[str, Any],
    provider_orders: list[dict[str, Any]],
    dependencies: list[dict[str, str]],
    states: list[dict[str, Any]],
    scenario_id: str | None,
) -> dict[str, Any]:
    state_by_order = {
        str(state["purchase_order_id"]): state for state in states
    }
    downstream_ids = {
        dependency["depends_on_provider_order_id"] for dependency in dependencies
    }
    terminal_orders = [
        order
        for order in provider_orders
        if order["provider_order_id"] not in downstream_ids
    ]
    terminal_statuses = [
        state_by_order[order["provider_order_id"]]["order_status"]
        for order in terminal_orders
        if order["provider_order_id"] in state_by_order
    ]
    critical_blocked = sum(
        1
        for order in provider_orders
        if order["critical_path"]
        and state_by_order.get(order["provider_order_id"], {}).get("order_status")
        == "BLOCKED"
    )
    if terminal_orders and len(terminal_statuses) == len(terminal_orders) and all(
        status == "DELIVERED" for status in terminal_statuses
    ):
        status = "DELIVERED"
    elif critical_blocked:
        status = "BLOCKED"
    elif states:
        status = "IN_PROGRESS"
    else:
        status = "PLANNED"

    forecasts = [
        state.get("current_forecast_date") or state.get("forecast_delivery_date")
        for order in terminal_orders
        if (state := state_by_order.get(order["provider_order_id"])) is not None
    ]
    reported_forecasts = sorted(value for value in forecasts if value is not None)
    planned_delivery_at = str(plan["planned_delivery_at"])
    planned_delivery_date = _date_part(planned_delivery_at).isoformat()
    current_delivery_date = (
        reported_forecasts[-1] if reported_forecasts else planned_delivery_date
    )
    drift = (
        date.fromisoformat(current_delivery_date)
        - date.fromisoformat(planned_delivery_date)
    ).days
    latest_updates = [state["state_as_of_time"] for state in states]
    return {
        "projection_version": CUSTOMER_PROJECTION_VERSION,
        "order_id": plan["order_id"],
        "scenario_id": scenario_id,
        "product_id": plan["product_id"],
        "product_version": plan["product_version"],
        "requested_quantity": plan["requested_quantity"],
        "output_unit": plan["output_unit"],
        "requested_at": plan["requested_at"],
        "planned_delivery_at": planned_delivery_at,
        "current_delivery_date": current_delivery_date,
        "delivery_estimate_source": (
            "provider_reported" if reported_forecasts else "planned_fallback"
        ),
        "delivery_drift_days": drift,
        "status": status,
        "provider_order_count": len(provider_orders),
        "reported_provider_order_count": len(states),
        "completed_provider_order_count": sum(
            state.get("order_status") == "DELIVERED" for state in states
        ),
        "terminal_provider_order_count": len(terminal_orders),
        "critical_path_blocked_count": critical_blocked,
        "latest_provider_update_at": max(latest_updates) if latest_updates else None,
        "plan_sha256": plan["plan_sha256"],
    }
