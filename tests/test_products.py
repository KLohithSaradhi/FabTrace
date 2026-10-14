from __future__ import annotations

from pathlib import Path

import pytest

from fabtrace.products.models import ProductRecipe
from fabtrace.products.storage import (
    ProductConfigStore,
    ProductPublicationError,
    ProductStorageError,
)
from fabtrace.products.validation import validate_for_publish
from fabtrace.web import create_app
from test_web import ASGITestClient

EDITOR_HEADERS = {"X-FabTrace-Editor": "1"}


def _valid_recipe() -> dict:
    return {
        "schema_version": "1.0",
        "product_id": "logic-chip",
        "product_name": "Logic chip",
        "description": "Synthetic teaching recipe",
        "synthetic": True,
        "status": "draft",
        "product_version": None,
        "updated_at": None,
        "output_node_id": "finished-chip",
        "nodes": {
            "silicon-wafer": {
                "name": "Silicon wafer",
                "kind": "material",
                "unit": "wafer",
                "lead_time_days": 2,
                "duration_days": None,
                "eto_reference_id": "N1",
                "provider_candidates": ["P208"],
                "position": {"x": 100, "y": 100},
                "requires": [],
            },
            "wafer-fabrication": {
                "name": "Wafer fabrication",
                "kind": "process",
                "unit": "wafer",
                "lead_time_days": None,
                "duration_days": 4,
                "eto_reference_id": "N26",
                "provider_candidates": ["P208"],
                "position": {"x": 320, "y": 100},
                "requires": [
                    {
                        "node_id": "silicon-wafer",
                        "quantity": 1,
                        "unit": "wafer",
                    }
                ],
            },
            "finished-chip": {
                "name": "Finished chip",
                "kind": "output",
                "unit": "chip",
                "lead_time_days": None,
                "duration_days": None,
                "eto_reference_id": "N99",
                "provider_candidates": [],
                "position": {"x": 540, "y": 100},
                "requires": [
                    {
                        "node_id": "wafer-fabrication",
                        "quantity": 1,
                        "unit": "wafer",
                    }
                ],
            },
        },
    }


@pytest.fixture
def product_directories(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "published", tmp_path / "drafts"


@pytest.fixture
def product_client(
    tmp_path: Path,
    product_directories: tuple[Path, Path],
) -> ASGITestClient:
    published, drafts = product_directories
    return ASGITestClient(
        create_app(
            tmp_path / "missing-operational.db",
            product_config_directory=published,
            product_draft_directory=drafts,
        )
    )


def test_valid_recipe_passes_authoritative_validation() -> None:
    recipe = ProductRecipe.model_validate(_valid_recipe())

    assert validate_for_publish(recipe) == []


def test_material_requires_positive_lead_time_to_publish() -> None:
    payload = _valid_recipe()
    payload["nodes"]["silicon-wafer"]["lead_time_days"] = None

    issues = validate_for_publish(ProductRecipe.model_validate(payload))

    assert any(issue.code == "missing_lead_time" for issue in issues)


def test_validation_reports_cycles_units_providers_and_orphans() -> None:
    payload = _valid_recipe()
    payload["nodes"]["silicon-wafer"]["provider_candidates"] = []
    payload["nodes"]["wafer-fabrication"]["requires"][0]["unit"] = "kg"
    payload["nodes"]["silicon-wafer"]["requires"] = [
        {"node_id": "wafer-fabrication", "quantity": 1, "unit": "wafer"}
    ]
    payload["nodes"]["unused-material"] = {
        "name": "Unused material",
        "kind": "material",
        "unit": "kg",
        "lead_time_days": 1,
        "duration_days": None,
        "eto_reference_id": None,
        "provider_candidates": ["P100"],
        "position": {"x": 0, "y": 0},
        "requires": [],
    }

    issues = validate_for_publish(ProductRecipe.model_validate(payload))
    codes = {issue.code for issue in issues}

    assert {"cycle", "unit_mismatch", "missing_provider", "orphan_node"} <= codes


def test_incomplete_draft_can_be_saved_but_not_published(
    product_directories: tuple[Path, Path],
) -> None:
    published, drafts = product_directories
    store = ProductConfigStore(drafts, published)
    recipe = ProductRecipe(
        product_id="unfinished-product",
        product_name="Unfinished product",
    )

    saved = store.save_draft(recipe.product_id, recipe)

    assert saved.status == "draft"
    assert (drafts / "unfinished-product.json").is_file()
    with pytest.raises(ProductPublicationError, match="exactly one"):
        store.publish(recipe.product_id)
    assert not (published / "unfinished-product").exists()


def test_publish_creates_immutable_numbered_versions(
    product_directories: tuple[Path, Path],
) -> None:
    published, drafts = product_directories
    store = ProductConfigStore(drafts, published)
    first_draft = store.save_draft(
        "logic-chip",
        ProductRecipe.model_validate(_valid_recipe()),
    )
    first = store.publish("logic-chip")
    first_path = published / "logic-chip/v1.json"
    first_bytes = first_path.read_bytes()

    updated = first_draft.model_copy(update={"description": "Second version"})
    store.save_draft("logic-chip", updated)
    second = store.publish("logic-chip")

    assert first.product_version == 1
    assert second.product_version == 2
    assert first_path.read_bytes() == first_bytes
    assert (published / "logic-chip/v2.json").is_file()


def test_published_path_cannot_escape_through_symlink(
    tmp_path: Path,
) -> None:
    published = tmp_path / "published"
    drafts = tmp_path / "drafts"
    outside = tmp_path / "outside"
    published.mkdir()
    outside.mkdir()
    (published / "logic-chip").symlink_to(outside, target_is_directory=True)
    store = ProductConfigStore(drafts, published)
    store.save_draft(
        "logic-chip",
        ProductRecipe.model_validate(_valid_recipe()),
    )

    with pytest.raises(ProductStorageError, match="escapes"):
        store.publish("logic-chip")
    assert list(outside.iterdir()) == []


def test_product_api_requires_local_editor_marker(
    product_client: ASGITestClient,
) -> None:
    response = product_client.put(
        "/api/v1/product-configs/logic-chip/draft",
        json=_valid_recipe(),
    )

    assert response.status_code == 403
    assert "request marker" in response.json()["detail"]


def test_product_api_saves_validates_and_publishes(
    product_client: ASGITestClient,
    product_directories: tuple[Path, Path],
) -> None:
    save = product_client.put(
        "/api/v1/product-configs/logic-chip/draft",
        json=_valid_recipe(),
        headers=EDITOR_HEADERS,
    )
    validation = product_client.post(
        "/api/v1/product-configs/logic-chip/validate",
        json=_valid_recipe(),
    )
    publish = product_client.post(
        "/api/v1/product-configs/logic-chip/publish",
        headers=EDITOR_HEADERS,
    )

    assert save.status_code == 200
    assert save.json()["recipe"]["status"] == "draft"
    assert validation.status_code == 200
    assert validation.json() == {"valid": True, "issues": []}
    assert publish.status_code == 200
    assert publish.json()["recipe"]["status"] == "published"
    assert publish.json()["recipe"]["product_version"] == 1
    published, drafts = product_directories
    assert not (drafts / "logic-chip.json").exists()
    assert (published / "logic-chip/v1.json").is_file()


def test_product_api_rejects_path_identity_mismatch(
    product_client: ASGITestClient,
) -> None:
    response = product_client.put(
        "/api/v1/product-configs/different-product/draft",
        json=_valid_recipe(),
        headers=EDITOR_HEADERS,
    )

    assert response.status_code == 400
    assert "does not match" in response.json()["detail"]


def test_published_version_can_start_a_new_draft(
    product_client: ASGITestClient,
) -> None:
    product_client.put(
        "/api/v1/product-configs/logic-chip/draft",
        json=_valid_recipe(),
        headers=EDITOR_HEADERS,
    )
    product_client.post(
        "/api/v1/product-configs/logic-chip/publish",
        headers=EDITOR_HEADERS,
    )

    response = product_client.post(
        "/api/v1/product-configs/logic-chip/versions/1/draft",
        headers=EDITOR_HEADERS,
    )

    assert response.status_code == 200
    recipe = response.json()["recipe"]
    assert recipe["status"] == "draft"
    assert recipe["product_version"] is None


def test_product_pages_render_designer_and_pinned_graph_library(
    product_client: ASGITestClient,
) -> None:
    catalog = product_client.get("/products")
    editor = product_client.get("/products/new")

    assert catalog.status_code == 200
    assert "Design the recipe." in catalog.text
    assert editor.status_code == 200
    assert "Product Designer v1" in editor.text
    assert "cytoscape@3.34.3" in editor.text
    assert 'id="product-graph"' in editor.text
    assert 'id="editor-action-status"' in editor.text
    assert "Flat recipe JSON" in editor.text


def test_editor_tracks_property_values_continuously() -> None:
    script = (
        Path(__file__).parents[1]
        / "src/fabtrace/web/static/product-editor.js"
    ).read_text(encoding="utf-8")

    assert 'input.addEventListener("input", updateNode)' in script
    assert 'input.addEventListener("change", updateNode, { once: true })' not in script
    assert "/api/v1/source/capabilities/" in script
    assert "Refresh providers from ETO" in script
    assert 'id="node-lead-time"' not in script
    assert "Material lead time in days" not in script
    assert 'node.kind === "material" && !(Number(node.lead_time_days) > 0)' in script
    assert 'const selectedNodes = selected.nodes();' in script
    assert 'delete recipe.nodes[node.id()];' in script
    assert 'if (selected.isNode()) delete recipe.nodes[selected.id()];' not in script


def test_source_api_detects_organization_provider_candidates(
    product_client: ASGITestClient,
) -> None:
    response = product_client.get(
        "/api/v1/source/capabilities/N26/providers"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["capability_id"] == "N26"
    assert payload["capability_name"] == "Wafer"
    assert payload["count"] == 13
    provider_ids = [item["provider_id"] for item in payload["candidates"]]
    assert provider_ids == sorted(provider_ids)
    assert "P208" in provider_ids
    assert "P226" in provider_ids
    assert "P7" not in provider_ids
    p226 = next(
        item for item in payload["candidates"] if item["provider_id"] == "P226"
    )
    assert p226["provider_name"] == "GlobalWafers"
    assert p226["country_code"] == "TWN"
    assert p226["share_provided"] is None


def test_source_api_rejects_unknown_capability(
    product_client: ASGITestClient,
) -> None:
    response = product_client.get(
        "/api/v1/source/capabilities/N-DOES-NOT-EXIST/providers"
    )

    assert response.status_code == 404
    assert "does not exist" in response.json()["detail"]
