"""The structure inside the API layer, and two rules that cover the whole tree (ADR 0013).

tests/unit/test_layering.py decides which layer may import which. These tests go one level further
down and read the source with the ast module, so they need no database:

1. Inside `ledger/api/rest/`, each part may import only what its job needs. Schemas are the wire
   contract: they hold the request and response shapes and the code that converts between them and
   the domain models, so they may import models but never services, infrastructure, routers or
   handlers. The problem table (`problems.py`) has no web framework in it. Middleware depends on
   infrastructure only. The error handlers use the table, the middleware and infrastructure.
   Routers use schemas, the request hash, the cursor codec, services, models and infrastructure,
   and never each other. The aggregate router (`router.py`) knows the routers and nothing else. A
   module under `rest/` that is not in `PART_RULES` fails the test: a new module has to say where it
   belongs.

2. File names say what a file is: `_model.py` in models, `_repo.py` in repository, `_service.py` in
   service, `_schema.py` in schemas. The suffixes keep names unique across layers.

3. The web frameworks (fastapi, starlette, pydantic) are imported only by the API layer and the
   entry points. Nothing else in the ledger knows what carries a request.

What the code in the router modules may do, and what main.py may do, is checked in
tests/unit/test_router_rules.py.
"""

import ast
from pathlib import Path

import pytest

from tests.unit.layering_scan import (
    SRC,
    build_tree,
    imported_names,
    layer_of,
    module_name,
)

# --- 1. the parts of the REST package ---------------------------------------------------------

# What each part of ledger/api/rest may import from inside ledger, by part (for modules in the REST
# package) or by layer (for everything else). A part may always import itself.
PART_RULES = {
    "schemas": {"models", "errors", "cursor"},
    "problems": {"errors"},
    "idempotency": set(),
    "cursor": {"errors", "schemas"},
    "dependencies": {"schemas"},
    "middleware": {"infra"},
    "error_handlers": {"problems", "middleware", "infra", "errors"},
    "routers": {
        "schemas",
        "idempotency",
        "cursor",
        "dependencies",
        "service",
        "models",
        "infra",
        "errors",
    },
    "router": {"routers"},
}

# Third-party modules a part must not import (matched by prefix).
PART_FORBIDDEN = {
    "schemas": ("fastapi", "starlette"),
    "problems": ("fastapi", "starlette", "pydantic"),
    "idempotency": ("fastapi", "starlette"),
    "cursor": ("fastapi", "starlette"),
    "middleware": ("fastapi",),
}


def rest_part(dotted: str) -> str | None:
    """The part of the REST package a module belongs to: `schemas` for
    `ledger.api.rest.schemas.types`, `problems` for `ledger.api.rest.problems`, and so on. None for
    a module outside the package."""
    parts = dotted.split(".")
    if parts[:3] != ["ledger", "api", "rest"]:
        return None
    return "rest" if len(parts) == 3 else parts[3]


# Parts whose modules must not import one another. Routers stay independent: code two routers
# share goes in a neutral module (`dependencies`, `idempotency`, a schema), not in one router for
# the other to import. Schema modules, by contrast, build on each other (every schema uses `types`).
NO_SIBLINGS = {"routers"}


def is_other_module(imported: str, own_name: str) -> bool:
    """True for a submodule of the part (`ledger.api.rest.routers.transfers`) that is not the module
    doing the importing. The bare package (`ledger.api.rest.routers`) is not a module of its own."""
    return imported != own_name and imported.count(".") >= 4


def rest_violations(root: Path) -> list[str]:
    problems: list[str] = []
    for path in sorted(root.rglob("*.py")):
        name = module_name(root, path)
        part = rest_part(name)
        if part is None or part == "rest":
            continue
        if part not in PART_RULES:
            problems.append(f"{name}: unknown part '{part}': register it in PART_RULES")
            continue
        for imported in sorted(imported_names(ast.parse(path.read_text(encoding="utf-8")))):
            target = rest_part(imported) or layer_of(imported)
            if target not in (None, "rest", part) and target not in PART_RULES[part]:
                problems.append(f"{name} ({part}) imports {imported} ({target})")
            if part in NO_SIBLINGS and target == part and is_other_module(imported, name):
                problems.append(f"{name} ({part}) imports its sibling {imported}")
            for prefix in PART_FORBIDDEN.get(part, ()):
                if imported == prefix or imported.startswith(prefix + "."):
                    problems.append(f"{name} ({part}) imports {imported}")
                    break
    return problems


def test_the_parts_of_the_rest_package_import_only_what_their_job_needs() -> None:
    assert rest_violations(SRC) == []


# --- 2. file names ----------------------------------------------------------------------------

SUFFIXES = {
    "models": ("_model.py", set()),
    "repository": ("_repo.py", set()),
    "service": ("_service.py", {"decorators.py"}),
    "api/rest/schemas": ("_schema.py", {"types.py"}),
}


def suffix_violations(root: Path) -> list[str]:
    problems: list[str] = []
    for folder, (suffix, exempt) in SUFFIXES.items():
        for path in sorted((root / folder).glob("*.py")):
            if path.name != "__init__.py" and path.name not in exempt:
                if not path.name.endswith(suffix):
                    problems.append(f"{folder}/{path.name} should end in {suffix}")
    return problems


def test_files_are_named_for_what_they_are() -> None:
    assert suffix_violations(SRC) == []


# --- 3. the web frameworks stay in the API layer ----------------------------------------------

FRAMEWORKS = ("fastapi", "starlette", "pydantic")
MAY_USE_FRAMEWORKS = {"api", "entry"}


def framework_violations(root: Path) -> list[str]:
    problems: list[str] = []
    for path in sorted(root.rglob("*.py")):
        name = module_name(root, path)
        layer = layer_of(name)
        if layer is None or layer in MAY_USE_FRAMEWORKS:
            continue
        for imported in sorted(imported_names(ast.parse(path.read_text(encoding="utf-8")))):
            if imported.split(".")[0] in FRAMEWORKS:
                problems.append(f"{name} ({layer}) imports {imported}")
    return problems


def test_the_web_frameworks_are_imported_only_by_the_api_layer_and_the_entry_points() -> None:
    assert framework_violations(SRC) == []


# --- the rules themselves must catch what they claim to catch ---------------------------------


def test_a_tree_that_follows_the_part_rules_has_no_violations(tmp_path: Path) -> None:
    root = build_tree(
        tmp_path,
        {
            "errors.py": "",
            "api/rest/schemas/types.py": "from pydantic import Field\n",
            "api/rest/schemas/account_schema.py": (
                "from pydantic import BaseModel\n"
                "from ledger.api.rest.schemas.types import X\n"
                "from ledger.models.account_model import Account\n"
            ),
            "api/rest/problems.py": (
                "from dataclasses import dataclass\nfrom ledger.errors import E\n"
            ),
            "api/rest/idempotency.py": "import hashlib\nfrom pydantic import BaseModel\n",
            "api/rest/dependencies.py": (
                "from fastapi import Header\nfrom ledger.api.rest.schemas import types\n"
            ),
            "api/rest/middleware/request_id.py": (
                "from starlette.types import Scope\nfrom ledger.infra.logging_config import v\n"
            ),
            "api/rest/error_handlers.py": (
                "from fastapi import FastAPI\n"
                "from ledger.api.rest.problems import problem_for\n"
                "from ledger.api.rest.middleware.request_id import RequestIDMiddleware\n"
                "from ledger.infra.logging_config import v\n"
                "from ledger.errors import E\n"
            ),
            "api/rest/routers/accounts.py": (
                "from fastapi import APIRouter\n"
                "from ledger.api.rest.schemas.account_schema import AccountResponse\n"
                "from ledger.api.rest.idempotency import request_hash\n"
                "from ledger.api.rest.dependencies import pool\n"
                "from ledger.service.account_service import create_account\n"
                "from ledger.models.account_model import Account\n"
                "from ledger.infra.db import run_in_transaction\n"
                "from ledger.errors import E\n"
            ),
            "api/rest/router.py": "from ledger.api.rest.routers import accounts\n",
        },
    )

    assert rest_violations(root) == []


@pytest.mark.parametrize(
    "path, source",
    [
        # schemas: the wire contract does not depend on behavior
        ("api/rest/schemas/a_schema.py", "from ledger.service.account_service import f\n"),
        ("api/rest/schemas/a_schema.py", "from ledger.infra.db import run_in_transaction\n"),
        ("api/rest/schemas/a_schema.py", "from ledger.repository import account_repo\n"),
        ("api/rest/schemas/a_schema.py", "from ledger.api.rest.problems import problem_for\n"),
        ("api/rest/schemas/a_schema.py", "from ledger.api.rest.routers import accounts\n"),
        ("api/rest/schemas/a_schema.py", "from fastapi import APIRouter\n"),
        # the problem table has no framework and no wiring
        ("api/rest/problems.py", "from fastapi import FastAPI\n"),
        ("api/rest/problems.py", "from starlette.responses import Response\n"),
        ("api/rest/problems.py", "from pydantic import BaseModel\n"),
        ("api/rest/problems.py", "from ledger.api.rest.schemas import types\n"),
        ("api/rest/problems.py", "from ledger.infra import logging_config\n"),
        # the request hash depends on nothing of ours
        ("api/rest/idempotency.py", "from ledger.models.posting_model import Entry\n"),
        ("api/rest/idempotency.py", "from fastapi import Request\n"),
        # middleware sees infrastructure only
        ("api/rest/middleware/request_id.py", "from ledger.api.rest.schemas import types\n"),
        ("api/rest/middleware/request_id.py", "from ledger.service import account_service\n"),
        ("api/rest/middleware/request_id.py", "from ledger.api.rest.problems import problem_for\n"),
        ("api/rest/middleware/request_id.py", "from fastapi import FastAPI\n"),
        # the error handlers do not reach into schemas, services or routers
        ("api/rest/error_handlers.py", "from ledger.api.rest.schemas import types\n"),
        ("api/rest/error_handlers.py", "from ledger.service import account_service\n"),
        ("api/rest/error_handlers.py", "from ledger.api.rest.routers import accounts\n"),
        ("api/rest/error_handlers.py", "from ledger.models.account_model import Account\n"),
        # routers do not reach into each other, the aggregate router, the handlers or the middleware
        ("api/rest/routers/accounts.py", "from ledger.api.rest.routers import transfers\n"),
        ("api/rest/routers/accounts.py", "from ledger.api.rest import router\n"),
        ("api/rest/routers/accounts.py", "from ledger.api.rest.error_handlers import f\n"),
        ("api/rest/routers/accounts.py", "from ledger.api.rest.middleware import request_id\n"),
        ("api/rest/routers/accounts.py", "from ledger.api.rest.problems import problem_for\n"),
        ("api/rest/routers/accounts.py", "from ledger.repository import account_repo\n"),
        # the aggregate router knows the routers and nothing else
        ("api/rest/router.py", "from ledger.api.rest.schemas import types\n"),
        ("api/rest/router.py", "from ledger.service import account_service\n"),
        # shared dependencies are small
        ("api/rest/dependencies.py", "from ledger.service import account_service\n"),
        # a module that says nothing about where it belongs
        ("api/rest/surprise.py", "x = 1\n"),
    ],
    ids=lambda value: value.strip() if isinstance(value, str) else value,
)
def test_each_kind_of_part_violation_is_caught(tmp_path: Path, path: str, source: str) -> None:
    root = build_tree(tmp_path, {"errors.py": "", path: source})

    assert rest_violations(root) != []


def test_a_tree_with_the_right_suffixes_is_clean(tmp_path: Path) -> None:
    root = build_tree(
        tmp_path,
        {
            "models/__init__.py": "",
            "models/account_model.py": "",
            "repository/account_repo.py": "",
            "service/account_service.py": "",
            "service/decorators.py": "",
            "api/rest/schemas/account_schema.py": "",
            "api/rest/schemas/types.py": "",
        },
    )

    assert suffix_violations(root) == []


@pytest.mark.parametrize(
    "path",
    [
        "models/account.py",
        "models/account_models.py",
        "repository/account.py",
        "repository/account_service.py",
        "service/account.py",
        "service/helpers.py",
        "api/rest/schemas/account.py",
        "api/rest/schemas/helpers.py",
    ],
)
def test_a_file_without_its_folders_suffix_is_caught(tmp_path: Path, path: str) -> None:
    root = build_tree(tmp_path, {path: ""})

    assert suffix_violations(root) != []


def test_a_clean_tree_keeps_the_frameworks_in_the_api_layer(tmp_path: Path) -> None:
    root = build_tree(
        tmp_path,
        {
            "main.py": "from fastapi import FastAPI\n",
            "api/rest/routers/accounts.py": (
                "from fastapi import APIRouter\nfrom pydantic import BaseModel\n"
            ),
            "api/rest/middleware/request_id.py": "from starlette.types import Scope\n",
            "service/account_service.py": "import psycopg\nfrom dataclasses import dataclass\n",
            "infra/db.py": "import psycopg_pool\n",
        },
    )

    assert framework_violations(root) == []


@pytest.mark.parametrize(
    "path, source",
    [
        ("service/account_service.py", "from pydantic import BaseModel\n"),
        ("service/account_service.py", "import fastapi\n"),
        ("repository/account_repo.py", "from starlette.requests import Request\n"),
        ("models/account_model.py", "from pydantic import BaseModel\n"),
        ("infra/config.py", "from pydantic import BaseSettings\n"),
        ("errors.py", "from fastapi import HTTPException\n"),
    ],
    ids=lambda value: value.strip() if isinstance(value, str) else value,
)
def test_a_framework_outside_the_api_layer_is_caught(
    tmp_path: Path, path: str, source: str
) -> None:
    root = build_tree(tmp_path, {path: source})

    assert framework_violations(root) != []
