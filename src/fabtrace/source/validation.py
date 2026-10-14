from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from fabtrace.source.models import SourceCatalog


class ValidationReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    errors: tuple[str, ...] = ()

    @property
    def is_valid(self) -> bool:
        return not self.errors


def validate_source_catalog(catalog: SourceCatalog) -> ValidationReport:
    """Check relationships that require more than one source table."""

    errors: list[str] = []

    for node in catalog.inputs.values():
        if node.stage_id is not None and node.stage_id not in catalog.stages:
            errors.append(
                f"input {node.input_id} references missing stage {node.stage_id}"
            )

    for index, relationship in enumerate(catalog.sequence, start=2):
        if relationship.input_id not in catalog.inputs:
            errors.append(
                f"sequence.csv:{index} references missing input "
                f"{relationship.input_id}"
            )
        if relationship.target_id not in catalog.inputs:
            errors.append(
                f"sequence.csv:{index} references missing target "
                f"{relationship.target_id}"
            )

    supplied_entities = catalog.inputs.keys() | catalog.stages.keys()
    for index, provision in enumerate(catalog.provisions, start=2):
        if provision.provider_id not in catalog.providers:
            errors.append(
                f"provision.csv:{index} references missing provider "
                f"{provision.provider_id}"
            )
        if provision.provided_id not in supplied_entities:
            errors.append(
                f"provision.csv:{index} references missing supplied entity "
                f"{provision.provided_id}"
            )
        if provision.share_provided is not None and not (
            0 <= provision.share_provided <= 100
        ):
            errors.append(
                f"provision.csv:{index} has out-of-range share "
                f"{provision.share_provided}"
            )

    return ValidationReport(errors=tuple(errors))
