from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from fabtrace.source import load_source_catalog, validate_source_catalog
from fabtrace.source.loader import SourceDataError
from fabtrace.source.models import ProviderType, SourceCatalog


class ProviderCandidate(BaseModel):
    provider_id: str
    provider_name: str
    country_code: str | None
    aliases: list[str]
    provision_year: int
    share_provided: float | None


class ProviderCandidateResponse(BaseModel):
    api_version: str
    capability_id: str
    capability_name: str
    count: int
    candidates: list[ProviderCandidate]


class SourceCatalogReader:
    def __init__(self, data_directory: Path) -> None:
        self.data_directory = Path(data_directory).resolve()
        self._catalog: SourceCatalog | None = None

    def catalog(self) -> SourceCatalog:
        if self._catalog is None:
            catalog = load_source_catalog(self.data_directory)
            report = validate_source_catalog(catalog)
            if not report.is_valid:
                raise SourceDataError(
                    "source data failed validation: " + "; ".join(report.errors)
                )
            self._catalog = catalog
        return self._catalog


def attach_source_routes(app: FastAPI, data_directory: Path) -> None:
    reader = SourceCatalogReader(data_directory)
    app.state.source_catalog_reader = reader

    @app.get(
        "/api/v1/source/capabilities/{capability_id}/providers",
        response_model=ProviderCandidateResponse,
        tags=["source reference"],
    )
    async def capability_provider_candidates(
        capability_id: str,
    ) -> ProviderCandidateResponse:
        try:
            catalog = reader.catalog()
        except SourceDataError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        node = catalog.inputs.get(capability_id)
        stage = catalog.stages.get(capability_id)
        if node is None and stage is None:
            raise HTTPException(
                status_code=404,
                detail=f"ETO capability does not exist: {capability_id}",
            )
        candidates: list[ProviderCandidate] = []
        for relation in catalog.provisions:
            if relation.provided_id != capability_id:
                continue
            provider = catalog.providers.get(relation.provider_id)
            if provider is None or provider.provider_type != ProviderType.ORGANIZATION:
                continue
            candidates.append(
                ProviderCandidate(
                    provider_id=provider.provider_id,
                    provider_name=provider.provider_name,
                    country_code=provider.country,
                    aliases=list(provider.aliases),
                    provision_year=relation.year,
                    share_provided=relation.share_provided,
                )
            )
        candidates.sort(key=lambda item: item.provider_id)
        return ProviderCandidateResponse(
            api_version="v1",
            capability_id=capability_id,
            capability_name=(
                node.input_name if node is not None else stage.stage_name
            ),
            count=len(candidates),
            candidates=candidates,
        )
