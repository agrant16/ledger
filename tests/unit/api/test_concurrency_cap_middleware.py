"""ConcurrencyCapMiddleware driven by hand as a plain ASGI app (docs/design.md, Stack decisions and
Error behavior; ADR 0004).

The cap bounds how many HTTP requests are in flight at once. A request that arrives while the cap
is reached is answered straight away with a 503 `service-busy` problem and a Retry-After header,
without reaching the app, so no transaction has started and retrying is safe. The middleware is
driven here with a fake inner app, because TestClient cannot hold a request open at a chosen point
and cannot produce a client disconnect.

The inner app is steered by the request path: `/hold` waits until the test releases it, `/ok`
answers at once and `/boom` raises. Every wait has its own timeout, so a hang fails the test.
No correctness rule is involved; this is the "Overload and timeouts" row of the testing table.
"""

import asyncio
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from ledger.api.rest.error_handlers import RETRY_AFTER_SECONDS
from ledger.api.rest.middleware.concurrency_cap import ConcurrencyCapMiddleware

WAIT_SECONDS = 5
REQUEST_ID = str(uuid.uuid4())


@dataclass
class Reply:
    status: int
    headers: dict[str, str]  # names in lowercase
    body: bytes


class Harness:
    """A middleware around a fake app, plus helpers to drive requests through it."""

    def __init__(self, cap: int) -> None:
        self.reached_app = 0  # http requests that got past the middleware
        self.release = asyncio.Event()  # lets the held requests finish
        self.release_lifespan = asyncio.Event()
        self.lifespans_seen = 0
        self.app = ConcurrencyCapMiddleware(self.inner, cap=cap)

    async def inner(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            self.lifespans_seen += 1
            await self.release_lifespan.wait()
            return
        self.reached_app += 1
        if scope["path"] == "/boom":
            raise RuntimeError("boom")
        if scope["path"] == "/hold":
            await self.release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def request(self, path: str = "/ok") -> Reply:
        scope = {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "state": {"request_id": REQUEST_ID},  # as the request-id middleware leaves it
        }
        sent: list[dict] = []

        async def receive() -> dict:
            return {"type": "http.request"}

        async def send(message: dict) -> None:
            sent.append(message)

        await self.app(scope, receive, send)
        start = sent[0]
        headers = {k.decode().lower(): v.decode() for k, v in start.get("headers", [])}
        body = b"".join(m.get("body", b"") for m in sent[1:])
        return Reply(start["status"], headers, body)

    async def lifespan(self) -> None:
        async def receive() -> dict:
            return {"type": "lifespan.startup"}

        async def send(message: dict) -> None:
            pass

        await self.app({"type": "lifespan"}, receive, send)

    def hold(self, count: int = 1) -> list[asyncio.Task]:
        return [asyncio.ensure_future(self.request("/hold")) for _ in range(count)]


async def wait_until(condition: Callable[[], bool], what: str) -> None:
    async def poll() -> None:
        while not condition():
            await asyncio.sleep(0.005)

    try:
        await asyncio.wait_for(poll(), WAIT_SECONDS)
    except TimeoutError:
        pytest.fail(f"timed out waiting for {what}")


def test_requests_up_to_the_cap_are_all_admitted() -> None:
    async def scenario() -> None:
        harness = Harness(cap=3)
        held = harness.hold(3)
        await wait_until(lambda: harness.reached_app == 3, "three requests inside the app")

        harness.release.set()
        replies = await asyncio.gather(*held)

        assert [r.status for r in replies] == [200, 200, 200]

    asyncio.run(scenario())


def test_the_request_over_the_cap_gets_a_503_problem_and_never_reaches_the_app() -> None:
    async def scenario() -> None:
        harness = Harness(cap=2)
        held = harness.hold(2)
        await wait_until(lambda: harness.reached_app == 2, "two requests inside the app")

        reply = await harness.request("/ok")

        assert reply.status == 503
        assert harness.reached_app == 2, "the rejected request must not reach the app"
        assert reply.headers["content-type"].startswith("application/problem+json")
        assert reply.headers["retry-after"] == str(RETRY_AFTER_SECONDS)
        if "content-length" in reply.headers:
            assert int(reply.headers["content-length"]) == len(reply.body)
        problem = json.loads(reply.body)
        assert set(problem) == {"type", "title", "status", "detail", "instance"}
        assert problem["type"] == "urn:ledger:error:service-busy"
        assert problem["title"] == "Service busy"
        assert problem["status"] == 503
        assert problem["instance"] == f"urn:uuid:{REQUEST_ID}"
        assert isinstance(problem["detail"], str) and problem["detail"]

        harness.release.set()
        assert [r.status for r in await asyncio.gather(*held)] == [200, 200]

    asyncio.run(scenario())


def test_of_many_requests_arriving_together_exactly_the_cap_is_admitted() -> None:
    async def scenario() -> None:
        harness = Harness(cap=3)
        tasks = harness.hold(10)

        await wait_until(
            lambda: harness.reached_app == 3 and sum(t.done() for t in tasks) == 7,
            "three admitted and seven rejected",
        )
        await asyncio.sleep(0.05)  # a wrongly admitted extra request would show up by now
        assert harness.reached_app == 3

        harness.release.set()
        statuses = sorted(r.status for r in await asyncio.gather(*tasks))

        assert statuses == [200] * 3 + [503] * 7

    asyncio.run(scenario())


def test_a_slot_is_free_again_once_a_request_has_finished() -> None:
    async def scenario() -> None:
        harness = Harness(cap=1)

        statuses = [(await harness.request("/ok")).status for _ in range(5)]

        assert statuses == [200] * 5

    asyncio.run(scenario())


def test_rejected_requests_do_not_use_up_slots() -> None:
    async def scenario() -> None:
        harness = Harness(cap=1)
        (held,) = harness.hold(1)
        await wait_until(lambda: harness.reached_app == 1, "the held request inside the app")

        rejected = [(await harness.request("/ok")).status for _ in range(5)]
        assert rejected == [503] * 5

        harness.release.set()
        assert (await held).status == 200
        assert (await harness.request("/ok")).status == 200

    asyncio.run(scenario())


def test_a_slot_is_free_again_after_the_app_raised() -> None:
    async def scenario() -> None:
        harness = Harness(cap=1)

        for _ in range(3):  # with a leak, the second attempt would be a 503, not the error
            with pytest.raises(RuntimeError, match="boom"):
                await harness.request("/boom")

        assert (await harness.request("/ok")).status == 200

    asyncio.run(scenario())


def test_a_slot_is_free_again_after_the_request_was_cancelled() -> None:
    """A client that disconnects mid-request cancels the task serving it."""

    async def scenario() -> None:
        harness = Harness(cap=1)
        (held,) = harness.hold(1)
        await wait_until(lambda: harness.reached_app == 1, "the held request inside the app")
        assert (await harness.request("/ok")).status == 503  # the slot is in use

        held.cancel()
        with pytest.raises(asyncio.CancelledError):
            await held

        assert (await harness.request("/ok")).status == 200

    asyncio.run(scenario())


def test_the_lifespan_is_passed_through_and_never_takes_a_slot() -> None:
    """The lifespan scope lasts as long as the app does, so counting it would fill the cap."""

    async def scenario() -> None:
        harness = Harness(cap=1)
        lifespan = asyncio.ensure_future(harness.lifespan())
        await wait_until(lambda: harness.lifespans_seen == 1, "the lifespan inside the app")

        assert (await harness.request("/ok")).status == 200

        harness.release_lifespan.set()
        await lifespan

    asyncio.run(scenario())


def test_each_middleware_instance_keeps_its_own_count() -> None:
    async def scenario() -> None:
        first = Harness(cap=1)
        second = Harness(cap=1)
        (held,) = first.hold(1)
        await wait_until(lambda: first.reached_app == 1, "the held request inside the app")

        assert (await second.request("/ok")).status == 200

        first.release.set()
        await held

    asyncio.run(scenario())
