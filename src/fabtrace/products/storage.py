from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from fabtrace.products.models import (
    ProductRecipe,
    ProductSummary,
    SAFE_ID_PATTERN,
)
from fabtrace.products.validation import validate_for_publish


class ProductStorageError(RuntimeError):
    """Raised when product configuration cannot be stored safely."""


class ProductNotFoundError(ProductStorageError):
    """Raised when a requested draft or published product does not exist."""


class ProductPublicationError(ProductStorageError):
    """Raised when an invalid recipe is published."""


def _now() -> datetime:
    return datetime.now(UTC)


class ProductConfigStore:
    def __init__(self, draft_directory: Path, config_directory: Path) -> None:
        self.draft_directory = Path(draft_directory).resolve()
        self.config_directory = Path(config_directory).resolve()

    @staticmethod
    def _check_id(product_id: str) -> None:
        if re.fullmatch(SAFE_ID_PATTERN, product_id) is None:
            raise ProductStorageError(f"invalid product ID: {product_id!r}")

    @staticmethod
    def _ensure_within(path: Path, root: Path) -> Path:
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise ProductStorageError(
                f"product configuration path escapes its storage root: {path}"
            )
        return path

    @staticmethod
    def _read(path: Path) -> ProductRecipe:
        try:
            return ProductRecipe.model_validate_json(path.read_text("utf-8"))
        except (OSError, ValidationError) as exc:
            raise ProductStorageError(
                f"cannot read product configuration {path}: {exc}"
            ) from exc

    @staticmethod
    def _serialize(recipe: ProductRecipe) -> str:
        return json.dumps(
            recipe.model_dump(mode="json"),
            indent=2,
            sort_keys=True,
        ) + "\n"

    def _draft_path(self, product_id: str) -> Path:
        self._check_id(product_id)
        return self._ensure_within(
            self.draft_directory / f"{product_id}.json",
            self.draft_directory,
        )

    def _product_directory(self, product_id: str) -> Path:
        self._check_id(product_id)
        return self._ensure_within(
            self.config_directory / product_id,
            self.config_directory,
        )

    def _version_path(self, product_id: str, version: int) -> Path:
        self._check_id(product_id)
        if version < 1:
            raise ProductStorageError("product version must be positive")
        return self._ensure_within(
            self._product_directory(product_id) / f"v{version}.json",
            self.config_directory,
        )

    def get_draft(self, product_id: str) -> ProductRecipe:
        path = self._draft_path(product_id)
        if not path.is_file():
            raise ProductNotFoundError(f"draft does not exist: {product_id}")
        return self._read(path)

    def save_draft(self, product_id: str, recipe: ProductRecipe) -> ProductRecipe:
        if recipe.product_id != product_id:
            raise ProductStorageError(
                "path product ID does not match recipe product ID"
            )
        normalized = recipe.model_copy(
            update={
                "status": "draft",
                "product_version": None,
                "updated_at": _now(),
            }
        )
        path = self._draft_path(product_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        try:
            temporary.write_text(self._serialize(normalized), encoding="utf-8")
            temporary.replace(path)
        except OSError as exc:
            raise ProductStorageError(f"cannot save product draft: {exc}") from exc
        return normalized

    def versions(self, product_id: str) -> list[int]:
        directory = self._product_directory(product_id)
        if not directory.is_dir():
            return []
        versions: list[int] = []
        for path in directory.glob("v*.json"):
            match = re.fullmatch(r"v([1-9][0-9]*)\.json", path.name)
            if match:
                versions.append(int(match.group(1)))
        return sorted(versions)

    def get_version(self, product_id: str, version: int) -> ProductRecipe:
        path = self._version_path(product_id, version)
        if not path.is_file():
            raise ProductNotFoundError(
                f"published product does not exist: {product_id} v{version}"
            )
        return self._read(path)

    def version_sha256(self, product_id: str, version: int) -> str:
        path = self._version_path(product_id, version)
        if not path.is_file():
            raise ProductNotFoundError(
                f"published product does not exist: {product_id} v{version}"
            )
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise ProductStorageError(
                f"cannot fingerprint product configuration: {exc}"
            ) from exc

    def latest(self, product_id: str) -> ProductRecipe:
        versions = self.versions(product_id)
        if not versions:
            raise ProductNotFoundError(
                f"published product does not exist: {product_id}"
            )
        return self.get_version(product_id, versions[-1])

    def publish(self, product_id: str) -> ProductRecipe:
        draft = self.get_draft(product_id)
        issues = validate_for_publish(draft)
        if issues:
            detail = "; ".join(issue.message for issue in issues)
            raise ProductPublicationError(detail)
        version = max(self.versions(product_id), default=0) + 1
        published = draft.model_copy(
            update={
                "status": "published",
                "product_version": version,
                "updated_at": _now(),
            }
        )
        path = self._version_path(product_id, version)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8") as output:
                output.write(self._serialize(published))
        except FileExistsError as exc:
            raise ProductStorageError(
                f"published version already exists: {product_id} v{version}"
            ) from exc
        except OSError as exc:
            raise ProductStorageError(f"cannot publish product: {exc}") from exc
        try:
            self._draft_path(product_id).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # Publication already succeeded and remains authoritative. Leaving
            # the recoverable draft is safer than reporting a failed publish.
            pass
        return published

    def start_draft(self, product_id: str, version: int) -> ProductRecipe:
        published = self.get_version(product_id, version)
        return self.save_draft(
            product_id,
            published.model_copy(
                update={"status": "draft", "product_version": None}
            ),
        )

    def list_products(self) -> list[ProductSummary]:
        product_ids: set[str] = set()
        if self.draft_directory.is_dir():
            product_ids.update(
                path.stem for path in self.draft_directory.glob("*.json")
            )
        if self.config_directory.is_dir():
            product_ids.update(
                path.name for path in self.config_directory.iterdir() if path.is_dir()
            )
        summaries: list[ProductSummary] = []
        for product_id in sorted(product_ids):
            try:
                draft_path = self._draft_path(product_id)
                versions = self.versions(product_id)
                if draft_path.is_file():
                    recipe = self._read(draft_path)
                elif versions:
                    recipe = self.get_version(product_id, versions[-1])
                else:
                    continue
                summaries.append(
                    ProductSummary(
                        product_id=product_id,
                        product_name=recipe.product_name,
                        has_draft=draft_path.is_file(),
                        latest_version=versions[-1] if versions else None,
                        latest_updated_at=recipe.updated_at,
                    )
                )
            except ProductStorageError:
                raise
        return summaries
