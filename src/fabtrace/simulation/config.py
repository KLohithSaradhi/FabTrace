from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from fabtrace import __version__
from fabtrace.planning.models import OrderPlan
from fabtrace.source.models import ProviderType, SourceCatalog
from fabtrace.simulation.models import (
    EffectiveConfig,
    EffectivePlanConfig,
    EffectivePlanSource,
    EffectiveProvider,
    EffectiveScenario,
    PlanExecutionOrder,
    PlanExecutionScenario,
    ProviderProfileReference,
    ProviderCapabilityConfig,
    ProviderProfile,
    ScenarioConfig,
    SourceCapabilitySnapshot,
    SourceProviderSnapshot,
)

ConfigModel = TypeVar("ConfigModel", bound=BaseModel)


class SimulationConfigError(ValueError):
    """Raised when provider or scenario configuration is invalid."""


def _load_model(path: Path, model: type[ConfigModel]) -> ConfigModel:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return model.model_validate(raw)
    except FileNotFoundError as exc:
        raise SimulationConfigError(f"configuration file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SimulationConfigError(f"invalid JSON in {path}: {exc}") from exc
    except ValidationError as exc:
        raise SimulationConfigError(f"invalid configuration in {path}: {exc}") from exc


def _country_name(catalog: SourceCatalog, country_code: str | None) -> str | None:
    if country_code is None:
        return None
    for provider in catalog.providers.values():
        if (
            provider.provider_type == ProviderType.COUNTRY
            and provider.provider_name == country_code
        ):
            return provider.aliases[0] if provider.aliases else provider.provider_name
    return None


def _load_profiles(
    scenario_path: Path,
    references: tuple[ProviderProfileReference, ...],
) -> dict[str, ProviderProfile]:
    profiles: dict[str, ProviderProfile] = {}
    for reference in references:
        profile_path = (scenario_path.parent / reference.profile).resolve()
        profile = _load_model(profile_path, ProviderProfile)
        if profile.profile_id in profiles:
            raise SimulationConfigError(
                f"duplicate provider profile ID: {profile.profile_id}"
            )
        profiles[profile.profile_id] = profile
    return profiles


def _resolve_providers(
    profiles: dict[str, ProviderProfile],
    catalog: SourceCatalog,
) -> tuple[EffectiveProvider, ...]:
    effective_providers: list[EffectiveProvider] = []
    for profile in profiles.values():
        provider = catalog.providers.get(profile.source_provider_id)
        if provider is None:
            raise SimulationConfigError(
                f"provider {profile.source_provider_id} does not exist in ETO data"
            )
        if provider.provider_type != ProviderType.ORGANIZATION:
            raise SimulationConfigError(
                f"provider {provider.provider_id} must be an organization"
            )

        source_capabilities: list[SourceCapabilitySnapshot] = []
        seen_capabilities: set[str] = set()
        for capability_config in profile.capabilities:
            capability_id = capability_config.source_capability_id
            if capability_id in seen_capabilities:
                raise SimulationConfigError(
                    f"profile {profile.profile_id} repeats capability {capability_id}"
                )
            seen_capabilities.add(capability_id)

            node = catalog.inputs.get(capability_id)
            if node is None:
                raise SimulationConfigError(
                    f"capability {capability_id} does not exist in ETO data"
                )
            provisions = [
                relation
                for relation in catalog.provisions
                if relation.provider_id == provider.provider_id
                and relation.provided_id == capability_id
            ]
            if not provisions:
                raise SimulationConfigError(
                    f"ETO data does not show {provider.provider_id} providing "
                    f"{capability_id}"
                )
            relation = provisions[0]
            source_capabilities.append(
                SourceCapabilitySnapshot(
                    input_id=node.input_id,
                    input_name=node.input_name,
                    input_type=node.type.value,
                    description=node.description,
                    source_year=node.year,
                    market_size_info=node.market_size_info,
                    share_provided=relation.share_provided,
                    provision_year=relation.year,
                    source_citation=relation.source,
                )
            )

        effective_providers.append(
            EffectiveProvider(
                profile_id=profile.profile_id,
                profile_version=profile.profile_version,
                simulation_name=profile.simulation_name,
                synthetic=True,
                source=SourceProviderSnapshot(
                    provider_id=provider.provider_id,
                    provider_name=provider.provider_name,
                    provider_type=provider.provider_type.value,
                    country_code=provider.country,
                    country_name=_country_name(catalog, provider.country),
                    aliases=provider.aliases,
                ),
                capabilities=tuple(source_capabilities),
                synthetic_capabilities=profile.capabilities,
                operations=profile.operations,
                communication=profile.communication,
                disruption_model=profile.disruption_model,
            )
        )
    return tuple(effective_providers)


def resolve_effective_config(
    scenario_path: Path,
    catalog: SourceCatalog,
) -> EffectiveConfig:
    scenario_path = Path(scenario_path).resolve()
    scenario = _load_model(scenario_path, ScenarioConfig)

    profiles = _load_profiles(scenario_path, scenario.providers)
    effective_providers = _resolve_providers(profiles, catalog)

    orders_by_id = {order.purchase_order_id: order for order in scenario.purchase_orders}
    if len(orders_by_id) != len(scenario.purchase_orders):
        raise SimulationConfigError("purchase order IDs must be unique")

    capabilities_by_order: dict[str, ProviderCapabilityConfig] = {}
    for order in scenario.purchase_orders:
        profile = profiles.get(order.provider_profile_id)
        if profile is None:
            raise SimulationConfigError(
                f"order {order.purchase_order_id} references unknown profile "
                f"{order.provider_profile_id}"
            )
        capability = next(
            (
                item
                for item in profile.capabilities
                if item.source_capability_id == order.capability_id
            ),
            None,
        )
        if capability is None:
            raise SimulationConfigError(
                f"order {order.purchase_order_id} uses unconfigured capability "
                f"{order.capability_id}"
            )
        if capability.unit != order.unit:
            raise SimulationConfigError(
                f"order {order.purchase_order_id} unit {order.unit!r} does not "
                f"match capability unit {capability.unit!r}"
            )
        capabilities_by_order[order.purchase_order_id] = capability

    if scenario.incidents and scenario.incident_generation is not None:
        raise SimulationConfigError(
            "explicit incidents and stochastic incident generation are mutually exclusive"
        )

    incident_ids: set[str] = set()
    incident_orders: set[str] = set()
    for incident in scenario.incidents:
        if incident.incident_id in incident_ids:
            raise SimulationConfigError(f"duplicate incident ID: {incident.incident_id}")
        incident_ids.add(incident.incident_id)
        order = orders_by_id.get(incident.purchase_order_id)
        if order is None:
            raise SimulationConfigError(
                f"incident {incident.incident_id} references unknown order "
                f"{incident.purchase_order_id}"
            )
        if incident.purchase_order_id in incident_orders:
            raise SimulationConfigError(
                "the current simulator supports at most one incident per order"
            )
        incident_orders.add(incident.purchase_order_id)
        capability = capabilities_by_order[incident.purchase_order_id]
        production_days = math.ceil(order.quantity / capability.daily_capacity)
        if incident.trigger_after_production_days >= production_days:
            raise SimulationConfigError(
                f"incident {incident.incident_id} must trigger before production "
                f"completes on day {production_days}"
            )

    return EffectiveConfig(
        schema_version="1.0",
        generator_version=__version__,
        scenario=EffectiveScenario(
            scenario_id=scenario.scenario_id,
            scenario_version=scenario.scenario_version,
            seed=scenario.seed,
            synthetic=True,
            start_at=scenario.start_at,
        ),
        providers=effective_providers,
        purchase_orders=scenario.purchase_orders,
        incidents=scenario.incidents,
        incident_generation=scenario.incident_generation,
    )


def resolve_plan_effective_config(
    plan_path: Path,
    scenario_path: Path,
    catalog: SourceCatalog,
) -> EffectivePlanConfig:
    plan_path = Path(plan_path).resolve()
    scenario_path = Path(scenario_path).resolve()
    plan = _load_model(plan_path, OrderPlan)
    scenario = _load_model(scenario_path, PlanExecutionScenario)
    profiles = _load_profiles(scenario_path, scenario.providers)
    providers = _resolve_providers(profiles, catalog)

    profiles_by_source: dict[str, EffectiveProvider] = {}
    for provider in providers:
        source_id = provider.source.provider_id
        if source_id in profiles_by_source:
            raise SimulationConfigError(
                f"multiple profiles map to source provider {source_id}"
            )
        profiles_by_source[source_id] = provider

    execution_orders: list[PlanExecutionOrder] = []
    order_ids = {order.provider_order_id for order in plan.provider_orders}
    for order in plan.provider_orders:
        provider = profiles_by_source.get(order.provider_id)
        if provider is None:
            raise SimulationConfigError(
                f"plan provider {order.provider_id} has no execution profile"
            )
        if not order.capability_id:
            raise SimulationConfigError(
                f"plan order {order.provider_order_id} has no ETO capability ID"
            )
        capability = next(
            (
                item
                for item in provider.synthetic_capabilities
                if item.source_capability_id == order.capability_id
            ),
            None,
        )
        if capability is None:
            raise SimulationConfigError(
                f"plan order {order.provider_order_id} capability "
                f"{order.capability_id} is not configured for provider "
                f"{order.provider_id}"
            )
        if capability.unit != order.unit:
            raise SimulationConfigError(
                f"plan order {order.provider_order_id} unit {order.unit!r} does "
                f"not match capability unit {capability.unit!r}"
            )
        unknown = set(order.depends_on_provider_order_ids) - order_ids
        if unknown:
            raise SimulationConfigError(
                f"plan order {order.provider_order_id} has unknown dependencies: "
                + ", ".join(sorted(unknown))
            )
        execution_orders.append(
            PlanExecutionOrder(
                provider_order_id=order.provider_order_id,
                order_type=order.order_type,
                node_id=order.node_id,
                provider_profile_id=provider.profile_id,
                source_provider_id=provider.source.provider_id,
                capability_id=order.capability_id,
                quantity=order.quantity,
                unit=order.unit,
                planned_release_at=order.release_at.astimezone(UTC),
                depends_on_provider_order_ids=tuple(
                    order.depends_on_provider_order_ids
                ),
            )
        )

    remaining = {
        order.provider_order_id: set(order.depends_on_provider_order_ids)
        for order in execution_orders
    }
    resolved: set[str] = set()
    while remaining:
        ready = sorted(
            order_id
            for order_id, dependencies in remaining.items()
            if dependencies <= resolved
        )
        if not ready:
            raise SimulationConfigError(
                "plan provider-order dependencies contain a cycle"
            )
        for order_id in ready:
            resolved.add(order_id)
            remaining.pop(order_id)

    try:
        plan_bytes = plan_path.read_bytes()
    except OSError as exc:
        raise SimulationConfigError(
            f"cannot fingerprint plan {plan_path}: {exc}"
        ) from exc
    return EffectivePlanConfig(
        schema_version="1.0",
        generator_version=__version__,
        scenario=EffectiveScenario(
            scenario_id=scenario.scenario_id,
            scenario_version=scenario.scenario_version,
            seed=scenario.seed,
            synthetic=True,
            start_at=plan.requested_at.astimezone(UTC),
        ),
        plan=EffectivePlanSource(
            order_id=plan.order_id,
            product_id=plan.product_id,
            product_version=plan.product_version,
            planner_version=plan.planner_version,
            source_path=str(plan_path),
            sha256=hashlib.sha256(plan_bytes).hexdigest(),
        ),
        providers=providers,
        provider_orders=tuple(execution_orders),
        incident_generation=scenario.incident_generation,
    )
