from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator


class SourceModel(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)


class NodeType(StrEnum):
    TOOL_RESOURCE = "tool_resource"
    MATERIAL_RESOURCE = "material_resource"
    PROCESS = "process"
    DESIGN_RESOURCE = "design_resource"
    ULTIMATE_OUTPUT = "ultimate_output"


class ProviderType(StrEnum):
    COUNTRY = "country"
    ORGANIZATION = "organization"


class Stage(SourceModel):
    stage_id: str
    stage_name: str
    description: str
    market_size_info: str | None = None
    market_share_caption: str | None = None
    market_share_source: str | None = None


class InputNode(SourceModel):
    input_id: str
    input_name: str
    type: NodeType
    stage_name: str | None = None
    stage_id: str | None = None
    description: str
    year: int | None = None
    market_size_info: str | None = None
    market_share_caption: str | None = None
    market_share_source: str | None = None


class ProviderRow(SourceModel):
    provider_name: str
    alias: str | None = None
    provider_id: str
    provider_type: ProviderType
    country: str | None = None


class Provider(SourceModel):
    provider_id: str
    provider_name: str
    provider_type: ProviderType
    country: str | None = None
    aliases: tuple[str, ...] = ()
    source_row_count: int = 1


class Provision(SourceModel):
    provider_name: str
    provider_id: str
    provided_name: str
    provided_id: str
    share_provided: float | None = None
    year: int
    source: str | None = None


class SequenceRelationship(SourceModel):
    input_name: str
    input_id: str
    goes_into_name: str | None = None
    goes_into_id: str | None = None
    is_type_of_name: str | None = None
    is_type_of_id: str | None = None

    @model_validator(mode="after")
    def exactly_one_relationship(self) -> "SequenceRelationship":
        has_goes_into = self.goes_into_id is not None
        has_is_type_of = self.is_type_of_id is not None
        if has_goes_into == has_is_type_of:
            raise ValueError(
                "exactly one of goes_into_id and is_type_of_id must be populated"
            )
        if has_goes_into != (self.goes_into_name is not None):
            raise ValueError("goes_into_id and goes_into_name must be populated together")
        if has_is_type_of != (self.is_type_of_name is not None):
            raise ValueError(
                "is_type_of_id and is_type_of_name must be populated together"
            )
        return self

    @property
    def target_id(self) -> str:
        target = self.goes_into_id or self.is_type_of_id
        if target is None:  # guarded by model validation
            raise AssertionError("relationship has no target")
        return target


class SourceCatalog(SourceModel):
    stages: dict[str, Stage]
    inputs: dict[str, InputNode]
    providers: dict[str, Provider]
    provisions: tuple[Provision, ...]
    sequence: tuple[SequenceRelationship, ...]
    provider_row_count: int
