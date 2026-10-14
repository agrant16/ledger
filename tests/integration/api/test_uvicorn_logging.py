"""An unexpected 500 is logged once, under a real uvicorn server (docs/design.md, Request id and
logging; ADR 0009).

Our catch-all handler logs the exception with the request id and sends the 500. Starlette then
re-raises the exception on purpose, and uvicorn catches that and logs it again ("Exception in ASGI
application") with its own traceback and no request id. TestClient swallows the re-raise, so no
other test can see the second record: this one starts a real server on a free port and counts every
ERROR record the process emits.

The throwaway app starts the same way the real one does: configure_logging() runs in its lifespan,
after uvicorn has set up its own loggers.
"""

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

import pytest
import uvicorn
from fastapi import FastAPI

from ledger.api.rest.error_handlers import install_error_handling
from ledger.infra.logging_config import configure_logging

WATCHED_LOGGERS = ("", "uvicorn", "uvicorn.error", "uvicorn.access")


class Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    yield


def build_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    install_error_handling(app)

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("probe failure")

    @app.get("/ok")
    def ok() -> dict[str, str]:
        return {"status": "ok"}

    return app


@pytest.fixture
def server() -> Iterator[tuple[str, Collect]]:
    """A running uvicorn server and a collector for every record the process logs while it runs.

    The collector is attached once the server has started, because uvicorn rebuilds its own
    loggers' handlers when it is configured. uvicorn.error and uvicorn.access do not propagate to
    the root logger, so each is watched directly."""
    config = uvicorn.Config(build_app(), host="127.0.0.1", port=0, log_level="info")
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not instance.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert instance.started, "the server did not start"
    port = instance.servers[0].sockets[0].getsockname()[1]

    collector = Collect()
    for name in WATCHED_LOGGERS:
        logging.getLogger(name).addHandler(collector)
    try:
        yield f"http://127.0.0.1:{port}", collector
    finally:
        for name in WATCHED_LOGGERS:
            logging.getLogger(name).removeHandler(collector)
        instance.should_exit = True
        thread.join(timeout=10)


def get(url: str) -> tuple[int, dict[str, str], dict]:
    """Status, headers with lowercase names (that is how uvicorn sends them), and the JSON body."""
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            status, headers, body = response.status, response.headers, response.read()
    except urllib.error.HTTPError as failure:
        status, headers, body = failure.code, failure.headers, failure.read()
    return status, {name.lower(): value for name, value in headers.items()}, json.loads(body)


def errors_in(collector: Collect) -> list[logging.LogRecord]:
    return [record for record in collector.records if record.levelno >= logging.ERROR]


def test_an_unexpected_500_is_logged_once(server: tuple[str, Collect]) -> None:
    base_url, collector = server

    status, headers, body = get(f"{base_url}/boom")

    assert status == 500
    assert body["type"] == "about:blank"
    errors = errors_in(collector)
    assert [(record.name, record.getMessage()) for record in errors] == [
        ("ledger.api.rest.error_handlers", "unhandled exception")
    ]


def test_the_one_record_has_the_exception_and_the_request_id(
    server: tuple[str, Collect],
) -> None:
    base_url, collector = server

    _, headers, body = get(f"{base_url}/boom")

    (record,) = errors_in(collector)
    assert record.exc_info is not None
    assert record.exc_info[0] is RuntimeError
    assert record.request_id == headers["x-request-id"]  # type: ignore[attr-defined]
    assert body["instance"] == f"urn:uuid:{headers['x-request-id']}"


def test_each_failing_request_is_logged_once(server: tuple[str, Collect]) -> None:
    base_url, collector = server

    get(f"{base_url}/boom")
    get(f"{base_url}/boom")
    get(f"{base_url}/boom")

    assert len(errors_in(collector)) == 3


def test_a_successful_request_logs_no_error(server: tuple[str, Collect]) -> None:
    base_url, collector = server

    status, _, _ = get(f"{base_url}/ok")

    assert status == 200
    assert errors_in(collector) == []


def test_uvicorns_access_line_for_the_500_is_still_logged(server: tuple[str, Collect]) -> None:
    """Only the duplicate traceback is dropped. The access log is how a 500 shows up in the log."""
    base_url, collector = server

    get(f"{base_url}/boom")

    access = [r for r in collector.records if r.name == "uvicorn.access"]
    assert any("/boom" in r.getMessage() and "500" in r.getMessage() for r in access)
