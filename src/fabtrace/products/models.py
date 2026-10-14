from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SAFE_ID_PATTERN = r"^[a-z][a-z0-9-]{0,62}$"
NodeKind = Literal["material", "process", "capability", "output"]


class CanvasPosition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: float
    y: float


class Requirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(pattern=SAFE_ID_PATTERN)
    quantity: float = Field(gt=0, allow_inf_nan=False)
    unit: str = Field(min_length=1, max_length=40)

    @field_validator("unit")
    @classmethod
    def normalize_unit(cls, value: str) -> str:
        return value.strip()


class ProductNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    kind: NodeKind
    unit: str | None = Field(default=None, max_length=40)
    lead_time_days: float | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )
    duration_days: float | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )
    eto_reference_id: str | None = Field(default=None, max_length=80)
    provider_candidates: list[str] = Field(default_factory=list)
    position: CanvasPosition = Field(
        default_factory=lambda: CanvasPosition(x=0, y=0)
    )
    requires: list[Requirement] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("unit", "eto_reference_id")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("provider_candidates")
    @classmethod
    def normalize_providers(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values if value.strip()]
        if len(normalized) != len(set(normalized)):
            raise ValueError("provider candidates must be unique")
        return normalized

    @model_validator(mode="after")
    def unique_requirements(self) -> ProductNode:
        requirement_ids = [requirement.node_id for requirement in self.requires]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("a node may require another node only once")
        return self


class ProductRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    product_id: str = Field(pattern=SAFE_ID_PATTERN)
    product_name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    synthetic: Literal[True] = True
    status: Literal["draft", "published"] = "draft"
    product_version: int | None = Field(default=None, ge=1)
    updated_at: datetime | None = None
    output_node_id: str | None = Field(default=None, pattern=SAFE_ID_PATTERN)
    nodes: dict[str, ProductNode] = Field(default_factory=dict)

    @field_validator("product_name")
    @classmethod
    def normalize_product_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_node_keys(self) -> ProductRecipe:
        import re

        for node_id in self.nodes:
            if re.fullmatch(SAFE_ID_PATTERN, node_id) is None:
                raise ValueError(f"invalid node ID: {node_id!r}")
        return self


class ValidationIssue(BaseModel):
    code: str
    message: str
    path: str


class ValidationResponse(BaseModel):
    valid: bool
    issues: list[ValidationIssue]


class ProductSummary(BaseModel):
    product_id: str
    product_name: str
    has_draft: bool
    latest_version: int | None
    latest_updated_at: datetime | None


class ProductListResponse(BaseModel):
    api_version: str
    count: int
    products: list[ProductSummary]


class RecipeResponse(BaseModel):
    api_version: str
    recipe: ProductRecipe
