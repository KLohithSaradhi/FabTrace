from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from fabtrace.products.models import (
    ProductListResponse,
    ProductRecipe,
    RecipeResponse,
    ValidationResponse,
)
from fabtrace.products.storage import (
    ProductConfigStore,
    ProductNotFoundError,
    ProductPublicationError,
    ProductStorageError,
)
from fabtrace.products.validation import validate_for_publish

LOCAL_CLIENTS = {"127.0.0.1", "::1", "testclient"}


def require_local_mutation(request: Request) -> None:
    client_host = request.client.host if request.client else ""
    if client_host not in LOCAL_CLIENTS:
        raise HTTPException(
            status_code=403,
            detail="product configuration writes are local-only",
        )
    if request.headers.get("X-FabTrace-Editor") != "1":
        raise HTTPException(
            status_code=403,
            detail="missing product editor request marker",
        )


def attach_product_routes(
    app: FastAPI,
    store: ProductConfigStore,
    templates: Jinja2Templates,
) -> None:
    def recipe_response(recipe: ProductRecipe) -> dict[str, object]:
        return {"api_version": "v1", "recipe": recipe}

    @app.get("/products", response_class=HTMLResponse)
    async def product_list_page(request: Request) -> HTMLResponse:
        try:
            products = store.list_products()
        except ProductStorageError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return templates.TemplateResponse(
            request=request,
            name="products.html",
            context={"products": products},
        )

    @app.get("/products/new", response_class=HTMLResponse)
    async def new_product_page(request: Request) -> HTMLResponse:
        recipe = ProductRecipe(
            product_id="new-product",
            product_name="New product",
        )
        return templates.TemplateResponse(
            request=request,
            name="product_editor.html",
            context={
                "recipe": recipe,
                "recipe_json": recipe.model_dump(mode="json"),
                "is_new": True,
            },
        )

    @app.get("/products/{product_id}/edit", response_class=HTMLResponse)
    async def edit_product_page(
        request: Request,
        product_id: str,
    ) -> HTMLResponse:
        try:
            try:
                recipe = store.get_draft(product_id)
            except ProductNotFoundError:
                recipe = store.latest(product_id)
        except ProductNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ProductStorageError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return templates.TemplateResponse(
            request=request,
            name="product_editor.html",
            context={
                "recipe": recipe,
                "recipe_json": recipe.model_dump(mode="json"),
                "is_new": False,
            },
        )

    @app.get(
        "/api/v1/product-configs",
        response_model=ProductListResponse,
        tags=["product configuration"],
    )
    async def list_product_configs() -> dict[str, object]:
        try:
            products = store.list_products()
        except ProductStorageError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {
            "api_version": "v1",
            "count": len(products),
            "products": products,
        }

    @app.get(
        "/api/v1/product-configs/{product_id}/draft",
        response_model=RecipeResponse,
        tags=["product configuration"],
    )
    async def get_product_draft(product_id: str) -> dict[str, object]:
        try:
            return recipe_response(store.get_draft(product_id))
        except ProductNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ProductStorageError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.put(
        "/api/v1/product-configs/{product_id}/draft",
        response_model=RecipeResponse,
        tags=["product configuration"],
    )
    async def save_product_draft(
        request: Request,
        product_id: str,
        recipe: ProductRecipe,
    ) -> dict[str, object]:
        require_local_mutation(request)
        try:
            return recipe_response(store.save_draft(product_id, recipe))
        except ProductStorageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post(
        "/api/v1/product-configs/{product_id}/validate",
        response_model=ValidationResponse,
        tags=["product configuration"],
    )
    async def validate_product(
        product_id: str,
        recipe: ProductRecipe,
    ) -> ValidationResponse:
        if product_id != recipe.product_id:
            raise HTTPException(
                status_code=400,
                detail="path product ID does not match recipe product ID",
            )
        issues = validate_for_publish(recipe)
        return ValidationResponse(valid=not issues, issues=issues)

    @app.post(
        "/api/v1/product-configs/{product_id}/publish",
        response_model=RecipeResponse,
        tags=["product configuration"],
    )
    async def publish_product(
        request: Request,
        product_id: str,
    ) -> dict[str, object]:
        require_local_mutation(request)
        try:
            return recipe_response(store.publish(product_id))
        except ProductNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ProductPublicationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ProductStorageError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get(
        "/api/v1/product-configs/{product_id}/versions/{version}",
        response_model=RecipeResponse,
        tags=["product configuration"],
    )
    async def get_product_version(
        product_id: str,
        version: int,
    ) -> dict[str, object]:
        try:
            return recipe_response(store.get_version(product_id, version))
        except ProductNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ProductStorageError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post(
        "/api/v1/product-configs/{product_id}/versions/{version}/draft",
        response_model=RecipeResponse,
        tags=["product configuration"],
    )
    async def draft_from_product_version(
        request: Request,
        product_id: str,
        version: int,
    ) -> dict[str, object]:
        require_local_mutation(request)
        try:
            return recipe_response(store.start_draft(product_id, version))
        except ProductNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ProductStorageError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
