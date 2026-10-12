"""configure_logging (docs/design.md, Request id and logging).

It sets up the log format once at startup: time, level, logger name, the request id in brackets,
then the message. The id shows as `-` outside a request.

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
        "from ledger.logging_config import configure_logging\n"
        "configure_logging()\n"
        "logging.getLogger('some.logger').warning('hello')\n"
    )
    assert len(lines) == 1
    assert LINE.match(lines[0]), lines[0]


def test_calling_it_twice_does_not_double_the_output() -> None:
    lines = stderr_of(
        "import logging\n"
        "from ledger.logging_config import configure_logging\n"
        "configure_logging()\n"
        "configure_logging()\n"
        "logging.getLogger('some.logger').warning('hello')\n"
    )
    assert len(lines) == 1


def test_info_messages_are_shown() -> None:
    """The level is INFO, so the startup line from the pool-wiring work will be visible."""
    lines = stderr_of(
        "import logging\n"
        "from ledger.logging_config import configure_logging\n"
        "configure_logging()\n"
        "logging.getLogger('some.logger').info('hello')\n"
    )
    assert len(lines) == 1
    assert lines[0].endswith("INFO some.logger [-] hello")
