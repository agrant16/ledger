"""configure_logging (docs/design.md, Request id and logging).

It sets up the log format once at startup: time, level, logger name, the request id in brackets,
then the message. The id shows as `-` outside a request.

It also drops the one record uvicorn adds for an unexpected 500. Starlette re-raises the exception
after our catch-all handler has logged it and sent the response, so uvicorn would log it a second
time, without the request id ("Exception in ASGI application", with a trailing newline). The
real-server test for this is tests/integration/api/test_uvicorn_logging.py.

pytest's own log handlers sit on the root logger while a test runs, which makes logging.basicConfig
do nothing, and caplog bypasses formatting entirely. So each test runs the function in a fresh
Python process and reads the line it prints on stderr.
"""

import re
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"

# 2026-10-12 11:03:26,119 WARNING some.logger [-] hello
LINE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} WARNING some\.logger \[-\] hello$")


def stderr_of(script: str) -> list[str]:
    run = subprocess.run(
        [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(SRC)!r})\n{script}"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, run.stderr
    return run.stderr.splitlines()


def test_a_log_line_has_the_time_level_logger_and_the_request_id_placeholder() -> None:
    lines = stderr_of(
        "import logging\n"
        "from ledger.infra.logging_config import configure_logging\n"
        "configure_logging()\n"
        "logging.getLogger('some.logger').warning('hello')\n"
    )
    assert len(lines) == 1
    assert LINE.match(lines[0]), lines[0]


def test_calling_it_twice_does_not_double_the_output() -> None:
    lines = stderr_of(
        "import logging\n"
        "from ledger.infra.logging_config import configure_logging\n"
        "configure_logging()\n"
        "configure_logging()\n"
        "logging.getLogger('some.logger').warning('hello')\n"
    )
    assert len(lines) == 1


def test_info_messages_are_shown() -> None:
    """The level is INFO, so the startup line from the pool-wiring work will be visible."""
    lines = stderr_of(
        "import logging\n"
        "from ledger.infra.logging_config import configure_logging\n"
        "configure_logging()\n"
        "logging.getLogger('some.logger').info('hello')\n"
    )
    assert len(lines) == 1
    assert lines[0].endswith("INFO some.logger [-] hello")


# --- uvicorn's duplicate record for an unexpected 500 -----------------------------------------

UVICORN_DUPLICATE = "Exception in ASGI application\n"  # as written in uvicorn's h11/httptools code
LIFESPAN_FAILURE = "Exception in 'lifespan' protocol\n"  # uvicorn/lifespan/on.py


def uvicorn_lines(*logger_calls: str) -> list[str]:
    """What reaches stderr when each call runs after configure_logging()."""
    body = "".join(f"{call}\n" for call in logger_calls)
    return stderr_of(
        "import logging\n"
        "from ledger.infra.logging_config import configure_logging\n"
        "configure_logging()\n"
        "uvicorn_error = logging.getLogger('uvicorn.error')\n"
        "other = logging.getLogger('some.logger')\n" + body
    )


def test_uvicorns_duplicate_record_for_an_unexpected_500_is_dropped() -> None:
    lines = uvicorn_lines(
        f"uvicorn_error.error({UVICORN_DUPLICATE!r}, exc_info=ValueError('boom'))",
    )
    assert lines == []


def test_the_duplicate_is_dropped_whether_or_not_it_ends_in_a_newline() -> None:
    lines = uvicorn_lines(
        "uvicorn_error.error('Exception in ASGI application', exc_info=ValueError('boom'))",
    )
    assert lines == []


def test_the_duplicate_is_dropped_when_logged_with_the_exception_method() -> None:
    """The websocket code in uvicorn logs the same text through logger.exception."""
    lines = uvicorn_lines(
        "try:\n"
        "    raise ValueError('boom')\n"
        "except ValueError:\n"
        f"    uvicorn_error.exception({UVICORN_DUPLICATE!r})",
    )
    assert lines == []


def test_other_uvicorn_records_are_kept() -> None:
    """Startup, shutdown and genuine server errors are not duplicates of anything."""
    lines = uvicorn_lines(
        "uvicorn_error.info('Application startup complete.')",
        "uvicorn_error.error('Error loading ASGI app. Could not import module \"nope\".')",
        "uvicorn_error.warning('Invalid HTTP request received.')",
        # A failing startup is a real server error that begins like the duplicate does.
        f"uvicorn_error.error({LIFESPAN_FAILURE!r}, exc_info=ValueError('boom'))",
    )
    messages = [line.split("] ", 1)[1] for line in lines if " uvicorn.error [" in line]
    assert messages == [
        "Application startup complete.",
        'Error loading ASGI app. Could not import module "nope".',
        "Invalid HTTP request received.",
        "Exception in 'lifespan' protocol",
    ]


def test_the_same_text_from_another_logger_is_kept() -> None:
    """Only uvicorn's own logger is filtered: the application's records are never dropped."""
    lines = [line for line in uvicorn_lines(f"other.error({UVICORN_DUPLICATE!r})") if line]
    assert len(lines) == 1  # the message ends in a newline, which prints as an empty line
    assert "some.logger" in lines[0]


def test_it_is_still_dropped_after_configuring_twice() -> None:
    body = f"uvicorn_error.error({UVICORN_DUPLICATE!r})"
    lines = stderr_of(
        "import logging\n"
        "from ledger.infra.logging_config import configure_logging\n"
        "configure_logging()\n"
        "configure_logging()\n"
        "uvicorn_error = logging.getLogger('uvicorn.error')\n"
        f"{body}\n"
        "uvicorn_error.info('still here')\n"
    )
    assert len(lines) == 1
    assert lines[0].endswith("still here")
