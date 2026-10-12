import logging

from ledger.api.request_id import install_log_record_factory


def configure_logging() -> None:
    install_log_record_factory()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"
    )
