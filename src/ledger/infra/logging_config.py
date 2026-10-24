import logging
from contextvars import ContextVar
from typing import Any

UVICORN_DUPLICATE = "Exception in ASGI application"

request_id_ctx_var: ContextVar[str] = ContextVar("request_id", default="-")

_original_factory = logging.getLogRecordFactory()


class DropUvicornDuplicate(logging.Filter):
    """Drops uvicorn's duplicate 500 log record.

    Uvicorn logs "Exception in ASGI application" for an unhandled error, which
    duplicates the service's own 500 log.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return record.getMessage().strip() != UVICORN_DUPLICATE


def _factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
    record = _original_factory(*args, **kwargs)
    record.request_id = request_id_ctx_var.get()  # "-" outside a request
    return record


def install_log_record_factory() -> None:
    """Sets the log record factory so every record carries the current `request_id`.

    Outside a request the `request_id` is `-`.
    """
    logging.setLogRecordFactory(_factory)


def configure_logging() -> None:
    """Configures logging for the service and for scripts.

    Installs the request id log record factory, sets the log format, and adds the filter
    that drops uvicorn's duplicate 500 record. Safe to call more than once.
    """
    install_log_record_factory()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"
    )
    logger = logging.getLogger("uvicorn.error")
    if not any(isinstance(f, DropUvicornDuplicate) for f in logger.filters):
        logger.addFilter(DropUvicornDuplicate())
