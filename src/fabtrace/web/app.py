from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from fabtrace import __version__
from fabtrace.planning import PlanArtifactStore
from fabtrace.products import ProductConfigStore
from fabtrace.web.models import (
    CustomerOrderDetailResponse,
    CustomerOrderListResponse,
    HealthResponse,
    OrderDetailResponse,
    OrderListResponse,
    ProviderListResponse,
)
from fabtrace.web.repository import (
    CustomerOrderAmbiguousError,
    CustomerOrderNotFoundError,
    DashboardDataError,
    DashboardRepository,
    OrderNotFoundError,
)
from fabtrace.web.product_routes import attach_product_routes
from fabtrace.web.plan_routes import attach_plan_routes
from fabtrace.web.source_routes import attach_source_routes

WEB_ROOT = Path(__file__).resolve().parent
DEFAULT_DATABASE = Path(".fabtrace/state/fabtrace.db")
DEFAULT_PRODUCT_CONFIG_DIRECTORY = Path(".fabtrace/config/products")
DEFAULT_PRODUCT_DRAFT_DIRECTORY = Path(".fabtrace/drafts/products")
DEFAULT_PLAN_DIRECTORY = Path(".fabtrace/plans")
DEFAULT_SOURCE_DATA_DIRECTORY = Path("Data")


def create_app(
    database_path: Path | None = None,
    product_config_directory: Path | None = None,
    product_draft_directory: Path | None = None,
    plan_directory: Path | None = None,
    data_directory: Path | None = None,
) -> FastAPI:
    resolved_database = database_path or Path(
        os.environ.get("FABTRACE_DATABASE", DEFAULT_DATABASE)
    )
    repository = DashboardRepository(resolved_database)
    product_store = ProductConfigStore(
        product_draft_directory or DEFAULT_PRODUCT_DRAFT_DIRECTORY,
        product_config_directory or DEFAULT_PRODUCT_CONFIG_DIRECTORY,
    )
    plan_store = PlanArtifactStore(plan_directory or DEFAULT_PLAN_DIRECTORY)
    resolved_data_directory = data_directory or Path(
        os.environ.get("FABTRACE_DATA_DIRECTORY", DEFAULT_SOURCE_DATA_DIRECTORY)
    )
    templates = Jinja2Templates(directory=WEB_ROOT / "templates")
    app = FastAPI(
        title="FabTrace API",
        version=__version__,
        description=(
            "Read-only operational data plus local product configuration."
        ),
    )
    app.state.repository = repository
    app.state.product_store = product_store
    app.state.plan_store = plan_store
    app.mount("/static", StaticFiles(directory=WEB_ROOT / "static"), name="static")

    def read_orders(scenario_id: str | None = None) -> list[dict[str, object]]:
        try:
            return repository.list_orders(scenario_id)
        except DashboardDataError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    def read_order(scenario_id: str, order_id: str) -> dict[str, object]:
        try:
            return repository.get_order(scenario_id, order_id)
        except OrderNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DashboardDataError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    def read_customer_orders(
        scenario_id: str | None = None,
    ) -> list[dict[str, object]]:
        try:
            return repository.list_customer_orders(scenario_id)
        except DashboardDataError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    def read_customer_order(
        order_id: str,
        scenario_id: str | None = None,
    ) -> dict[str, object]:
        try:
            return repository.get_customer_order(order_id, scenario_id)
        except CustomerOrderNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except CustomerOrderAmbiguousError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except DashboardDataError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/health", tags=["system"], response_model=HealthResponse)
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "database_mode": "read-only",
            "product_config_mode": "local-write",
        }

    @app.get(
        "/api/v1/orders",
        tags=["orders"],
        response_model=OrderListResponse,
    )
    async def api_orders(
        scenario_id: str | None = Query(default=None),
    ) -> dict[str, object]:
        orders = read_orders(scenario_id)
        return {
            "api_version": "v1",
            "scenario_id": scenario_id,
            "count": len(orders),
            "orders": orders,
        }

    @app.get(
        "/api/v1/orders/{scenario_id}/{order_id}",
        tags=["orders"],
        response_model=OrderDetailResponse,
    )
    async def api_order(scenario_id: str, order_id: str) -> dict[str, object]:
        return {"api_version": "v1", **read_order(scenario_id, order_id)}

    @app.get(
        "/api/v1/providers",
        tags=["providers"],
        response_model=ProviderListResponse,
    )
    async def api_providers(
        scenario_id: str | None = Query(default=None),
    ) -> dict[str, object]:
        try:
            providers = repository.list_providers(scenario_id)
        except DashboardDataError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {
            "api_version": "v1",
            "scenario_id": scenario_id,
            "count": len(providers),
            "providers": providers,
        }

    @app.get(
        "/api/v1/customer-orders",
        tags=["customer-orders"],
        response_model=CustomerOrderListResponse,
    )
    async def api_customer_orders(
        scenario_id: str | None = Query(default=None),
    ) -> dict[str, object]:
        customer_orders = read_customer_orders(scenario_id)
        return {
            "api_version": "v1",
            "scenario_id": scenario_id,
            "count": len(customer_orders),
            "customer_orders": customer_orders,
        }

    @app.get(
        "/api/v1/customer-orders/{order_id}",
        tags=["customer-orders"],
        response_model=CustomerOrderDetailResponse,
    )
    async def api_customer_order(
        order_id: str,
        scenario_id: str | None = Query(default=None),
    ) -> dict[str, object]:
        return {
            "api_version": "v1",
            **read_customer_order(order_id, scenario_id),
        }

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard(
        request: Request,
        scenario_id: str | None = Query(default=None),
    ) -> HTMLResponse:
        orders = read_customer_orders(scenario_id)
        try:
            scenarios = repository.list_scenarios()
        except DashboardDataError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "orders": orders,
                "scenarios": scenarios,
                "selected_scenario": scenario_id,
            },
        )

    @app.get(
        "/fragments/orders",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def order_fragment(
        request: Request,
        scenario_id: str | None = Query(default=None),
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="partials/order_table.html",
            context={"orders": read_customer_orders(scenario_id)},
        )

    @app.get("/customer-orders/{order_id}", response_class=HTMLResponse)
    async def customer_order_page(
        request: Request,
        order_id: str,
        scenario_id: str | None = Query(default=None),
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="customer_order_detail.html",
            context=read_customer_order(order_id, scenario_id),
        )

    @app.get("/orders/{scenario_id}/{order_id}", response_class=HTMLResponse)
    async def order_page(
        request: Request,
        scenario_id: str,
        order_id: str,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="order_detail.html",
            context=read_order(scenario_id, order_id),
        )

    @app.get("/providers", response_class=HTMLResponse)
    async def provider_page(
        request: Request,
        scenario_id: str | None = Query(default=None),
    ) -> HTMLResponse:
        try:
            providers = repository.list_providers(scenario_id)
            scenarios = repository.list_scenarios()
        except DashboardDataError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return templates.TemplateResponse(
            request=request,
            name="providers.html",
            context={
                "providers": providers,
                "scenarios": scenarios,
                "selected_scenario": scenario_id,
            },
        )

    attach_product_routes(app, product_store, templates)
    attach_plan_routes(app, product_store, plan_store, templates)
    attach_source_routes(app, resolved_data_directory)

    return app


app = create_app()
