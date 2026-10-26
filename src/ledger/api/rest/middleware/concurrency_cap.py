import json
import logging

from starlette.types import ASGIApp, Receive, Scope, Send

logger: logging.Logger = logging.getLogger(__name__)


class ConcurrencyCapMiddleware:
    """ASGI middleware that rejects requests above the concurrency cap. (ADR 0004, ADR 0009)

    A request that arrives while `cap` HTTP requests are in flight gets a 503 problem
    response with a `Retry-After` header, and the wrapped app never sees it. Other scopes,
    such as lifespan, pass through. Calls to `/health` are passed through.

    Attributes:
        app: The wrapped application.
        cap: The allowed number of concurrent HTTP requests.
        request_count: The number of HTTP requests currently being handled.
    """

    def __init__(self, app: ASGIApp, cap: int):
        """Initializes the middleware.

        Args:
            app: The application to wrap.
            cap: The allowed number of concurrent HTTP requests.
        """
        self.app = app
        self.cap = cap
        self.request_count = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Passes the request on, or rejects it with a 503 when the cap is reached.

        Args:
            scope: The ASGI connection scope.
            receive: The ASGI receive channel.
            send: The ASGI send channel.
        """
        # any non-http requests (lifespan, etc.) get passed through
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # health calls are passed through as they have their own limiter
        if scope["path"] == "/health":
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
        """Sends the 503 problem response with a `Retry-After` header."""
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
