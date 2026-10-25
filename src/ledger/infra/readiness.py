import math
from dataclasses import dataclass

import psycopg
from psycopg import sql

REQUIRED_PRIVILEGES = {
    "accounts": ("SELECT", "INSERT"),
    "transactions": ("SELECT", "INSERT"),
    "entries": ("SELECT", "INSERT"),
    "balances": ("SELECT", "INSERT", "UPDATE"),
}


@dataclass(frozen=True)
class ReadinessResult:
    """The result of a call to the readiness endpoint.

    Attributes:
        failed_check: The specific readiness check that failed.
        cause: The exception that caused the failure.
        ready: True if no checks failed, False otherwise.
    """

    failed_check: str | None = None
    cause: Exception | None = None
    ready: bool = True if failed_check is None else False


def check_readiness(app_url: str, timeout_seconds: float) -> ReadinessResult:
    stage = "database"
    try:
        with psycopg.connect(
            app_url,
            autocommit=True,
            connect_timeout=max(1, math.ceil(timeout_seconds)),
            options=f"-c statement_timeout={int(timeout_seconds * 1000)}",
        ) as conn:
            conn.execute("SELECT 1;")
            stage = "writable"
            conn.execute("SHOW transaction_read_only;")
            result = conn.fetchone()[0]
            if result != "off":
                return ReadinessResult(stage)

            stage = "table"
            for table in REQUIRED_PRIVILEGES.keys():
                if not _check_table_and_privileges(conn, table, REQUIRED_PRIVILEGES[table]):
                    return ReadinessResult(stage)
    except psycopg.Error as failure:
        return ReadinessResult(stage, failure, False)
    return ReadinessResult()


def _check_table_and_privileges(
    conn: psycopg.Connection, table: str, privileges: tuple[str, ...]
) -> bool:
    conn.execute(sql.SQL("SELECT 1 FROM {} LIMIT 1").format(sql.Identifier(table)))
    conn.execute(
        "SELECT bool_and(has_table_privilege(current_user, %s, p)) FROM unnest(%s::text[]) AS p",
        (table, list(privileges)),
    )
    if not conn.fetchone()[0]:
        return False
    return True
