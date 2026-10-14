from __future__ import annotations

import math
import random
from datetime import timedelta

import simpy

from fabtrace import __version__
from fabtrace.simulation.engine import OrderStateMachine, derive_random_seed
from fabtrace.simulation.models import (
    EffectivePlanConfig,
    EffectiveProvider,
    EquipmentFailureConfig,
    EventType,
    GroundTruthEvent,
    OrderState,
    PlanExecutionOrder,
    ProviderCapabilityConfig,
    ProviderMessage,
)


class PlanProviderSimulation:
    """Execute provider orders concurrently on one deterministic virtual clock."""

    def __init__(self, config: EffectivePlanConfig) -> None:
        self.config = config
        self.environment = simpy.Environment()
        self.events: list[GroundTruthEvent] = []
        self.messages: list[ProviderMessage] = []
        self.providers = {provider.profile_id: provider for provider in config.providers}
        self.completions = {
            order.provider_order_id: self.environment.event()
            for order in config.provider_orders
        }

    def run(self) -> tuple[tuple[GroundTruthEvent, ...], tuple[ProviderMessage, ...]]:
        for order in sorted(
            self.config.provider_orders,
            key=lambda item: item.provider_order_id,
        ):
            agent = _PlanOrderAgent(self, order)
            self.environment.process(agent.execute())
        self.environment.run()
        # SimPy executes callbacks in deterministic scheduling order. Preserve
        # that order so a dependency's DELIVERED event remains before a
        # dependent actor's same-instant ORDER_CREATED event.
        return tuple(self.events), tuple(self.messages)


class _PlanOrderAgent:
    def __init__(
        self,
        simulation: PlanProviderSimulation,
        order: PlanExecutionOrder,
    ) -> None:
        self.simulation = simulation
        self.config = simulation.config
        self.environment = simulation.environment
        self.order = order
        self.provider = simulation.providers[order.provider_profile_id]
        self.capability = self._capability(self.provider, order.capability_id)
        self.machine = OrderStateMachine()
        self.completed_quantity = 0.0
        self.production_days_elapsed = 0
        self.failure_count = 0
        self.forecast_delivery_at = self.config.scenario.start_at
        self._event_number = 0
        self._message_number = 0
        generation = self.config.incident_generation
        self.random_stream_version = (
            generation.random_stream_version if generation is not None else None
        )
        self.random = (
            random.Random(
                derive_random_seed(
                    self.config.scenario.seed,
                    self.config.scenario.scenario_id,
                    self.provider.profile_id,
                    self.order.provider_order_id,
                    self.random_stream_version,
                )
            )
            if self.random_stream_version is not None
            else None
        )

    @staticmethod
    def _capability(
        provider: EffectiveProvider,
        capability_id: str,
    ) -> ProviderCapabilityConfig:
        capability = next(
            (
                item
                for item in provider.synthetic_capabilities
                if item.source_capability_id == capability_id
            ),
            None,
        )
        if capability is None:
            raise ValueError(f"capability {capability_id} is not configured")
        return capability

    def execute(self):
        release_days = max(
            0.0,
            (
                self.order.planned_release_at - self.config.scenario.start_at
            ).total_seconds()
            / 86400,
        )
        gates = [self.environment.timeout(release_days)]
        gates.extend(
            self.simulation.completions[dependency]
            for dependency in self.order.depends_on_provider_order_ids
        )
        yield self.environment.all_of(gates)

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
        self.forecast_delivery_at = self._event_time() + timedelta(days=total_days)
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
            yield self.environment.timeout(update_after_days)
            self.production_days_elapsed += update_after_days
            self.completed_quantity = min(
                self.order.quantity,
                self.completed_quantity
                + self.capability.daily_capacity * update_after_days,
            )
            self._emit(EventType.PROGRESS_UPDATED)
            if self._can_generate_failure():
                if self.random is None:
                    raise AssertionError("missing order-local random stream")
                draw = self.random.random()
                probability = (
                    self.provider.disruption_model.daily_failure_probability
                )
                if draw < probability:
                    incident = self._sample_incident()
                    self.failure_count += 1
                    yield from self._pause_for_incident(
                        incident,
                        random_draw=draw,
                        trigger_probability=probability,
                    )

        self._transition(OrderState.QUALITY_CHECK, EventType.QUALITY_CHECK_STARTED)
        yield self.environment.timeout(self.capability.quality_check_days)
        self._transition(OrderState.READY_TO_SHIP, EventType.QUALITY_CHECK_PASSED)
        self._transition(OrderState.SHIPPED, EventType.SHIPMENT_DISPATCHED)
        yield self.environment.timeout(self.provider.operations.transit_days)
        self._transition(OrderState.DELIVERED, EventType.ORDER_DELIVERED)
        self.simulation.completions[self.order.provider_order_id].succeed(
            self.environment.now
        )

    def _event_time(self):
        return self.config.scenario.start_at + timedelta(
            days=float(self.environment.now)
        )

    def _can_generate_failure(self) -> bool:
        return (
            self.random is not None
            and self.failure_count
            < self.provider.disruption_model.maximum_failures_per_order
            and self.completed_quantity < self.order.quantity
        )

    def _sample_incident(self) -> EquipmentFailureConfig:
        if self.random is None:
            raise AssertionError("stochastic incident requested without random stream")
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
                f"STOCH-{self.order.provider_order_id}-"
                f"{self.failure_count + 1:04d}"
            ),
            incident_type="equipment_failure",
            purchase_order_id=self.order.provider_order_id,
            trigger_after_production_days=self.production_days_elapsed,
            downtime_days=downtime,
            reason_code=reason,
        )

    def _pause_for_incident(
        self,
        incident: EquipmentFailureConfig,
        random_draw: float,
        trigger_probability: float,
    ):
        previous_forecast = self.forecast_delivery_at
        self.forecast_delivery_at += timedelta(days=incident.downtime_days)
        self.machine.transition(OrderState.BLOCKED)
        self._emit(
            EventType.EQUIPMENT_FAILURE,
            incident=incident,
            previous_forecast=previous_forecast,
            random_draw=random_draw,
            trigger_probability=trigger_probability,
        )
        yield self.environment.timeout(incident.downtime_days)
        self.machine.transition(OrderState.IN_PRODUCTION)
        self._emit(
            EventType.PRODUCTION_RESUMED,
            incident=incident,
            random_draw=random_draw,
            trigger_probability=trigger_probability,
        )

    def _transition(self, state: OrderState, event_type: EventType) -> None:
        self.machine.transition(state)
        self._emit(event_type)

    def _emit(
        self,
        event_type: EventType,
        *,
        communicate: bool = True,
        incident: EquipmentFailureConfig | None = None,
        previous_forecast=None,
        random_draw: float | None = None,
        trigger_probability: float | None = None,
    ) -> None:
        self._event_number += 1
        event = GroundTruthEvent(
            generator_version=__version__,
            scenario_id=self.config.scenario.scenario_id,
            scenario_version=self.config.scenario.scenario_version,
            event_id=(
                f"{self.config.scenario.scenario_id}:"
                f"{self.order.provider_order_id}:{self._event_number:04d}"
            ),
            event_type=event_type,
            state=self.machine.state,
            provider_profile_id=self.provider.profile_id,
            source_provider_id=self.provider.source.provider_id,
            capability_id=self.order.capability_id,
            purchase_order_id=self.order.provider_order_id,
            event_time=self._event_time(),
            sim_time_days=float(self.environment.now),
            quantity_ordered=self.order.quantity,
            quantity_completed=self.completed_quantity,
            unit=self.order.unit,
            progress_pct=100 * self.completed_quantity / self.order.quantity,
            forecast_delivery_at=self.forecast_delivery_at,
            previous_forecast_delivery_at=previous_forecast,
            incident_id=incident.incident_id if incident else None,
            incident_type=incident.incident_type if incident else None,
            reason_code=incident.reason_code if incident else None,
            delay_days=incident.downtime_days if incident else None,
            incident_source="stochastic" if incident else None,
            random_stream_version=(
                self.random_stream_version if incident else None
            ),
            random_draw=random_draw,
            trigger_probability=trigger_probability,
            sampled_downtime_days=incident.downtime_days if incident else None,
        )
        self.simulation.events.append(event)
        if communicate:
            self.simulation.messages.append(self._render_message(event))

    def _render_message(self, event: GroundTruthEvent) -> ProviderMessage:
        date = event.event_time.date().isoformat()
        delivery = event.forecast_delivery_at.date().isoformat()
        order_id = event.purchase_order_id
        quantity = f"{event.quantity_ordered:g}"
        completed = f"{event.quantity_completed:g}"
        reason = (event.reason_code or "equipment").replace("_", " ")
        is_work = self.order.order_type == "work_order"
        content = {
            EventType.ORDER_ACKNOWLEDGED: (
                "Order acknowledged",
                f"We have acknowledged {order_id} for {quantity} "
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
                f"({completed} of {quantity} {event.unit}s).",
            ),
            EventType.EQUIPMENT_FAILURE: (
                "Production delay",
                f"An {reason} issue has paused production for {order_id}. "
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
                f"{order_id} passed quality checks and is ready for handoff."
                if is_work
                else f"{order_id} passed quality checks and is ready to ship.",
            ),
            EventType.SHIPMENT_DISPATCHED: (
                "Output dispatched" if is_work else "Shipment dispatched",
                f"Output for {order_id} was transferred on {date}; "
                f"handoff is expected on {delivery}."
                if is_work
                else f"{order_id} shipped on {date}; delivery is expected on {delivery}.",
            ),
            EventType.ORDER_DELIVERED: (
                "Work output delivered" if is_work else "Order delivered",
                f"Output handoff for {order_id} was completed on {date}."
                if is_work
                else f"Delivery of {order_id} was completed on {date}.",
            ),
        }[event.event_type]
        self._message_number += 1
        return ProviderMessage(
            generator_version=__version__,
            scenario_id=event.scenario_id,
            message_id=(
                f"{event.scenario_id}:{event.purchase_order_id}:"
                f"MSG-{self._message_number:04d}"
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
