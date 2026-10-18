"""The command line of `python -m ledger.seed` (issue #62; docs/design.md, Seed money).

    python -m ledger.seed --output PATH [--currency CODE ...] [--per-currency N]

A bad command line exits with status 2 (argparse's usage error) before the script touches the
database. These tests need no database: the unit-test guard (ADR 0010) makes any attempt to connect
raise, so a script that connected first would fail here with the wrong exception, not with exit 2.

What a good command line does is tested against the database in
tests/integration/service/test_seed.py.
"""

import pytest


@pytest.fixture
def seed():
    """Imported lazily so these tests fail one by one while the module does not exist yet."""
    from ledger import seed as module

    return module


def exit_code(seed, argv: list[str]) -> int:
    with pytest.raises(SystemExit) as exited:
        seed.main(argv)
    assert isinstance(exited.value.code, int)
    return exited.value.code


def test_the_output_file_is_required(seed, capsys: pytest.CaptureFixture[str]) -> None:
    assert exit_code(seed, []) == 2
    assert "--output" in capsys.readouterr().err


@pytest.mark.parametrize(
    "currency",
    ["usd", "Usd", "US", "USDX", "U5D", "", " USD", "USD\n", "ÜSD"],
)
def test_a_currency_that_is_not_three_uppercase_letters_is_a_usage_error(
    seed, tmp_path, currency: str
) -> None:
    """The same rule as the API and the accounts CHECK, caught before any account is created."""
    argv = ["--output", str(tmp_path / "ids.json"), "--currency", currency]

    assert exit_code(seed, argv) == 2


@pytest.mark.parametrize("count", ["0", "-1", "1.5", "two", "", "5\n", "05", "+5"])
def test_a_per_currency_count_below_one_or_not_an_integer_is_a_usage_error(
    seed, tmp_path, count: str
) -> None:
    argv = ["--output", str(tmp_path / "ids.json"), "--per-currency", count]

    assert exit_code(seed, argv) == 2


@pytest.mark.parametrize("count", ["501", "1000", "99999999999999999999999999"])
def test_a_per_currency_count_above_500_is_a_usage_error_and_not_clamped(
    seed, tmp_path, count: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """The limit is 500 accounts per currency. A larger request is refused, not quietly cut down:
    a script that reports success after creating fewer accounts than were asked for would hide the
    difference. 500 itself is allowed (tests/integration/service/test_seed.py)."""
    argv = ["--output", str(tmp_path / "ids.json"), "--per-currency", count]

    assert exit_code(seed, argv) == 2
    assert "500" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_an_unknown_option_is_a_usage_error(seed, tmp_path) -> None:
    argv = ["--output", str(tmp_path / "ids.json"), "--no-such-option"]

    assert exit_code(seed, argv) == 2


def test_a_usage_error_creates_no_file(seed, tmp_path) -> None:
    output = tmp_path / "ids.json"

    exit_code(seed, ["--output", str(output), "--per-currency", "0"])

    assert list(tmp_path.iterdir()) == []
