from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SimulationModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
    )


class ProviderCapabilityConfig(SimulationModel):
    source_capability_id: str
    unit: str
    daily_capacity: int = Field(gt=0)
    quality_check_days: int = Field(ge=0)


class ProviderOperationsConfig(SimulationModel):
    acknowledgement_delay_days: int = Field(ge=0)
    queue_delay_days: int = Field(ge=0)
    transit_days: int = Field(ge=0)


class CommunicationConfig(SimulationModel):
    channel: str
    progress_update_interval_days: int = Field(gt=0)
    style: str
    send_milestone_updates: bool


class WeightedDowntime(SimulationModel):
    days: int = Field(gt=0)
    weight: float = Field(gt=0)


class WeightedReason(SimulationModel):
    reason_code: str
    weight: float = Field(gt=0)


class DisruptionModel(SimulationModel):
    model_version: Literal[1]
    daily_failure_probability: float = Field(gt=0, le=1)
    maximum_failures_per_order: Literal[1]
    downtime_distribution: tuple[WeightedDowntime, ...] = Field(min_length=1)
    reason_distribution: tuple[WeightedReason, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_distribution_values(self) -> "DisruptionModel":
        downtime_values = [item.days for item in self.downtime_distribution]
        if len(downtime_values) != len(set(downtime_values)):
            raise ValueError("downtime distribution days must be unique")
        reasons = [item.reason_code for item in self.reason_distribution]
        if len(reasons) != len(set(reasons)):
            raise ValueError("reason distribution codes must be unique")
        return self


class ProviderProfile(SimulationModel):
    schema_version: Literal["1.0"]
    profile_id: str
    profile_version: int = Field(gt=0)
    synthetic: Literal[True]
    source_provider_id: str
    simulation_name: str
    capabilities: tuple[ProviderCapabilityConfig, ...] = Field(min_length=1)
    operations: ProviderOperationsConfig
    communication: CommunicationConfig
    disruption_model: DisruptionModel


class ProviderProfileReference(SimulationModel):
    profile: str


class PurchaseOrderConfig(SimulationModel):
    purchase_order_id: str
    provider_profile_id: str
    capability_id: str
    quantity: int = Field(gt=0)
    unit: str


class EquipmentFailureConfig(SimulationModel):
    incident_id: str
    incident_type: Literal["equipment_failure"]
    purchase_order_id: str
    trigger_after_production_days: int = Field(gt=0)
    downtime_days: int = Field(gt=0)
    reason_code: str


class IncidentGenerationConfig(SimulationModel):
    mode: Literal["seeded_stochastic"]
    model: Literal["provider_profile"]
    random_stream_version: Literal[1]


class PlanExecutionScenario(SimulationModel):
    schema_version: Literal["1.0"]
    scenario_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    scenario_version: int = Field(gt=0)
    seed: int = Field(ge=0)
    synthetic: Literal[True]
    providers: tuple[ProviderProfileReference, ...] = Field(min_length=1)
    incident_generation: IncidentGenerationConfig | None = None


class ScenarioConfig(SimulationModel):
    schema_version: Literal["1.0"]
    scenario_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    scenario_version: int = Field(gt=0)
    seed: int = Field(ge=0)
    synthetic: Literal[True]
    start_at: datetime
    providers: tuple[ProviderProfileReference, ...] = Field(min_length=1)
    purchase_orders: tuple[PurchaseOrderConfig, ...] = Field(min_length=1)
    incidents: tuple[EquipmentFailureConfig, ...] = ()
    incident_generation: IncidentGenerationConfig | None = None

    @field_validator("start_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("start_at must include a timezone")
        return value


class SourceProviderSnapshot(SimulationModel):
    provider_id: str
    provider_name: str
    provider_type: str
    country_code: str | None
    country_name: str | None
    aliases: tuple[str, ...]


class SourceCapabilitySnapshot(SimulationModel):
    input_id: str
    input_name: str
    input_type: str
    description: str
    source_year: int | None
    market_size_info: str | None
    share_provided: float | None
    provision_year: int
    source_citation: str | None


class EffectiveProvider(SimulationModel):
    profile_id: str
    profile_version: int
    simulation_name: str
    synthetic: Literal[True]
    source: SourceProviderSnapshot
    capabilities: tuple[SourceCapabilitySnapshot, ...]
    synthetic_capabilities: tuple[ProviderCapabilityConfig, ...]
    operations: ProviderOperationsConfig
    communication: CommunicationConfig
    disruption_model: DisruptionModel


class EffectiveScenario(SimulationModel):
    scenario_id: str
    scenario_version: int
    seed: int
    synthetic: Literal[True]
    start_at: datetime


class PlanExecutionOrder(SimulationModel):
    provider_order_id: str
    order_type: Literal["purchase_order", "work_order"]
    node_id: str
    provider_profile_id: str
    source_provider_id: str
    capability_id: str
    quantity: float = Field(gt=0, allow_inf_nan=False)
    unit: str
    planned_release_at: datetime
    depends_on_provider_order_ids: tuple[str, ...] = ()

    @field_validator("planned_release_at")
    @classmethod
    def require_release_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("planned_release_at must include a timezone")
        return value


class EffectivePlanSource(SimulationModel):
    order_id: str
    product_id: str
    product_version: int = Field(gt=0)
    planner_version: str
    source_path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class EffectivePlanConfig(SimulationModel):
    schema_version: Literal["1.0"]
    generator_version: str
    scenario: EffectiveScenario
    plan: EffectivePlanSource
    providers: tuple[EffectiveProvider, ...]
    provider_orders: tuple[PlanExecutionOrder, ...]
    incident_generation: IncidentGenerationConfig | None = None


class EffectiveConfig(SimulationModel):
    schema_version: Literal["1.0"]
    generator_version: str
    scenario: EffectiveScenario
    providers: tuple[EffectiveProvider, ...]
    purchase_orders: tuple[PurchaseOrderConfig, ...]
    incidents: tuple[EquipmentFailureConfig, ...] = ()
    incident_generation: IncidentGenerationConfig | None = None


class OrderState(StrEnum):
    CREATED = "CREATED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    QUEUED = "QUEUED"
    IN_PRODUCTION = "IN_PRODUCTION"
    BLOCKED = "BLOCKED"
    QUALITY_CHECK = "QUALITY_CHECK"
    READY_TO_SHIP = "READY_TO_SHIP"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"


class EventType(StrEnum):
    ORDER_CREATED = "order_created"
    ORDER_ACKNOWLEDGED = "order_acknowledged"
    ORDER_QUEUED = "order_queued"
    PRODUCTION_STARTED = "production_started"
    PROGRESS_UPDATED = "progress_updated"
    EQUIPMENT_FAILURE = "equipment_failure"
    PRODUCTION_RESUMED = "production_resumed"
    QUALITY_CHECK_STARTED = "quality_check_started"
    QUALITY_CHECK_PASSED = "quality_check_passed"
    SHIPMENT_DISPATCHED = "shipment_dispatched"
    ORDER_DELIVERED = "order_delivered"


class GroundTruthEvent(SimulationModel):
    schema_version: Literal["1.0"] = "1.0"
    generator_version: str
    synthetic: Literal[True] = True
    scenario_id: str
    scenario_version: int
    event_id: str
    event_type: EventType
    state: OrderState
    provider_profile_id: str
    source_provider_id: str
    capability_id: str
    purchase_order_id: str
    event_time: datetime
    sim_time_days: float = Field(ge=0)
    quantity_ordered: int | float = Field(gt=0)
    quantity_completed: int | float = Field(ge=0)
    unit: str
    progress_pct: float = Field(ge=0, le=100)
    forecast_delivery_at: datetime
    previous_forecast_delivery_at: datetime | None = None
    incident_id: str | None = None
    incident_type: str | None = None
    reason_code: str | None = None
    delay_days: int | None = Field(default=None, gt=0)
    incident_source: Literal["configured", "stochastic"] | None = None
    random_stream_version: int | None = Field(default=None, gt=0)
    random_draw: float | None = Field(default=None, ge=0, lt=1)
    trigger_probability: float | None = Field(default=None, gt=0, le=1)
    sampled_downtime_days: int | None = Field(default=None, gt=0)


class ProviderMessage(SimulationModel):
    schema_version: Literal["1.0"] = "1.0"
    generator_version: str
    synthetic: Literal[True] = True
    scenario_id: str
    message_id: str
    source_event_id: str
    provider_profile_id: str
    source_provider_id: str
    purchase_order_id: str
    channel: str
    sent_at: datetime
    received_at: datetime
    subject: str
    body: str


class RunManifest(SimulationModel):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    status: Literal["started", "completed", "failed"]
    created_at: datetime
    completed_at: datetime | None = None
    generator_version: str
    python_version: str
    command: tuple[str, ...]
    scenario_path: str
    plan_path: str | None = None
    plan_sha256: str | None = None
    plan_order_id: str | None = None
    data_directory: str
    source_files: dict[str, str]
    effective_config_sha256: str
    event_count: int = Field(ge=0)
    message_count: int = Field(ge=0)
    error: str | None = None
