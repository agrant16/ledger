"""The layers of src/ledger and the direction of their imports (ADR 0013).

    errors, models      import nothing from ledger
    infra               errors
    repository          models, errors      (may use psycopg and psycopg.errors, never the pool)
    service             repository, models, errors   (never the pool, psycopg.errors or infra)
    api                 service, models, errors, infra   (never psycopg or the pool directly)
    main, migrate       entry points: may import any layer, and nothing imports them

A module may always import its own layer. The scan reads the source with the ast module, so it
needs no database and imports nothing.

The tree is on its way to this shape in steps, and each step tightens a ratchet in this file:
`LEGACY` lists the modules still waiting to be split into repository and service, and the root of
src/ledger may hold only the modules in `ROOT_MODULES`. A step ends when its entries are gone.
"""

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "ledger"

ENTRY_POINTS = {"main", "migrate"}
# Still to be split into repository and service functions (the next step removes this).
LEGACY = {"posting"}
ROOT_MODULES = {"__init__", "errors"} | ENTRY_POINTS | LEGACY
LAYER_PACKAGES = {"models", "repository", "service", "infra", "api"}

ALL_LAYERS = {"errors", "models", "repository", "service", "infra", "api", "legacy", "entry"}
ALLOWED = {
    "errors": set(),
    "models": set(),
    "infra": {"errors"},
    "repository": {"models", "errors"},
    "service": {"repository", "models", "errors"},
    "api": {"service", "models", "errors", "infra"},
    "legacy": {"models", "errors", "infra", "service", "legacy"},
    "entry": ALL_LAYERS,
}
# Third-party modules a layer must not import (matched by prefix). Legacy is exempt until split.
FORBIDDEN = {
    "models": ("psycopg", "psycopg_pool", "fastapi", "starlette"),
    "infra": (),
    "repository": ("psycopg_pool", "fastapi", "starlette"),
    "service": ("psycopg.errors", "psycopg_pool", "fastapi", "starlette"),
    "api": ("psycopg", "psycopg_pool"),
}

# Modules the current step creates; the scan fails until they exist.
REQUIRED_MODULES = [
    "infra/config.py",
    "infra/db.py",
    "infra/logging_config.py",
    "models/account_model.py",
    "models/posting_model.py",
    "repository/__init__.py",
    "repository/account_repo.py",
    "service/account_service.py",
    "service/decorators.py",
]

# Layers whose code must not talk to the database: the SQL lives in the repository (and in infra's
# transaction control). Legacy modules are exempt until they are split.
NO_SQL_LAYERS = {"service", "api", "models"}
DATABASE_CALLS = {"execute", "executemany", "cursor"}
SQL_STATEMENT = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|WITH)\b")


def layer_of(dotted: str) -> str | None:
    """The layer of a module named like `ledger.service.decorators`, or None if it is not one of
    ours or does not belong to a known layer (the layout test reports that)."""
    parts = dotted.split(".")
    if parts[0] != "ledger":
        return None
    if len(parts) == 1:
        return "entry"
    top = parts[1]
    if top == "errors":
        return "errors"
    if top in LAYER_PACKAGES:
        return top
    if top in ENTRY_POINTS:
        return "entry"
    if top in LEGACY:
        return "legacy"
    return None


def imported_names(tree: ast.AST) -> set[str]:
    """Every module an import statement names, with the names imported from a package also listed
    as submodules (`from psycopg import errors` counts as `psycopg.errors`)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def module_name(root: Path, path: Path) -> str:
    relative = path.relative_to(root.parent).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def import_violations(root: Path) -> list[str]:
    """Rule breaks in a package directory named `ledger`, one message each."""
    problems: list[str] = []
    for path in sorted(root.rglob("*.py")):
        name = module_name(root, path)
        layer = layer_of(name)
        if layer is None:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for imported in sorted(imported_names(tree)):
            target = layer_of(imported)
            if target is not None and target != layer and target not in ALLOWED[layer]:
                problems.append(f"{name} ({layer}) imports {imported} ({target})")
            if layer in FORBIDDEN:
                for prefix in FORBIDDEN[layer]:
                    if imported == prefix or imported.startswith(prefix + "."):
                        problems.append(f"{name} ({layer}) imports {imported}")
                        break
    return problems


def sql_violations(root: Path) -> list[str]:
    """Database access outside the repository: a call to execute, executemany or cursor, or a string
    that starts like an SQL statement, in a service, API or model module."""
    problems: list[str] = []
    for path in sorted(root.rglob("*.py")):
        name = module_name(root, path)
        if layer_of(name) not in NO_SQL_LAYERS:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in DATABASE_CALLS
            ):
                problems.append(f"{name} calls .{node.func.attr}(): SQL belongs in the repository")
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and SQL_STATEMENT.match(node.value)
            ):
                problems.append(f"{name} contains SQL text: it belongs in the repository")
    return problems


# --- the real tree ----------------------------------------------------------------------------


def test_imports_in_the_source_tree_follow_the_layer_rules() -> None:
    assert import_violations(SRC) == []


def test_sql_lives_only_in_the_repository() -> None:
    assert sql_violations(SRC) == []


def test_the_root_of_the_package_holds_only_entry_points_and_the_modules_still_to_split() -> None:
    modules = {path.stem for path in SRC.glob("*.py")}
    packages = {path.name for path in SRC.iterdir() if path.is_dir() and path.name != "__pycache__"}

    assert modules - ROOT_MODULES == set(), "move these into a layer package"
    assert packages - LAYER_PACKAGES == set(), "unknown package: add it to a layer or remove it"


@pytest.mark.parametrize("relative", REQUIRED_MODULES)
def test_the_modules_this_step_creates_exist(relative: str) -> None:
    assert (SRC / relative).is_file()


def test_the_scan_sees_the_whole_tree() -> None:
    names = {module_name(SRC, path) for path in SRC.rglob("*.py")}

    assert "ledger.main" in names
    assert "ledger.api.errors" in names
    assert len(names) >= 12


# --- the scan itself: it must catch what it claims to catch -----------------------------------


def build_tree(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "ledger"
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def test_a_clean_tree_has_no_violations(tmp_path: Path) -> None:
    root = build_tree(
        tmp_path,
        {
            "errors.py": "",
            "models/account_model.py": "from dataclasses import dataclass\n",
            "repository/account_repo.py": (
                "import psycopg\nfrom psycopg import errors\n"
                "from ledger.models.account_model import Account\nfrom ledger.errors import X\n"
            ),
            "service/account_service.py": (
                "import psycopg\nfrom ledger.repository import account_repo\n"
                "from ledger.models.account_model import Account\n"
            ),
            "infra/db.py": "import psycopg_pool\nfrom ledger.errors import X\n",
            "api/accounts.py": (
                "from ledger.service import account_service\nfrom ledger.infra.db import x\n"
            ),
            "main.py": "from ledger.api import accounts\nfrom ledger.infra import db\n",
        },
    )

    assert import_violations(root) == []


@pytest.mark.parametrize(
    "path, source",
    [
        ("models/account_model.py", "from ledger.errors import X\n"),
        ("models/account_model.py", "import psycopg\n"),
        ("infra/db.py", "from ledger.models.account_model import Account\n"),
        ("repository/account_repo.py", "from ledger.service import account_service\n"),
        ("repository/account_repo.py", "from ledger.infra.db import run_in_transaction\n"),
        ("repository/account_repo.py", "import psycopg_pool\n"),
        ("service/account_service.py", "from ledger.infra.db import run_in_transaction\n"),
        ("service/account_service.py", "from ledger.infra import db\n"),
        ("service/account_service.py", "from psycopg import errors\n"),
        ("service/account_service.py", "import psycopg.errors\n"),
        ("service/account_service.py", "import psycopg_pool\n"),
        ("service/account_service.py", "from ledger.api import accounts\n"),
        ("api/accounts.py", "from ledger.repository import account_repo\n"),
        ("api/accounts.py", "import psycopg\n"),
        ("api/accounts.py", "from psycopg_pool import ConnectionPool\n"),
        ("errors.py", "from ledger.models.account_model import Account\n"),
        ("infra/config.py", "from ledger.api import errors\n"),
        ("service/account_service.py", "from ledger import main\n"),
        ("service/account_service.py", "import ledger.migrate\n"),
    ],
    ids=lambda value: value.strip() if isinstance(value, str) else value,
)
def test_each_kind_of_violation_is_caught(tmp_path: Path, path: str, source: str) -> None:
    root = build_tree(tmp_path, {"errors.py": "", "main.py": "", path: source})

    assert import_violations(root) != []


def test_an_import_inside_a_function_or_a_type_checking_block_is_caught(tmp_path: Path) -> None:
    root = build_tree(
        tmp_path,
        {
            "service/a.py": "def f():\n    from ledger.infra.db import x\n",
            "service/b.py": (
                "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"
                "    from ledger.api import accounts\n"
            ),
        },
    )

    flagged = {message.split(" ")[0] for message in import_violations(root)}
    assert flagged == {"ledger.service.a", "ledger.service.b"}


# --- the SQL rule: it must catch what it claims to catch --------------------------------------


@pytest.mark.parametrize(
    "path, source",
    [
        ("service/account_service.py", "def f(conn):\n    conn.execute('x')\n"),
        ("service/account_service.py", "def f(conn):\n    conn.executemany('x', [])\n"),
        (
            "service/account_service.py",
            "def f(conn):\n    with conn.cursor() as c:\n        pass\n",
        ),
        ("service/account_service.py", "SQL = 'SELECT 1'\n"),
        ("service/account_service.py", "SQL = '''\n    INSERT INTO t VALUES (1)\n'''\n"),
        ("service/account_service.py", "SQL = 'UPDATE t SET x = 1'\n"),
        ("service/account_service.py", "SQL = 'DELETE FROM t'\n"),
        ("service/account_service.py", "SQL = 'WITH x AS (SELECT 1) SELECT * FROM x'\n"),
        ("api/accounts.py", "def f(cursor):\n    cursor.execute('x')\n"),
        ("models/account_model.py", "def f(conn):\n    conn.execute('x')\n"),
    ],
    ids=lambda value: value.strip().splitlines()[0] if isinstance(value, str) else value,
)
def test_database_access_outside_the_repository_is_caught(
    tmp_path: Path, path: str, source: str
) -> None:
    root = build_tree(tmp_path, {path: source})

    assert sql_violations(root) != []


def test_the_repository_and_infra_may_run_sql(tmp_path: Path) -> None:
    root = build_tree(
        tmp_path,
        {
            "repository/account_repo.py": (
                "SQL = 'SELECT 1'\ndef f(conn):\n    with conn.cursor() as c:\n"
                "        c.execute(SQL)\n"
            ),
            "infra/db.py": "def f(conn):\n    conn.execute('SET TRANSACTION READ ONLY')\n",
            "main.py": "def f(conn):\n    conn.execute('SELECT 1')\n",
        },
    )

    assert sql_violations(root) == []


def test_ordinary_words_and_calls_are_not_mistaken_for_sql(tmp_path: Path) -> None:
    root = build_tree(
        tmp_path,
        {
            "service/account_service.py": (
                "def f(conn, pool):\n"
                "    '''Selects and updates nothing; deletes nothing.'''\n"
                "    message = 'Select an account to update'\n"
                "    with pool.connection():\n"
                "        return conn.autocommit, message, 'with care'\n"
            ),
        },
    )

    assert sql_violations(root) == []
