"""Helpers that make `/openapi.json` describe the errors the service really returns.

`problem_responses` builds a route's `responses=` from the error table, so a documented status,
type and title cannot drift from what the handlers send. `install_openapi` adds the problem
schemas to the document and removes the `422` FastAPI documents for validation errors, because
validation errors are 400 here and 422 means insufficient funds.
"""

from typing import Any

from fastapi import FastAPI

from ledger.api.rest.problems import problem_for
from ledger.api.rest.schemas.problem_schema import ProblemDetails
from ledger.errors import LedgerError

PROBLEM_MEDIA_TYPE = "application/problem+json"
PROBLEM_REF = {"$ref": "#/components/schemas/ProblemDetails"}
FASTAPI_VALIDATION_SCHEMAS = ("HTTPValidationError", "ValidationError")

RETRY_AFTER_HEADER = {
    "Retry-After": {
        "description": "Seconds to wait before retrying. Retrying with the same key is safe.",
        "schema": {"type": "integer"},
    }
}


def problem_responses(*error_classes: type[LedgerError]) -> dict[int | str, dict[str, Any]]:
    """The `responses=` entries for the given errors, plus the generic 500 every route can
    return. Errors that share a status share one entry that names each of their `type`s."""
    lines_by_status: dict[int, list[str]] = {}
    for error_class in error_classes:
        spec = problem_for(error_class)
        lines_by_status.setdefault(spec.status, []).append(f"{spec.title} (`{spec.type}`)")
    lines_by_status[500] = ["Internal Server Error (`about:blank`): a bug, logged by the service"]

    responses: dict[int | str, dict[str, Any]] = {}
    for status, lines in sorted(lines_by_status.items()):
        entry: dict[str, Any] = {
            "description": "\n".join(lines),
            "content": {PROBLEM_MEDIA_TYPE: {"schema": PROBLEM_REF}},
        }
        if status == 503:
            entry["headers"] = RETRY_AFTER_HEADER
        responses[status] = entry
    return responses


def _is_fastapi_validation_response(response: dict[str, Any]) -> bool:
    schema = response.get("content", {}).get("application/json", {}).get("schema", {})
    return schema.get("$ref") == "#/components/schemas/HTTPValidationError"


def _fix_document(document: dict[str, Any]) -> None:
    """Edits the generated document in place. Safe to run more than once."""
    for methods in document.get("paths", {}).values():
        for operation in methods.values():
            responses = operation.get("responses", {})
            if "422" in responses and _is_fastapi_validation_response(responses["422"]):
                del responses["422"]

    schemas = document.setdefault("components", {}).setdefault("schemas", {})
    for name in FASTAPI_VALIDATION_SCHEMAS:
        schemas.pop(name, None)

    problem_schema = ProblemDetails.model_json_schema(ref_template="#/components/schemas/{model}")
    schemas.update(problem_schema.pop("$defs", {}))
    schemas["ProblemDetails"] = problem_schema


def install_openapi(app: FastAPI) -> None:
    """Make `app.openapi()` return the cleaned-up document."""
    generate = app.openapi

    def openapi() -> dict[str, Any]:
        document = generate()
        _fix_document(document)
        return document

    app.openapi = openapi  # type: ignore[method-assign]
