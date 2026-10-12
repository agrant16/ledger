import uuid
from contextvars import ContextVar

request_id_ctx_var: ContextVar[str] = ContextVar("request_id", default="")


class RequestIDMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        # not http: pass through. http: make the id, store it, wrap `send`
        # so the "http.response.start" message gets the X-Request-ID header
        if scope["type"] != "http":
            await self.app(scope, receive, send)

        req_id = str(uuid.uuid4())
        token = request_id_ctx_var.set(req_id)

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start":
                if "headers" not in message:
                    message["headers"] = []
                message["headers"].append(b"x-request-id", req_id.encode("utf-8"))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            request_id_ctx_var.reset(token)