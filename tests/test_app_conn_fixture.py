"""The app_conn fixture rolls back whatever a test wrote through it.

This is a pair of tests, because the claim is about what is left behind after a test has ended.
"""

PROBE = "rollback-probe"


def test_app_conn_writes_are_visible_inside_the_test(clean_db, app_conn) -> None:
    app_conn.execute(
        "INSERT INTO accounts (account_name, currency, allow_negative) VALUES (%s, 'USD', false)", (PROBE,)
    )
    assert app_conn.execute(
        "SELECT count(*) FROM accounts WHERE account_name = %s", (PROBE,)
    ).fetchone() == (1,)


def test_app_conn_writes_are_not_committed_when_the_test_ends(owner_conn) -> None:
    """Runs after the test above and checks that nothing it wrote survived. It deliberately does
    not use clean_db, which would hide a leak. If the order ever changes it can only pass
    vacuously, never fail wrongly."""
    row = owner_conn.execute("SELECT count(*) FROM accounts WHERE account_name = %s", (PROBE,)).fetchone()
    assert row == (0,)
