"""Helpers for the tests that read the source tree with the ast module (ADR 0013).

Shared by tests/unit/test_layering.py (which layer may import which) and
tests/unit/test_api_structure.py (the structure inside the API layer). Nothing here imports the code
under test: it only parses source files, so these tests need no database.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "ledger"

ENTRY_POINTS = {"main", "migrate", "seed"}
LAYER_PACKAGES = {"models", "repository", "service", "infra", "api"}


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


def build_tree(tmp_path: Path, files: dict[str, str]) -> Path:
    """A throwaway package directory named `ledger` holding the given files, for testing a rule."""
    root = tmp_path / "ledger"
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root
