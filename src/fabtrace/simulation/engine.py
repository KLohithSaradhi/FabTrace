from __future__ import annotations

import hashlib
import math
import random
from datetime import datetime, timedelta

import simpy

from fabtrace import __version__
from fabtrace.simulation.models import (
    EffectiveConfig,
    EffectiveProvider,
    EquipmentFailureConfig,
    EventType,
    GroundTruthEvent,
    OrderState,
    ProviderMessage,
    PurchaseOrderConfig,
)


class InvalidStateTransition(RuntimeError):
    """Raised when provider behavior violates the approved order lifecycle."""


def derive_random_seed(
    scenario_seed: int,
    scenario_id: str,
    provider_profile_id: str,
    purchase_order_id: str,
    stream_version: int,
) -> int:
    """Derive an order-local seed without relying on process iteration order."""

    material = (
        f"{scenario_seed}|{scenario_id}|{provider_profile_id}|"
        f"{purchase_order_id}|equipment-failure-v{stream_version}"
    )
    return int.from_bytes(hashlib.sha256(material.encode("utf-8")).digest()[:8], "big")


ALLOWED_TRANSITIONS = {
    OrderState.CREATED: {OrderState.ACKNOWLEDGED},
    OrderState.ACKNOWLEDGED: {OrderState.QUEUED},
    OrderState.QUEUED: {OrderState.IN_PRODUCTION},
    OrderState.IN_PRODUCTION: {OrderState.BLOCKED, OrderState.QUALITY_CHECK},
    OrderState.BLOCKED: {OrderState.IN_PRODUCTION},
    OrderState.QUALITY_CHECK: {OrderState.READY_TO_SHIP},
    OrderState.READY_TO_SHIP: {OrderState.SHIPPED},
    OrderState.SHIPPED: {OrderState.DELIVERED},
    OrderState.DELIVERED: set(),
}


class OrderStateMachine:
    def __init__(self) -> None:
        self.state = OrderState.CREATED

    def transition(self, target: OrderState) -> None:
        if target not in ALLOWED_TRANSITIONS[self.state]:
            raise InvalidStateTransition(f"cannot transition {self.state} to {target}")
        self.state = target


class ProviderSimulation:
    def __init__(self, config: EffectiveConfig) -> None:
        if len(config.providers) != 1 or len(config.purchase_orders) != 1:
            raise ValueError("the first simulator slice requires one provider and one order")
        self.config = config
        self.provider = config.providers[0]
        self.order = config.purchase_orders[0]
        self.capability = self._capability(self.provider, self.order)
        self.incident = self._incident(config, self.order)
        self.incident_generation = config.incident_generation
        self.random_stream_version = (
            self.incident_generation.random_stream_version
            if self.incident_generation is not None
            else None
        )
        self.random = (
            random.Random(
                derive_random_seed(
                    config.scenario.seed,
                    config.scenario.scenario_id,
                    self.provider.profile_id,
                    self.order.purchase_order_id,
                    self.random_stream_version,
                )
            )
            if self.random_stream_version is not None
            else None
        )
        self.environment = simpy.Environment()
        self.machine = OrderStateMachine()
        self.events: list[GroundTruthEvent] = []
        self.messages: list[ProviderMessage] = []
        self.completed_quantity = 0
        self.production_days_elapsed = 0
        self.incident_triggered = False
        self.failure_count = 0
        self._event_number = 0

        production_days = math.ceil(
            self.order.quantity / self.capability.daily_capacity
        )
        total_days = (
            self.provider.operations.acknowledgement_delay_days
            + self.provider.operations.queue_delay_days
            + production_days
            + self.capability.quality_check_days
            + self.provider.operations.transit_days
        )
        self.forecast_delivery_at = config.scenario.start_at + timedelta(
            days=total_days
        )

    @staticmethod
    def _capability(
        provider: EffectiveProvider,
        order: PurchaseOrderConfig,
    ):
        capability = next(
            (
                item
                for item in provider.synthetic_capabilities
                if item.source_capability_id == order.capability_id
            ),
            None,
        )
        if capability is None:
            raise ValueError(f"capability {order.capability_id} is not configured")
        return capability

    @staticmethod
    def _incident(
        config: EffectiveConfig,
        order: PurchaseOrderConfig,
    ) -> EquipmentFailureConfig | None:
        return next(
            (
                incident
                for incident in config.incidents
                if incident.purchase_order_id == order.purchase_order_id
            ),
            None,
        )

    def run(self) -> tuple[tuple[GroundTruthEvent, ...], tuple[ProviderMessage, ...]]:
        self.environment.process(self._execute_order())
        self.environment.run()
        return tuple(self.events), tuple(self.messages)

    def _execute_order(self):
        self._emit(EventType.ORDER_CREATED, communicate=False)

        yield self.environment.timeout(
            self.provider.operations.acknowledgement_delay_days
        )
        self._transition(OrderState.ACKNOWLEDGED, EventType.ORDER_ACKNOWLEDGED)
        self._transition(OrderState.QUEUED, EventType.ORDER_QUEUED)

        yield self.environment.timeout(self.provider.operations.queue_delay_days)
        self._transition(OrderState.IN_PRODUCTION, EventType.PRODUCTION_STARTED)

        interval = self.provider.communication.progress_update_interval_days
        while self.completed_quantity < self.order.quantity:
            remaining = self.order.quantity - self.completed_quantity
            remaining_days = math.ceil(remaining / self.capability.daily_capacity)
            update_after_days = min(interval, remaining_days)
            if self.incident is not None and not self.incident_triggered:
                until_incident = (
                    self.incident.trigger_after_production_days
                    - self.production_days_elapsed
                )
                update_after_days = min(update_after_days, until_incident)
            yield self.environment.timeout(update_after_days)
            self.production_days_elapsed += update_after_days
            self.completed_quantity = min(
                self.order.quantity,
                self.completed_quantity
                + self.capability.daily_capacity * update_after_days,
            )
            self._emit(EventType.PROGRESS_UPDATED)

            if (
                self.incident is not None
                and not self.incident_triggered
                and self.production_days_elapsed
                == self.incident.trigger_after_production_days
            ):
                self.incident_triggered = True
                yield from self._pause_for_incident(
                    self.incident,
                    incident_source="configured",
                )
            elif self._can_generate_failure():
                draw = self.random.random()
                probability = self.provider.disruption_model.daily_failure_probability
                if draw < probability:
                    incident = self._sample_incident()
                    self.failure_count += 1
                    yield from self._pause_for_incident(
                        incident,
                        incident_source="stochastic",
                        random_draw=draw,
                        trigger_probability=probability,
                    )

        self._transition(OrderState.QUALITY_CHECK, EventType.QUALITY_CHECK_STARTED)
        yield self.environment.timeout(self.capability.quality_check_days)
        self._transition(OrderState.READY_TO_SHIP, EventType.QUALITY_CHECK_PASSED)
        self._transition(OrderState.SHIPPED, EventType.SHIPMENT_DISPATCHED)

        yield self.environment.timeout(self.provider.operations.transit_days)
        self._transition(OrderState.DELIVERED, EventType.ORDER_DELIVERED)

    def _can_generate_failure(self) -> bool:
        return (
            self.random is not None
            and self.failure_count
            < self.provider.disruption_model.maximum_failures_per_order
            and self.completed_quantity < self.order.quantity
        )

    def _sample_incident(self) -> EquipmentFailureConfig:
        if self.random is None:
            raise AssertionError("stochastic incident requested without a random stream")
        disruption = self.provider.disruption_model
        downtime = self.random.choices(
            [item.days for item in disruption.downtime_distribution],
            weights=[item.weight for item in disruption.downtime_distribution],
            k=1,
        )[0]
        reason = self.random.choices(
            [item.reason_code for item in disruption.reason_distribution],
            weights=[item.weight for item in disruption.reason_distribution],
            k=1,
        )[0]
        return EquipmentFailureConfig(
            incident_id=(
                f"STOCH-{self.order.purchase_order_id}-{self.failure_count + 1:04d}"
            ),
            incident_type="equipment_failure",
            purchase_order_id=self.order.purchase_order_id,
            trigger_after_production_days=self.production_days_elapsed,
            downtime_days=downtime,
            reason_code=reason,
        )

    def _pause_for_incident(
        self,
        incident: EquipmentFailureConfig,
        incident_source: str,
        random_draw: float | None = None,
        trigger_probability: float | None = None,
    ):
        previous_forecast = self.forecast_delivery_at
        self.forecast_delivery_at += timedelta(days=incident.downtime_days)
        self.machine.transition(OrderState.BLOCKED)
        self._emit(
            EventType.EQUIPMENT_FAILURE,
            incident=incident,
            incident_source=incident_source,
            previous_forecast=previous_forecast,
            random_draw=random_draw,
            trigger_probability=trigger_probability,
        )
        yield self.environment.timeout(incident.downtime_days)
        self.machine.transition(OrderState.IN_PRODUCTION)
        self._emit(
            EventType.PRODUCTION_RESUMED,
            incident=incident,
            incident_source=incident_source,
            random_draw=random_draw,
            trigger_probability=trigger_probability,
        )

    def _transition(self, state: OrderState, event_type: EventType) -> None:
        self.machine.transition(state)
        self._emit(event_type)

    def _emit(
        self,
        event_type: EventType,
        communicate: bool = True,
        incident: EquipmentFailureConfig | None = None,
        incident_source: str | None = None,
        previous_forecast: datetime | None = None,
        random_draw: float | None = None,
        trigger_probability: float | None = None,
    ) -> None:
        self._event_number += 1
        event_time = self.config.scenario.start_at + timedelta(
            days=float(self.environment.now)
        )
        progress = 100 * self.completed_quantity / self.order.quantity
        event = GroundTruthEvent(
            generator_version=__version__,
            scenario_id=self.config.scenario.scenario_id,
            scenario_version=self.config.scenario.scenario_version,
            event_id=(
                f"{self.config.scenario.scenario_id}:"
                f"{self.order.purchase_order_id}:{self._event_number:04d}"
            ),
            event_type=event_type,
            state=self.machine.state,
            provider_profile_id=self.provider.profile_id,
            source_provider_id=self.provider.source.provider_id,
            capability_id=self.order.capability_id,
            purchase_order_id=self.order.purchase_order_id,
            event_time=event_time,
            sim_time_days=float(self.environment.now),
            quantity_ordered=self.order.quantity,
            quantity_completed=self.completed_quantity,
            unit=self.order.unit,
            progress_pct=progress,
            forecast_delivery_at=self.forecast_delivery_at,
            previous_forecast_delivery_at=previous_forecast,
            incident_id=incident.incident_id if incident else None,
            incident_type=incident.incident_type if incident else None,
            reason_code=incident.reason_code if incident else None,
            delay_days=incident.downtime_days if incident else None,
            incident_source=incident_source,
            random_stream_version=(
                self.random_stream_version if incident_source == "stochastic" else None
            ),
            random_draw=random_draw,
            trigger_probability=trigger_probability,
            sampled_downtime_days=(
                incident.downtime_days if incident_source == "stochastic" else None
            ),
        )
        self.events.append(event)
        if communicate:
            self.messages.append(self._render_message(event))

    def _render_message(self, event: GroundTruthEvent) -> ProviderMessage:
        date = event.event_time.date().isoformat()
        delivery = event.forecast_delivery_at.date().isoformat()
        order_id = event.purchase_order_id
        reason = (event.reason_code or "equipment").replace("_", " ")
        content = {
            EventType.ORDER_ACKNOWLEDGED: (
                "Order acknowledged",
                f"We have acknowledged {order_id} for {event.quantity_ordered} "
                f"{event.unit}s. Expected delivery remains {delivery}.",
            ),
            EventType.ORDER_QUEUED: (
                "Order queued",
                f"{order_id} entered our production queue on {date}.",
            ),
            EventType.PRODUCTION_STARTED: (
                "Production started",
                f"Production for {order_id} started on {date}.",
            ),
            EventType.PROGRESS_UPDATED: (
                "Production progress",
                f"{order_id} is {event.progress_pct:.0f}% complete "
                f"({event.quantity_completed} of {event.quantity_ordered} "
                f"{event.unit}s).",
            ),
            EventType.EQUIPMENT_FAILURE: (
                "Production delay",
                f"An {reason} issue has paused "
                f"production for {order_id}. "
                f"Delivery is now expected on {delivery}, "
                f"{event.delay_days} days later than previously forecast.",
            ),
            EventType.PRODUCTION_RESUMED: (
                "Production resumed",
                f"Production for {order_id} resumed on {date}. "
                f"Expected delivery remains {delivery}.",
            ),
            EventType.QUALITY_CHECK_STARTED: (
                "Quality check started",
                f"Production for {order_id} is complete and quality checks have begun.",
            ),
            EventType.QUALITY_CHECK_PASSED: (
                "Quality check passed",
                f"{order_id} passed quality checks and is ready to ship.",
            ),
            EventType.SHIPMENT_DISPATCHED: (
                "Shipment dispatched",
                f"{order_id} shipped on {date}; delivery is expected on {delivery}.",
            ),
            EventType.ORDER_DELIVERED: (
                "Order delivered",
                f"Delivery of {order_id} was completed on {date}.",
            ),
        }[event.event_type]
        message_number = len(self.messages) + 1
        return ProviderMessage(
            generator_version=__version__,
            scenario_id=event.scenario_id,
            message_id=(
                f"{event.scenario_id}:{event.purchase_order_id}:MSG-{message_number:04d}"
            ),
            source_event_id=event.event_id,
            provider_profile_id=event.provider_profile_id,
            source_provider_id=event.source_provider_id,
            purchase_order_id=event.purchase_order_id,
            channel=self.provider.communication.channel,
            sent_at=event.event_time,
            received_at=event.event_time,
            subject=content[0],
            body=content[1],
        )
