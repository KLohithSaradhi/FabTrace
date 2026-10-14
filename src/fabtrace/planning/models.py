from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from fabtrace.products.models import SAFE_ID_PATTERN

ORDER_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"


class PlanOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(pattern=ORDER_ID_PATTERN)
    product_id: str = Field(pattern=SAFE_ID_PATTERN)
    product_version: int = Field(ge=1)
    quantity: float = Field(gt=0, allow_inf_nan=False)
    requested_at: datetime

    @field_validator("requested_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("requested_at must include a timezone offset")
        return value


class PlannedNode(BaseModel):
    node_id: str
    name: str
    kind: Literal["material", "process", "capability", "output"]
    action_type: Literal[
        "purchase_order",
        "work_order",
        "capability_check",
        "customer_output",
    ]
    demanded_quantity: float
    unit: str
    selected_provider_id: str | None
    start_at: datetime
    finish_at: datetime
    elapsed_days: float
    critical_path: bool


class ProviderOrder(BaseModel):
    provider_order_id: str
    order_type: Literal["purchase_order", "work_order"]
    node_id: str
    node_name: str
    capability_id: str | None = None
    provider_id: str
    quantity: float
    unit: str
    release_at: datetime
    expected_completion_at: datetime
    depends_on_provider_order_ids: list[str]


class OrderPlan(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    planner_version: Literal["planner-v1"] = "planner-v1"
    synthetic: Literal[True] = True
    generated_at: datetime
    order_id: str
    product_id: str
    product_version: int
    product_config_sha256: str
    requested_quantity: float
    output_unit: str
    requested_at: datetime
    expected_delivery_at: datetime
    critical_path_node_ids: list[str]
    node_plans: list[PlannedNode]
    provider_orders: list[ProviderOrder]
    assumptions: list[str]


class PlanResponse(BaseModel):
    api_version: str
    plan: OrderPlan


class PlanSummary(BaseModel):
    order_id: str
    product_id: str
    product_version: int
    requested_quantity: float
    output_unit: str
    requested_at: datetime
    expected_delivery_at: datetime
    provider_order_count: int


class PlanListResponse(BaseModel):
    api_version: str
    count: int
    plans: list[PlanSummary]
