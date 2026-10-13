import logging

from ledger.api.request_id import install_log_record_factory

UVICORN_DUPLICATE = "Exception in ASGI application"


class DropUvicornDuplicate(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.getMessage().strip() != UVICORN_DUPLICATE


def configure_logging() -> None:
    install_log_record_factory()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"
    )
    logger = logging.getLogger("uvicorn.error")
    if not any(isinstance(f, DropUvicornDuplicate) for f in logger.filters):
        logger.addFilter(DropUvicornDuplicate())
