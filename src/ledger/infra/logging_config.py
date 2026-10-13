import logging
from contextvars import ContextVar

UVICORN_DUPLICATE = "Exception in ASGI application"

request_id_ctx_var: ContextVar[str] = ContextVar("request_id", default="-")

_original_factory = logging.getLogRecordFactory()


class DropUvicornDuplicate(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.getMessage().strip() != UVICORN_DUPLICATE


def _factory(*args, **kwargs):
    record = _original_factory(*args, **kwargs)
    record.request_id = request_id_ctx_var.get()  # "-" outside a request
    return record


def install_log_record_factory():
    logging.setLogRecordFactory(_factory)


def configure_logging() -> None:
    install_log_record_factory()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"
    )
    logger = logging.getLogger("uvicorn.error")
    if not any(isinstance(f, DropUvicornDuplicate) for f in logger.filters):
        logger.addFilter(DropUvicornDuplicate())
