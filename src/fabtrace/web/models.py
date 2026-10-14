from __future__ import annotations

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    database_mode: str
    product_config_mode: str


class OrderSummary(BaseModel):
    scenario_id: str
    purchase_order_id: str
    reconciliation_version: str
    provider_profile_id: str
    source_provider_id: str
    state_as_of_time: str
    order_status: str
    progress_pct: float | None
    quantity_completed: float | None
    quantity_ordered: float | None
    quantity_unit: str | None
    forecast_delivery_date: str | None
    delay_days: float | None
    incident_reason: str | None
    metric_as_of_time: str | None
    initial_forecast_date: str | None
    current_forecast_date: str | None
    forecast_drift_days: int | None
    forecast_revision_count: int | None
    days_to_forecast: int | None
    reported_delay_days: float | None
    has_reported_delay: bool | None
    progress_velocity_pct_per_day: float | None
    latest_update_age_days: float | None
    communication_lag_days: float | None
    applied_message_count: int | None
    stale_message_count: int | None
    conflict_message_count: int | None


class StateSnapshot(BaseModel):
    snapshot_id: str
    decision_id: str
    snapshot_sequence: int
    as_of_received_at: str
    effective_through_sent_at: str
    last_message_id: str
    order_status: str | None
    progress_pct: float | None
    quantity_completed: float | None
    quantity_ordered: float | None
    quantity_unit: str | None
    forecast_delivery_date: str | None
    delay_days: float | None
    incident_reason: str | None
    milestone_date: str | None
    order_status_source_message_id: str | None
    progress_pct_source_message_id: str | None
    forecast_delivery_date_source_message_id: str | None
    delay_days_source_message_id: str | None
    incident_reason_source_message_id: str | None


class MetricSnapshot(BaseModel):
    metric_snapshot_id: str
    source_snapshot_id: str
    as_of_time: str
    computed_at: str
    order_status: str
    progress_pct: float | None
    initial_forecast_date: str | None
    current_forecast_date: str | None
    forecast_drift_days: int | None
    forecast_revision_count: int
    days_to_forecast: int | None
    reported_delay_days: float | None
    has_reported_delay: bool
    progress_velocity_pct_per_day: float | None
    latest_update_age_days: float
    communication_lag_days: float
    applied_message_count: int
    stale_message_count: int
    conflict_message_count: int


class ReconciliationDecision(BaseModel):
    decision_id: str
    message_id: str
    processing_sequence: int
    sent_at: str
    received_at: str
    decision: str
    detail: str | None
    decided_at: str


class OrderListResponse(BaseModel):
    api_version: str
    scenario_id: str | None
    count: int
    orders: list[OrderSummary]


class OrderDetailResponse(BaseModel):
    api_version: str
    order: OrderSummary
    state_history: list[StateSnapshot]
    metric_history: list[MetricSnapshot]
    decisions: list[ReconciliationDecision]


class ProviderGroup(BaseModel):
    provider_profile_id: str
    source_provider_id: str
    orders: list[OrderSummary]


class ProviderListResponse(BaseModel):
    api_version: str
    scenario_id: str | None
    count: int
    providers: list[ProviderGroup]


class CustomerOrderSummary(BaseModel):
    projection_version: str
    order_id: str
    scenario_id: str | None
    product_id: str
    product_version: int
    requested_quantity: float
    output_unit: str
    requested_at: str
    planned_delivery_at: str
    current_delivery_date: str
    delivery_estimate_source: str
    delivery_drift_days: int
    status: str
    provider_order_count: int
    reported_provider_order_count: int
    completed_provider_order_count: int
    terminal_provider_order_count: int
    critical_path_blocked_count: int
    latest_provider_update_at: str | None
    plan_sha256: str


class CustomerProviderOrder(BaseModel):
    provider_order_id: str
    customer_order_id: str
    order_type: str
    node_id: str
    node_name: str
    capability_id: str | None
    provider_id: str
    quantity: float
    unit: str
    release_at: str
    expected_completion_at: str
    critical_path: bool
    terminal: bool
    depends_on_provider_order_ids: list[str]
    execution: OrderSummary | None


class PipelineStageStatus(BaseModel):
    stage_name: str
    attempt_number: int
    status: str
    started_at: str
    completed_at: str | None
    duration_ms: int | None
    error: str | None


class PipelineRunStatus(BaseModel):
    pipeline_run_id: str
    customer_order_id: str
    scenario_id: str
    status: str
    created_at: str
    latest_event_at: str
    error: str | None
    stages: list[PipelineStageStatus]


class CustomerOrderListResponse(BaseModel):
    api_version: str
    scenario_id: str | None
    count: int
    customer_orders: list[CustomerOrderSummary]


class CustomerOrderDetailResponse(BaseModel):
    api_version: str
    customer_order: CustomerOrderSummary
    provider_orders: list[CustomerProviderOrder]
    dependencies: list[dict[str, str]]
    pipeline_runs: list[PipelineRunStatus]
