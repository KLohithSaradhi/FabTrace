from pathlib import Path

import pytest
from pydantic import ValidationError

from fabtrace.source.loader import SourceDataError, load_source_catalog
from fabtrace.source.models import SequenceRelationship
from fabtrace.source.validation import validate_source_catalog


DATA_DIR = Path(__file__).parents[1] / "Data"


def test_real_source_catalog_loads_and_validates() -> None:
    catalog = load_source_catalog(DATA_DIR)
    report = validate_source_catalog(catalog)

    assert report.is_valid
    assert report.errors == ()
    assert len(catalog.stages) == 3
    assert len(catalog.inputs) == 126
    assert len(catalog.providers) == 396
    assert catalog.provider_row_count == 403
    assert len(catalog.provisions) == 1300
    assert len(catalog.sequence) == 139


def test_provider_alias_rows_are_aggregated_without_loss() -> None:
    catalog = load_source_catalog(DATA_DIR)

    canon = catalog.providers["P159"]
    assert canon.provider_name == "Canon"
    assert canon.source_row_count == 3
    assert set(canon.aliases) == {"Canon ANELVA", "Canon Machinery"}


def test_sequence_requires_exactly_one_relationship_type() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        SequenceRelationship(
            input_name="Example",
            input_id="N1",
            goes_into_name="Target",
            goes_into_id="N2",
            is_type_of_name="Category",
            is_type_of_id="N3",
        )


def test_missing_source_file_has_a_useful_error(tmp_path: Path) -> None:
    with pytest.raises(SourceDataError, match="missing required source file"):
        load_source_catalog(tmp_path)


def test_missing_provider_reference_is_reported() -> None:
    catalog = load_source_catalog(DATA_DIR)
    broken_provision = catalog.provisions[0].model_copy(
        update={"provider_id": "P-MISSING"}
    )
    broken_catalog = catalog.model_copy(
        update={"provisions": (broken_provision, *catalog.provisions[1:])}
    )

    report = validate_source_catalog(broken_catalog)

    assert not report.is_valid
    assert any("missing provider P-MISSING" in error for error in report.errors)
