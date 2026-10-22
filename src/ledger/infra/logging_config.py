import logging
from contextvars import ContextVar

UVICORN_DUPLICATE = "Exception in ASGI application"

request_id_ctx_var: ContextVar[str] = ContextVar("request_id", default="-")

_original_factory = logging.getLogRecordFactory()


class DropUvicornDuplicate(logging.Filter):
    """Drops uvicorn’s ‘Exception in ASGI application’ record, which duplicates the service’s own 500 log."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.getMessage().strip() != UVICORN_DUPLICATE


def _factory(*args, **kwargs):
    record = _original_factory(*args, **kwargs)
    record.request_id = request_id_ctx_var.get()  # "-" outside a request
    return record


def install_log_record_factory():
    """Applies the request-id from context variables to the log record."""
    logging.setLogRecordFactory(_factory)


def configure_logging() -> None:
    """Adds the uvicorn duplicate filter to remove duplicate 500 logs. Safe to call twice."""
    install_log_record_factory()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"
    )
    logger = logging.getLogger("uvicorn.error")
    if not any(isinstance(f, DropUvicornDuplicate) for f in logger.filters):
        logger.addFilter(DropUvicornDuplicate())
