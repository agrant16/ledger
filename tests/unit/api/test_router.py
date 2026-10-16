"""The aggregate router and the app's route inventory (ledger/api/rest/router.py; ADR 0013).

`router.py` is the one place that knows every router module. It includes each module's router, and
`main.py` includes only this one. Without a database, these tests check that:

- every module in `ledger/api/rest/routers/` exposes a `router` made of ordinary routes, and the
  aggregate serves every one of them, so a new router module cannot be forgotten;
- the aggregate has no routes of its own, and `main.py` defines none either: the app's own routes
  are FastAPI's documentation routes and the included aggregate, nothing else;
- the inventory of endpoints is pinned. The project's rule is that a change to the public API needs
  the author's approval, so adding or removing an endpoint has to change this list on purpose.

FastAPI includes a router lazily: the aggregate's `routes` holds an internal wrapper, not the
routes inside it. These tests therefore never read the wrapper. They ask the public question, "does
this router match a request for this path and method?", which is what Starlette's `matches` answers.
"""

import importlib
import pkgutil
import re

import pytest
from fastapi import APIRouter
from fastapi.routing import APIRoute
from starlette.routing import Match, Route

# (path, methods) of every endpoint the service offers. Update it together with the design doc's
# API table when an endpoint is added or removed.
ENDPOINTS = {
    ("/accounts", ("POST",)),
    ("/accounts/{account_id}/balance", ("GET",)),
    ("/health", ("GET",)),
    ("/transfers", ("POST",)),
    ("/transfers/{transfer_id}", ("GET",)),
}
# The endpoints that appear in the OpenAPI document (/health is for the platform, not for clients).
DOCUMENTED = {
    ("/accounts", "post"),
    ("/accounts/{account_id}/balance", "get"),
    ("/transfers", "post"),
    ("/transfers/{transfer_id}", "get"),
}
DOCUMENTATION_ROUTES = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def route_keys(routes) -> set[tuple[str, tuple[str, ...]]]:
    return {(route.path, tuple(sorted(route.methods))) for route in routes}


def serves(router_or_app, path: str, method: str) -> bool:
    """True if the router would handle a request for this path template and method."""
    concrete = re.sub(r"\{[^}]+\}", "1", path)
    scope = {
        "type": "http",
        "path": concrete,
        "raw_path": concrete.encode(),
        "root_path": "",
        "method": method,
        "headers": [],
        "query_string": b"",
    }
    routes = getattr(router_or_app, "routes", [])
    return any(route.matches(scope)[0] == Match.FULL for route in routes)


def router_modules() -> list[str]:
    try:
        package = importlib.import_module("ledger.api.rest.routers")
    except ModuleNotFoundError:  # not written yet: the tests below then fail one by one
        return []
    return [
        f"{package.__name__}.{module.name}"
        for module in pkgutil.iter_modules(package.__path__)
        if not module.name.startswith("_")
    ]


@pytest.fixture
def aggregate() -> APIRouter:
    """Imported lazily so these tests fail one by one while the module does not exist yet."""
    from ledger.api.rest.router import router

    return router


def test_the_aggregate_is_an_api_router(aggregate) -> None:
    assert isinstance(aggregate, APIRouter)


def test_there_is_at_least_one_router_module() -> None:
    assert router_modules() != []


@pytest.mark.parametrize("module", router_modules() or ["ledger.api.rest.routers.missing"])
def test_each_router_module_exposes_a_router_of_ordinary_routes_that_belong_to_an_endpoint(
    module: str,
) -> None:
    """A module's paths are relative to the prefix the aggregate gives it, so each one must be
    the end of a listed endpoint's path."""
    router = importlib.import_module(module).router

    assert isinstance(router, APIRouter)
    assert router.routes, f"{module} has a router with no routes"
    assert all(isinstance(route, APIRoute) for route in router.routes)
    for path, methods in route_keys(router.routes):
        assert any(
            endpoint_path.endswith(path) and methods == endpoint_methods
            for endpoint_path, endpoint_methods in ENDPOINTS
        ), f"{module}: {methods} {path!r} matches no listed endpoint"


def test_the_aggregate_serves_every_endpoint(aggregate) -> None:
    for path, methods in ENDPOINTS:
        for method in methods:
            assert serves(aggregate, path, method), f"the aggregate does not serve {method} {path}"


def test_the_aggregate_has_no_routes_of_its_own(aggregate) -> None:
    assert [route for route in aggregate.routes if isinstance(route, APIRoute)] == []


def test_the_router_modules_hold_exactly_the_listed_endpoints() -> None:
    """One route per listed (path, method), none extra, none missing. The prefix is the aggregate's,
    so the count is compared here and the full paths are checked by the serving tests."""
    routes = [
        (route.path, method)
        for module in router_modules()
        for route in importlib.import_module(module).router.routes
        for method in route.methods
    ]

    assert len(routes) == sum(len(methods) for _, methods in ENDPOINTS)


def test_the_app_defines_no_route_itself_and_serves_every_endpoint() -> None:
    from ledger.main import app

    assert [route for route in app.routes if isinstance(route, APIRoute)] == []
    for path, methods in ENDPOINTS:
        for method in methods:
            assert serves(app.router, path, method), f"the app does not serve {method} {path}"


def test_the_apps_only_plain_routes_are_the_documentation_routes() -> None:
    from ledger.main import app

    plain = {route.path for route in app.routes if isinstance(route, Route)}

    assert plain <= DOCUMENTATION_ROUTES


def test_the_documented_endpoints_are_the_ones_in_the_openapi_document() -> None:
    from ledger.main import app

    paths = app.openapi()["paths"]
    found = {(path, method) for path, item in paths.items() for method in item}

    assert found == DOCUMENTED
