from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from fabtrace.planning import (
    PlanArtifactStore,
    PlanOrderRequest,
    PlanningConflictError,
    PlanningError,
    plan_order,
)
from fabtrace.planning.models import PlanListResponse, PlanResponse
from fabtrace.planning.storage import PlanNotFoundError, PlanStorageError
from fabtrace.products.storage import (
    ProductConfigStore,
    ProductNotFoundError,
    ProductStorageError,
)
from fabtrace.web.product_routes import require_local_mutation


def attach_plan_routes(
    app: FastAPI,
    product_store: ProductConfigStore,
    plan_store: PlanArtifactStore,
    templates: Jinja2Templates,
) -> None:
    @app.get("/plans", response_class=HTMLResponse)
    async def plan_list_page(request: Request) -> HTMLResponse:
        try:
            plans = plan_store.list_plans()
        except PlanStorageError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return templates.TemplateResponse(
            request=request,
            name="plans.html",
            context={"plans": plans},
        )

    @app.get("/plans/new", response_class=HTMLResponse)
    async def new_plan_page(
        request: Request,
        product_id: str = Query(...),
        product_version: int = Query(..., ge=1),
    ) -> HTMLResponse:
        try:
            recipe = product_store.get_version(product_id, product_version)
        except ProductNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ProductStorageError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return templates.TemplateResponse(
            request=request,
            name="plan_order.html",
            context={"recipe": recipe},
        )

    @app.get("/plans/{order_id}", response_class=HTMLResponse)
    async def plan_detail_page(
        request: Request,
        order_id: str,
    ) -> HTMLResponse:
        try:
            plan = plan_store.load(order_id)
        except PlanNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PlanStorageError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return templates.TemplateResponse(
            request=request,
            name="plan_detail.html",
            context={"plan": plan},
        )

    @app.get(
        "/api/v1/plans",
        response_model=PlanListResponse,
        tags=["planning"],
    )
    async def list_plans() -> dict[str, object]:
        try:
            plans = plan_store.list_plans()
        except PlanStorageError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"api_version": "v1", "count": len(plans), "plans": plans}

    @app.post(
        "/api/v1/plans",
        response_model=PlanResponse,
        status_code=201,
        tags=["planning"],
    )
    async def create_plan(
        request: Request,
        order_request: PlanOrderRequest,
    ) -> dict[str, object]:
        require_local_mutation(request)
        try:
            result = plan_order(product_store, plan_store, order_request)
        except PlanningConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PlanningError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"api_version": "v1", "plan": result.plan}

    @app.get(
        "/api/v1/plans/{order_id}",
        response_model=PlanResponse,
        tags=["planning"],
    )
    async def get_plan(order_id: str) -> dict[str, object]:
        try:
            plan = plan_store.load(order_id)
        except PlanNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PlanStorageError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"api_version": "v1", "plan": plan}
