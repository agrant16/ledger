import json
import logging

from starlette.types import Receive, Scope, Send

logger: logging.Logger = logging.getLogger(__name__)


class ConcurrencyCapMiddleware:
    def __init__(self, app, cap: int):
        self.app = app
        self.cap = cap
        self.request_count = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        # http requests increase the request_count
        if scope["type"] == "http":
            if self.request_count >= self.cap:
                logger.warning(
                    f"Concurrency cap reached ({self.request_count}/{self.cap}). Rejecting request."
                )
                await self._send_503_response(scope, send)
                return
            self.request_count += 1

            # once an http request ends we decrement the counter
            try:
                await self.app(scope, receive, send)
            finally:
                self.request_count = max(0, self.request_count - 1)
            return
        # any non-http requests (lifespan, etc.) get passed through
        await self.app(scope, receive, send)
        return

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
                    (b"content-type", b"application/problem+json"),
                    (b"content-length", str(len(body)).encode("utf-8")),
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
