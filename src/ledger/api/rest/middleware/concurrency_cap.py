import json
import logging

from starlette.types import ASGIApp, Receive, Scope, Send

logger: logging.Logger = logging.getLogger(__name__)


class ConcurrencyCapMiddleware:
    """An ASGI Middleware class that handles concurrency cap failures.

    This middleware class counts incoming HTTP requests and checks when the
    concurrency cap is reached. If the cap is reached any incoming HTTP requests
    will return a 503 response with a Retry-After header. This will happen until
    ongoing HTTP requests finish and the count is decremented.

    Attributes:
        app: The inner application service being run
        cap: The allowed number of concurrent requests
        request_count: The current number of incoming HTTP requests being handled
    """

    def __init__(self, app: ASGIApp, cap: int):
        """Initialize an instance of the ConcurrencyCapMiddleware class"""
        self.app = app
        self.cap = cap
        self.request_count = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handles incoming messages and checks if we've hit the concurrency cap."""
        # any non-http requests (lifespan, etc.) get passed through
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if self.request_count >= self.cap:
            # if the concurrency cap is reached log a warning and return a 503
            logger.warning(
                f"Concurrency cap reached ({self.request_count}/{self.cap}). Rejecting request."
            )
            await self._send_503_response(scope, send)
            return

        # Increment the counter for any http request and decrement once
        # that request has finished
        self.request_count += 1
        try:
            await self.app(scope, receive, send)
        finally:
            self.request_count -= 1

    async def _send_503_response(self, scope: Scope, send: Send) -> None:
        """Builds a 503 response to deal with the concurrency cap"""
        body = json.dumps(
            {
                "title": "Service busy",
                "detail": (
                    "The service is handling too many requests right now. "
                    "Retry after the number of seconds in the Retry-After "
                    "header."
                ),
                "type": "urn:ledger:error:service-busy",
                "status": 503,
                "instance": f"urn:uuid:{scope['state']['request_id']}",
            }
        ).encode("utf-8")

        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    (b"Content-Type", b"application/problem+json"),
                    (b"Content-Length", str(len(body)).encode("utf-8")),
                    (b"Retry-After", b"10"),
                ],
            }
        )

        await send(
            {
                "type": "http.response.body",
                "body": body,
                "more_body": False,
            }
        )
