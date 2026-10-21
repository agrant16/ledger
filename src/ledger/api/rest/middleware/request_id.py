import uuid

from starlette.types import ASGIApp, Receive, Scope, Send

from ledger.infra.logging_config import request_id_ctx_var


class RequestIDMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # not http: pass through. http: make the id, store it, wrap `send`
        # so the "http.response.start" message gets the X-Request-ID header
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        req_id = str(uuid.uuid4())
        # no finally block because the 500 handler runs after this middleware
        # has exited and still needs the id for its log record
        request_id_ctx_var.set(req_id)
        scope.setdefault("state", {})["request_id"] = req_id

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start":
                if "headers" not in message:
                    message["headers"] = []
                message["headers"].append((b"X-Request-Id", req_id.encode("utf-8")))
            await send(message)

        await self.app(scope, receive, send_wrapper)
        return
