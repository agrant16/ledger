"""RequestIDMiddleware driven by hand as a plain ASGI app (docs/design.md, Request id and logging).

TestClient cannot produce some of the messages an ASGI app is allowed to send, such as a response
start with no `headers` key, and it always runs with an HTTP scope. These tests call the middleware
directly with a tiny fake inner app and a `send` that records what it is given. Header names are
compared in lowercase, because ASGI treats them as case-insensitive bytes.
"""

import asyncio
import uuid

from ledger.api.request_id import RequestIDMiddleware
from ledger.infra.logging_config import request_id_ctx_var

HEADER = b"x-request-id"


def http_scope() -> dict:
    return {"type": "http", "method": "GET", "path": "/", "headers": []}


def run(inner_app, scope: dict) -> list[dict]:
    """Drive the middleware once and return every message it passed on."""
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request"}

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(RequestIDMiddleware(inner_app)(scope, receive, send))
    return sent


def request_id_header(start: dict) -> str:
    """The request id header of a response start message, which must be there exactly once."""
    values = [v for k, v in start["headers"] if k.lower() == HEADER]
    assert len(values) == 1
    uuid.UUID(values[0].decode())
    return values[0].decode()


def test_a_response_start_without_a_headers_key_still_gets_the_request_id() -> None:
    async def inner(scope, receive, send) -> None:
        await send({"type": "http.response.start", "status": 204})  # no "headers" key at all
        await send({"type": "http.response.body", "body": b""})

    start, body = run(inner, http_scope())

    request_id_header(start)
    assert body == {"type": "http.response.body", "body": b""}, "only the start message changes"


def test_existing_response_headers_are_kept_and_the_id_is_added_once() -> None:
    async def inner(scope, receive, send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )

    (start,) = run(inner, http_scope())

    assert start["headers"][0] == (b"content-type", b"text/plain")
    assert len(start["headers"]) == 2
    request_id_header(start)


def test_a_request_with_no_headers_still_gets_an_id_in_state_and_the_context_variable() -> None:
    seen: dict = {}

    async def inner(scope, receive, send) -> None:
        seen["state"] = scope["state"]["request_id"]
        seen["var"] = request_id_ctx_var.get()
        await send({"type": "http.response.start", "status": 200, "headers": []})

    (start,) = run(inner, http_scope())  # the scope has "headers": []

    header_id = request_id_header(start)
    assert seen["state"] == seen["var"] == header_id


def test_every_request_gets_a_different_id() -> None:
    async def inner(scope, receive, send) -> None:
        await send({"type": "http.response.start", "status": 200})

    first = request_id_header(run(inner, http_scope())[0])
    second = request_id_header(run(inner, http_scope())[0])
    assert first != second


def test_a_scope_that_is_not_http_goes_straight_through_exactly_once() -> None:
    """Lifespan and websocket scopes get no id and no header. Calling the app twice would run the
    application's startup twice."""
    calls: list[str] = []

    async def inner(scope, receive, send) -> None:
        calls.append(scope["type"])
        await send({"type": "lifespan.startup.complete"})

    sent = run(inner, {"type": "lifespan"})

    assert calls == ["lifespan"]
    assert sent == [{"type": "lifespan.startup.complete"}]
