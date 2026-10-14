from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fabtrace.planning.engine import PlanningModelError, build_order_plan
from fabtrace.planning.models import OrderPlan, PlanOrderRequest
from fabtrace.planning.storage import (
    PlanAlreadyExistsError,
    PlanArtifactStore,
    PlanStorageError,
)
from fabtrace.products.storage import ProductConfigStore, ProductStorageError


class PlanningError(RuntimeError):
    """Raised when an order cannot be planned and persisted."""


class PlanningConflictError(PlanningError):
    """Raised when an immutable order plan already exists."""


@dataclass(frozen=True)
class PlanningResult:
    plan: OrderPlan
    plan_path: Path


def plan_order(
    product_store: ProductConfigStore,
    plan_store: PlanArtifactStore,
    request: PlanOrderRequest,
) -> PlanningResult:
    try:
        recipe = product_store.get_version(
            request.product_id,
            request.product_version,
        )
        fingerprint = product_store.version_sha256(
            request.product_id,
            request.product_version,
        )
        plan = build_order_plan(recipe, fingerprint, request)
        path = plan_store.save(plan)
    except PlanAlreadyExistsError as exc:
        raise PlanningConflictError(str(exc)) from exc
    except (ProductStorageError, PlanningModelError, PlanStorageError) as exc:
        raise PlanningError(str(exc)) from exc
    return PlanningResult(plan=plan, plan_path=path)
