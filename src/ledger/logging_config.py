import logging

from ledger.api.errors import install_log_record_factory


def configure_logging() -> None:
    install_log_record_factory()
    logging.basicConfig(level=logging.INFO, format="... [%(request_id)s] %(message)s")
