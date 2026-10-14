from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import ValidationError

from fabtrace.planning.models import (
    ORDER_ID_PATTERN,
    OrderPlan,
    PlanSummary,
)


class PlanStorageError(RuntimeError):
    """Raised when an immutable plan artifact cannot be stored safely."""


class PlanAlreadyExistsError(PlanStorageError):
    """Raised when an order ID already has a plan artifact."""


class PlanNotFoundError(PlanStorageError):
    """Raised when a requested plan does not exist."""


class PlanArtifactStore:
    def __init__(self, root_directory: Path) -> None:
        self.root_directory = Path(root_directory).resolve()

    def _order_directory(self, order_id: str) -> Path:
        if re.fullmatch(ORDER_ID_PATTERN, order_id) is None:
            raise PlanStorageError(f"invalid order ID: {order_id!r}")
        path = self.root_directory / order_id
        if not path.resolve().is_relative_to(self.root_directory):
            raise PlanStorageError("plan path escapes its storage root")
        return path

    def plan_path(self, order_id: str) -> Path:
        return self._order_directory(order_id) / "plan.json"

    def save(self, plan: OrderPlan) -> Path:
        serialized = json.dumps(
            plan.model_dump(mode="json"),
            indent=2,
            sort_keys=True,
        ) + "\n"
        self.root_directory.mkdir(parents=True, exist_ok=True)
        order_directory = self._order_directory(plan.order_id)
        try:
            order_directory.mkdir(exist_ok=False)
        except FileExistsError as exc:
            raise PlanAlreadyExistsError(
                f"plan already exists for order {plan.order_id!r}"
            ) from exc
        path = order_directory / "plan.json"
        temporary = order_directory / "plan.json.tmp"
        try:
            temporary.write_text(serialized, encoding="utf-8")
            temporary.replace(path)
        except OSError as exc:
            raise PlanStorageError(f"cannot store plan artifact: {exc}") from exc
        return path

    def load(self, order_id: str) -> OrderPlan:
        path = self.plan_path(order_id)
        if not path.is_file():
            raise PlanNotFoundError(f"plan does not exist: {order_id}")
        try:
            return OrderPlan.model_validate_json(path.read_text("utf-8"))
        except (OSError, ValidationError) as exc:
            raise PlanStorageError(f"cannot read plan artifact: {exc}") from exc

    def list_plans(self) -> list[PlanSummary]:
        if not self.root_directory.is_dir():
            return []
        summaries: list[PlanSummary] = []
        for path in sorted(self.root_directory.glob("*/plan.json")):
            plan = self.load(path.parent.name)
            summaries.append(
                PlanSummary(
                    order_id=plan.order_id,
                    product_id=plan.product_id,
                    product_version=plan.product_version,
                    requested_quantity=plan.requested_quantity,
                    output_unit=plan.output_unit,
                    requested_at=plan.requested_at,
                    expected_delivery_at=plan.expected_delivery_at,
                    provider_order_count=len(plan.provider_orders),
                )
            )
        return summaries
