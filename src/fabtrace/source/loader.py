from __future__ import annotations

import csv
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, TypeVar

from pydantic import ValidationError

from fabtrace.source.models import (
    InputNode,
    Provider,
    ProviderRow,
    Provision,
    SequenceRelationship,
    SourceCatalog,
    Stage,
)

Record = TypeVar("Record")


class SourceDataError(ValueError):
    """Raised when source files cannot be represented by the source contract."""


REQUIRED_COLUMNS = {
    "stages.csv": {
        "stage_id",
        "stage_name",
        "description",
        "market_share_chart_global_market_size_info",
        "market_share_chart_caption",
        "market_share_chart_source",
    },
    "inputs.csv": {
        "input_id",
        "input_name",
        "type",
        "stage_name",
        "stage_id",
        "description",
        "year",
        "market_share_chart_global_market_size_info",
        "market_share_chart_caption",
        "market_share_chart_source",
    },
    "providers.csv": {
        "provider_name",
        "alias",
        "provider_id",
        "provider_type",
        "country",
    },
    "provision.csv": {
        "provider_name",
        "provider_id",
        "provided_name",
        "provided_id",
        "share_provided",
        "year",
        "source",
    },
    "sequence.csv": {
        "input_name",
        "input_id",
        "goes_into_name",
        "goes_into_id",
        "is_type_of_name",
        "is_type_of_id",
    },
}


def _optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise SourceDataError(f"missing required source file: {path}")

    with path.open(encoding="utf-8-sig", newline="") as source_file:
        reader = csv.DictReader(source_file)
        headers = set(reader.fieldnames or ())
        missing = REQUIRED_COLUMNS[path.name] - headers
        if missing:
            missing_list = ", ".join(sorted(missing))
            raise SourceDataError(f"{path.name} is missing columns: {missing_list}")
        return [dict(row) for row in reader]


def _parse_rows(
    path: Path,
    factory: Callable[[dict[str, str]], Record],
) -> list[Record]:
    parsed: list[Record] = []
    for row_number, row in enumerate(_read_rows(path), start=2):
        try:
            parsed.append(factory(row))
        except (ValidationError, ValueError) as exc:
            raise SourceDataError(f"{path.name}:{row_number}: {exc}") from exc
    return parsed


def _unique_by_id(
    records: Iterable[Record],
    id_getter: Callable[[Record], str],
    source_name: str,
) -> dict[str, Record]:
    indexed: dict[str, Record] = {}
    for record in records:
        record_id = id_getter(record)
        if record_id in indexed:
            raise SourceDataError(f"{source_name} contains duplicate ID {record_id}")
        indexed[record_id] = record
    return indexed


def _stage(row: dict[str, str]) -> Stage:
    return Stage(
        stage_id=row["stage_id"],
        stage_name=row["stage_name"],
        description=row["description"],
        market_size_info=_optional(
            row["market_share_chart_global_market_size_info"]
        ),
        market_share_caption=_optional(row["market_share_chart_caption"]),
        market_share_source=_optional(row["market_share_chart_source"]),
    )


def _input(row: dict[str, str]) -> InputNode:
    return InputNode(
        input_id=row["input_id"],
        input_name=row["input_name"],
        type=row["type"],
        stage_name=_optional(row["stage_name"]),
        stage_id=_optional(row["stage_id"]),
        description=row["description"],
        year=int(row["year"]) if _optional(row["year"]) else None,
        market_size_info=_optional(
            row["market_share_chart_global_market_size_info"]
        ),
        market_share_caption=_optional(row["market_share_chart_caption"]),
        market_share_source=_optional(row["market_share_chart_source"]),
    )


def _provider_row(row: dict[str, str]) -> ProviderRow:
    return ProviderRow(
        provider_name=row["provider_name"],
        alias=_optional(row["alias"]),
        provider_id=row["provider_id"],
        provider_type=row["provider_type"],
        country=_optional(row["country"]),
    )


def _provision(row: dict[str, str]) -> Provision:
    share = _optional(row["share_provided"])
    return Provision(
        provider_name=row["provider_name"],
        provider_id=row["provider_id"],
        provided_name=row["provided_name"],
        provided_id=row["provided_id"],
        share_provided=float(share.strip("%")) if share is not None else None,
        year=int(row["year"]),
        source=_optional(row["source"]),
    )


def _sequence(row: dict[str, str]) -> SequenceRelationship:
    return SequenceRelationship(
        input_name=row["input_name"],
        input_id=row["input_id"],
        goes_into_name=_optional(row["goes_into_name"]),
        goes_into_id=_optional(row["goes_into_id"]),
        is_type_of_name=_optional(row["is_type_of_name"]),
        is_type_of_id=_optional(row["is_type_of_id"]),
    )


def _aggregate_providers(rows: Iterable[ProviderRow]) -> dict[str, Provider]:
    grouped: dict[str, list[ProviderRow]] = {}
    for row in rows:
        grouped.setdefault(row.provider_id, []).append(row)

    providers: dict[str, Provider] = {}
    for provider_id, provider_rows in grouped.items():
        first = provider_rows[0]
        for row in provider_rows[1:]:
            identity = (row.provider_name, row.provider_type, row.country)
            first_identity = (
                first.provider_name,
                first.provider_type,
                first.country,
            )
            if identity != first_identity:
                raise SourceDataError(
                    f"providers.csv has conflicting rows for {provider_id}"
                )

        aliases = tuple(
            dict.fromkeys(
                row.alias for row in provider_rows if row.alias is not None
            )
        )
        providers[provider_id] = Provider(
            provider_id=provider_id,
            provider_name=first.provider_name,
            provider_type=first.provider_type,
            country=first.country,
            aliases=aliases,
            source_row_count=len(provider_rows),
        )
    return providers


def load_source_catalog(data_dir: Path) -> SourceCatalog:
    """Load immutable ETO CSV snapshots into validated source models."""

    data_dir = Path(data_dir)
    stages = _parse_rows(data_dir / "stages.csv", _stage)
    inputs = _parse_rows(data_dir / "inputs.csv", _input)
    provider_rows = _parse_rows(data_dir / "providers.csv", _provider_row)
    provisions = _parse_rows(data_dir / "provision.csv", _provision)
    sequence = _parse_rows(data_dir / "sequence.csv", _sequence)

    return SourceCatalog(
        stages=_unique_by_id(stages, lambda record: record.stage_id, "stages.csv"),
        inputs=_unique_by_id(inputs, lambda record: record.input_id, "inputs.csv"),
        providers=_aggregate_providers(provider_rows),
        provisions=tuple(provisions),
        sequence=tuple(sequence),
        provider_row_count=len(provider_rows),
    )
