"""The OpenAPI document of the real app (docs/design.md, Milestone 3 and Error model; issue #107).

`/openapi.json` is the contract clients read, so it must say what the service really does. These
tests read `create_app().openapi()` of the real app and need no database.

Contract these tests expect:

- The paths are exactly the five endpoints, and `/health` is not among them.
- Every operation has a summary and a description.
- Each operation documents its success status and the errors it can return, no more and no fewer,
  as in EXPECTED_STATUSES. Validation errors are 400. FastAPI's own `422` / `HTTPValidationError`
  must be gone: 422 is insufficient funds, so only `POST /transfers` documents a 422.
- Every error response is `application/problem+json` and refers to the `ProblemDetails` schema.
  Each 503 declares a `Retry-After` header.
- The description of an error response names the `type` URN of every error that can produce it
  (EXPECTED_TYPES), and each URN exists in the error table (`ledger/api/rest/problems.py`), so the
  documentation cannot name a type the handlers never send.
- `ProblemDetails` lists the members every problem carries, `errors` for validation problems and
  the extension members of the domain errors.

The control is the exact status table: a check for "has some error responses" would pass on the
wrong 422 FastAPI generates, and the table does not. None of correctness rules 1 to 6 is touched.
"""

import json

import pytest

from ledger.api.rest.problems import PROBLEMS
from ledger.main import create_app

PATHS = [
    ("post", "/accounts"),
    ("get", "/accounts/{account_id}/balance"),
    ("get", "/accounts/{account_id}/entries"),
    ("post", "/transfers"),
    ("get", "/transfers/{transfer_id}"),
]

# The statuses each operation documents: its success status, the errors from the design doc, and
# the generic 500 and 503 that every route can return.
EXPECTED_STATUSES = {
    ("post", "/accounts"): {"201", "400", "409", "500", "503"},
    ("get", "/accounts/{account_id}/balance"): {"200", "400", "404", "500", "503"},
    ("get", "/accounts/{account_id}/entries"): {"200", "400", "404", "500", "503"},
    ("post", "/transfers"): {"201", "400", "404", "409", "422", "500", "503"},
    ("get", "/transfers/{transfer_id}"): {"200", "400", "404", "500", "503"},
}

INVALID = "urn:ledger:error:invalid-request"
UNKNOWN_ACCOUNT = "urn:ledger:error:unknown-account"
BUSY = "urn:ledger:error:service-busy"

# The type URNs each (operation, error status) description must name.
EXPECTED_TYPES = {
    ("post", "/accounts"): {
        "400": {INVALID},
        "409": {"urn:ledger:error:duplicate-account-name"},
        "503": {BUSY},
    },
    ("get", "/accounts/{account_id}/balance"): {
        "400": {INVALID},
        "404": {UNKNOWN_ACCOUNT},
        "503": {BUSY},
    },
    ("get", "/accounts/{account_id}/entries"): {
        "400": {INVALID},
        "404": {UNKNOWN_ACCOUNT},
        "503": {BUSY},
    },
    ("post", "/transfers"): {
        "400": {INVALID, "urn:ledger:error:currency-mismatch"},
        "404": {UNKNOWN_ACCOUNT},
        "409": {"urn:ledger:error:idempotency-conflict"},
        "422": {"urn:ledger:error:insufficient-funds"},
        "503": {BUSY},
    },
    ("get", "/transfers/{transfer_id}"): {
        "400": {INVALID},
        "404": {"urn:ledger:error:unknown-transfer"},
        "503": {BUSY},
    },
}

PROBLEM_MEMBERS = {"type", "title", "status", "detail", "instance"}
EXTENSION_MEMBERS = {
    "errors",
    "account_id",
    "balance_minor",
    "net_change_minor",
    "account_name",
}


@pytest.fixture(scope="module")
def document() -> dict:
    return create_app().openapi()


def operation(document: dict, method: str, path: str) -> dict:
    return document["paths"][path][method]


def error_responses(op: dict) -> dict[str, dict]:
    return {status: body for status, body in op["responses"].items() if int(status) >= 400}


def test_the_paths_are_exactly_the_five_endpoints(document) -> None:
    documented = {
        (method, path) for path, methods in document["paths"].items() for method in methods
    }
    assert documented == set(PATHS)
    assert "/health" not in document["paths"]


@pytest.mark.parametrize(("method", "path"), PATHS)
def test_every_operation_has_a_summary_and_a_description(document, method, path) -> None:
    op = operation(document, method, path)
    assert op.get("summary", "").strip()
    assert op.get("description", "").strip()


@pytest.mark.parametrize(("method", "path"), PATHS)
def test_each_operation_documents_exactly_its_expected_statuses(document, method, path) -> None:
    op = operation(document, method, path)
    assert set(op["responses"]) == EXPECTED_STATUSES[(method, path)]


def test_only_post_transfers_documents_a_422(document) -> None:
    with_422 = {(m, p) for m, p in PATHS if "422" in operation(document, m, p)["responses"]}
    assert with_422 == {("post", "/transfers")}


def test_fastapis_default_validation_schemas_are_gone(document) -> None:
    text = json.dumps(document)
    assert "HTTPValidationError" not in text
    assert not [name for name in document["components"]["schemas"] if "ValidationError" in name]


@pytest.mark.parametrize(("method", "path"), PATHS)
def test_error_responses_are_problem_json_with_the_problem_schema(document, method, path) -> None:
    for status, body in error_responses(operation(document, method, path)).items():
        assert set(body["content"]) == {"application/problem+json"}, status
        schema = body["content"]["application/problem+json"]["schema"]
        assert schema == {"$ref": "#/components/schemas/ProblemDetails"}, status


@pytest.mark.parametrize(("method", "path"), PATHS)
def test_a_503_declares_a_retry_after_header(document, method, path) -> None:
    headers = operation(document, method, path)["responses"]["503"].get("headers", {})
    assert "Retry-After" in headers


@pytest.mark.parametrize(("method", "path"), PATHS)
def test_error_descriptions_name_the_type_of_every_error_that_can_cause_them(
    document, method, path
) -> None:
    responses = operation(document, method, path)["responses"]
    for status, urns in EXPECTED_TYPES[(method, path)].items():
        description = responses[status]["description"]
        for urn in urns:
            assert urn in description, f"{status} does not name {urn}"


def test_every_documented_type_exists_in_the_error_table() -> None:
    known = {spec.type for spec in PROBLEMS.values()}
    documented = {
        urn for per_route in EXPECTED_TYPES.values() for urns in per_route.values() for urn in urns
    }
    assert documented <= known


def test_the_problem_schema_lists_the_common_and_extension_members(document) -> None:
    schema = document["components"]["schemas"]["ProblemDetails"]
    assert set(schema["properties"]) == PROBLEM_MEMBERS | EXTENSION_MEMBERS
    assert set(schema["required"]) == PROBLEM_MEMBERS


def test_a_validation_error_item_has_a_detail_and_a_pointer_or_parameter(document) -> None:
    item = document["components"]["schemas"]["ProblemFieldError"]
    assert set(item["properties"]) == {"detail", "pointer", "parameter"}
    assert item["required"] == ["detail"]
