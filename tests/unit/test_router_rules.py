"""The shape of the router modules and of main.py (ADR 0004, ADR 0011, ADR 0013).

tests/unit/test_api_structure.py decides what the parts of the API layer may import. These rules
look at what the code in the router modules is allowed to do, and read the source with the ast
module, so they need no database:

1. Every module in `ledger/api/rest/routers/` defines a module-level `router`, which is how the
   aggregate router includes it.
2. Route functions are synchronous. A blocking database call inside an `async def` would stall the
   event loop for every request (ADR 0004), so `async def` and `await` do not appear in a router
   module.
3. A route function calls `run_in_transaction` at most once: one request, one transaction
   (ADR 0011).
4. A route calls a service function only inside the function it hands to `run_in_transaction`,
   never directly in its own body, and never without a `run_in_transaction` to put it in. The API
   layer owns the transaction; the service is always given an open connection.
5. main.py reaches into the API layer for the aggregate router and the error handlers only, and
   defines no route itself.
"""

import ast
from pathlib import Path

import pytest

from tests.unit.layering_scan import SRC, build_tree, imported_names, module_name

ROUTERS = "api/rest/routers"
TRANSACTION_HELPER = "run_in_transaction"
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "route", "api_route"}
MAIN_MAY_USE_FROM_THE_API = {"router", "error_handlers"}


def service_names(tree: ast.Module) -> set[str]:
    """Names bound by importing from ledger.service: functions, or whole service modules."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module == "ledger.service" or node.module.startswith("ledger.service."):
                names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("ledger.service."):
                    names.add(alias.asname or alias.name.split(".")[0])
    return names


def calls_in(function: ast.AST, include_nested: bool) -> list[ast.Call]:
    """The calls in a function. A call inside a lambda or an inner def counts as nested."""
    found: list[ast.Call] = []

    def visit(node: ast.AST, nested: bool) -> None:
        for child in ast.iter_child_nodes(node):
            child_nested = nested or isinstance(
                child, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef)
            )
            if isinstance(child, ast.Call) and (include_nested or not child_nested):
                found.append(child)
            visit(child, child_nested)

    visit(function, False)
    return found


def is_service_call(call: ast.Call, names: set[str]) -> bool:
    target = call.func
    if isinstance(target, ast.Name):
        return target.id in names
    return (
        isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id in names
    )


def is_transaction_call(call: ast.Call) -> bool:
    return isinstance(call.func, ast.Name) and call.func.id == TRANSACTION_HELPER


def router_violations(root: Path) -> list[str]:
    problems: list[str] = []
    for path in sorted((root / ROUTERS).glob("*.py")):
        if path.name == "__init__.py":
            continue
        name = module_name(root, path)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        defines_router = any(
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "router" for target in node.targets
            )
            for node in tree.body
        )
        if not defines_router:
            problems.append(f"{name} defines no module-level `router`")
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.Await)):
                problems.append(
                    f"{name} line {node.lineno}: routes are synchronous (no async/await)"
                )
        services = service_names(tree)
        for function in tree.body:
            if not isinstance(function, ast.FunctionDef):
                continue
            where = f"{name}.{function.name}"
            transactions = [c for c in calls_in(function, True) if is_transaction_call(c)]
            if len(transactions) > 1:
                problems.append(f"{where} calls {TRANSACTION_HELPER} {len(transactions)} times")
            direct = [c for c in calls_in(function, False) if is_service_call(c, services)]
            if direct:
                problems.append(f"{where} calls a service directly, outside {TRANSACTION_HELPER}")
            inside = [c for c in calls_in(function, True) if is_service_call(c, services)]
            if inside and not transactions:
                problems.append(f"{where} calls a service without {TRANSACTION_HELPER}")
    return problems


def main_violations(root: Path) -> list[str]:
    problems: list[str] = []
    path = root / "main.py"
    if not path.exists():
        return problems
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for imported in sorted(imported_names(tree)):
        parts = imported.split(".")
        if parts[:2] == ["ledger", "api"] and len(parts) > 3 and parts[2] == "rest":
            if parts[3] not in MAIN_MAY_USE_FROM_THE_API:
                problems.append(f"main imports {imported}: only the aggregate router and handlers")
        elif parts[:2] == ["ledger", "api"] and len(parts) > 2 and parts[2] != "rest":
            problems.append(f"main imports {imported}: the API layer lives in api/rest")
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                call = decorator.func if isinstance(decorator, ast.Call) else decorator
                if (
                    isinstance(call, ast.Attribute)
                    and call.attr in HTTP_METHODS
                    and isinstance(call.value, ast.Name)
                    and call.value.id == "app"
                ):
                    problems.append(f"main defines a route: {node.name}")
    return problems


# --- the real tree ----------------------------------------------------------------------------


def test_the_router_modules_follow_the_shape_rules() -> None:
    assert router_violations(SRC) == []


def test_main_reaches_into_the_api_layer_only_for_the_router_and_the_handlers() -> None:
    assert main_violations(SRC) == []


# --- the rules themselves must catch what they claim to catch ---------------------------------

GOOD_ROUTER = """
from fastapi import APIRouter, Request
from ledger.infra.db import run_in_transaction
from ledger.service.account_service import create_account, get_balance
from ledger.service import posting_service

router = APIRouter()


@router.post("")
def create(body, request: Request):
    account = run_in_transaction(
        request.app.state.pool, lambda conn: create_account(conn, body.name)
    )
    return account


@router.get("/x")
def read(request: Request):
    def work(conn):
        return posting_service.post_transaction(conn, 1, [])

    return run_in_transaction(request.app.state.pool, work, read_only=True)


@router.get("/ping")
def ping(request: Request):
    return {"ok": True}
"""


def test_a_router_that_follows_the_rules_is_clean(tmp_path: Path) -> None:
    root = build_tree(tmp_path, {f"{ROUTERS}/accounts.py": GOOD_ROUTER})

    assert router_violations(root) == []


@pytest.mark.parametrize(
    "source",
    [
        # no module-level router
        "def f():\n    return 1\n",
        # async routes
        "router = 1\n\n\nasync def f():\n    return 1\n",
        "router = 1\n\n\ndef f():\n    async def g():\n        await h()\n",
        # two transactions in one route
        "router = 1\nfrom ledger.infra.db import run_in_transaction\n\n\n"
        "def f(pool):\n    run_in_transaction(pool, lambda c: 1)\n"
        "    run_in_transaction(pool, lambda c: 2)\n",
        # a service called straight from the route body
        "router = 1\nfrom ledger.service.account_service import create_account\n\n\n"
        "def f(conn):\n    return create_account(conn, 'x', 'USD', False)\n",
        # a service module's function called straight from the route body
        "router = 1\nfrom ledger.service import account_service\n\n\n"
        "def f(conn):\n    return account_service.create_account(conn, 'x', 'USD', False)\n",
        # a service called from inside a function that is never handed to a transaction
        "router = 1\nfrom ledger.service.account_service import create_account\n\n\n"
        "def f():\n    return lambda conn: create_account(conn, 'x', 'USD', False)\n",
        # a transaction, and a service called outside it
        "router = 1\nfrom ledger.infra.db import run_in_transaction\n"
        "from ledger.service.account_service import create_account\n\n\n"
        "def f(pool, conn):\n    create_account(conn, 'x', 'USD', False)\n"
        "    return run_in_transaction(pool, lambda c: create_account(c, 'y', 'USD', False))\n",
        # the transaction called inside a nested function twice
        "router = 1\nfrom ledger.infra.db import run_in_transaction\n\n\n"
        "def f(pool):\n    def a():\n        return run_in_transaction(pool, lambda c: 1)\n"
        "    return a(), run_in_transaction(pool, lambda c: 2)\n",
    ],
    ids=[
        "no router",
        "async def",
        "await in a nested async def",
        "two transactions",
        "direct service call",
        "direct service module call",
        "service in a function never given to a transaction",
        "a service call outside the transaction",
        "a nested transaction call and another",
    ],
)
def test_each_kind_of_router_violation_is_caught(tmp_path: Path, source: str) -> None:
    root = build_tree(tmp_path, {f"{ROUTERS}/accounts.py": source})

    assert router_violations(root) != []


def test_other_folders_are_not_held_to_the_router_rules(tmp_path: Path) -> None:
    """The handlers and middleware are async by nature, and services call each other freely."""
    root = build_tree(
        tmp_path,
        {
            "api/rest/error_handlers.py": "async def handler(request, exc):\n    return 1\n",
            "api/rest/middleware/request_id.py": (
                "class M:\n    async def __call__(self):\n        pass\n"
            ),
        },
    )

    assert router_violations(root) == []


def test_a_main_that_includes_the_router_and_installs_the_handlers_is_clean(
    tmp_path: Path,
) -> None:
    root = build_tree(
        tmp_path,
        {
            "main.py": (
                "from fastapi import FastAPI\n"
                "from ledger.api.rest.error_handlers import install_error_handling\n"
                "from ledger.api.rest.router import router\n"
                "from ledger.infra.db import create_pool\n"
                "app = FastAPI()\napp.include_router(router)\n"
            ),
        },
    )

    assert main_violations(root) == []


@pytest.mark.parametrize(
    "source",
    [
        "from ledger.api.rest.routers import accounts\n",
        "from ledger.api.rest.routers.accounts import router\n",
        "from ledger.api.rest.schemas import types\n",
        "from ledger.api.rest.middleware.request_id import RequestIDMiddleware\n",
        "from ledger.api.rest.problems import problem_for\n",
        "from ledger.api import accounts\n",
        "from ledger.api.accounts import router\n",
        "from fastapi import FastAPI\napp = FastAPI()\n\n\n@app.get('/x')\n"
        "def x():\n    return 1\n",
        "from fastapi import FastAPI\napp = FastAPI()\n\n\n@app.post('/x', status_code=201)\n"
        "async def x():\n    return 1\n",
    ],
    ids=[
        "a routers module",
        "a router from the routers package",
        "a schema",
        "the middleware",
        "the problem table",
        "the old api package module",
        "the old accounts router",
        "a route defined in main",
        "an async route with arguments defined in main",
    ],
)
def test_each_kind_of_main_violation_is_caught(tmp_path: Path, source: str) -> None:
    root = build_tree(tmp_path, {"main.py": source})

    assert main_violations(root) != []
