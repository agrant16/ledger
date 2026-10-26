import math
from dataclasses import dataclass
from typing import Literal

import psycopg
from psycopg import sql
from psycopg_pool import ConnectionPool

REQUIRED_PRIVILEGES = {
    "accounts": ("SELECT", "INSERT"),
    "transactions": ("SELECT", "INSERT"),
    "entries": ("SELECT", "INSERT"),
    "balances": ("SELECT", "INSERT", "UPDATE"),
}

FailedCheck = Literal["database", "writable", "tables"]

NOT_READY_DETAIL: dict[FailedCheck, str] = {
    "database": "The database is not available",
    "writable": "The database is not writable",
    "tables": "Required tables, or privileges, are missing",
}

TABLE_EXISTS_SQL = """SELECT 1 FROM {} LIMIT 1;"""

TABLE_PRIVILEGES_CHECK_SQL = """
                           SELECT 
                               bool_and(has_table_privilege(current_user, %s, p)) 
                           FROM unnest(%s::text[]) AS p;
                           """


@dataclass(frozen=True)
class ReadinessResult:
    """The outcome of the readiness checks.

    Attributes:
        failed_check: The first check that failed, or `None` when all passed.
        cause: The database error behind the failure, or `None` when the database answered
            but the answer was not acceptable (read-only, or a missing grant).
    """

    failed_check: FailedCheck | None
    cause: Exception | None = None

    @property
    def ready(self) -> bool:
        """Whether every check passed."""
        return self.failed_check is None


def pool_state(pool: ConnectionPool) -> Literal["ok", "saturated"]:
    """Reports whether the request pool has a free connection. (ADR 0004)

    Reads the pool's counters and never takes a connection, so it cannot wait or fail.

    Args:
        pool: The request connection pool.

    Returns:
        `"ok"` when at least one connection is free, `"saturated"` when every connection is in use.
    """
    stats = pool.get_stats()
    if stats["pool_available"] == 0:
        return "saturated"
    return "ok"


def check_readiness(app_url: str, timeout_seconds: float) -> ReadinessResult:
    """Checks that the service can do its job against the database. (ADR 0004)

    Opens its own connection, not one from the request pool, runs the checks in order and stops
    at the first failure: the database answers, it accepts writes, and each required table is
    readable and carries the privileges the service needs. It never writes. A database error is
    reported in the result and not raised.

    Args:
        app_url: The connection URL of the app role.
        timeout_seconds: The limit in seconds for the connection and for each statement.

    Returns:
        The result. `ready` is true when every check passed. Otherwise `failed_check` names
        the first check that failed and `cause` holds the database error, if there was one.
    """
    stage: FailedCheck = "database"
    try:
        with psycopg.connect(
            app_url,
            autocommit=True,
            connect_timeout=max(1, math.ceil(timeout_seconds)),
            options=f"-c statement_timeout={int(timeout_seconds * 1000)}",
        ) as conn:
            conn.execute("SELECT 1;")
            stage = "writable"
            read_only = conn.execute("SHOW transaction_read_only;").fetchone()[0]
            if read_only == "on":
                return ReadinessResult(stage)
            stage = "tables"
            for table, privileges in REQUIRED_PRIVILEGES.items():
                if not _check_table_and_privileges(conn, table, privileges):
                    return ReadinessResult(stage)
    except psycopg.Error as failure:
        return ReadinessResult(stage, failure)
    return ReadinessResult(None)


def _check_table_and_privileges(
    conn: psycopg.Connection, table: str, privileges: tuple[str, ...]
) -> bool:
    conn.execute(sql.SQL(TABLE_EXISTS_SQL).format(sql.Identifier(table))).fetchone()
    result = bool(
        conn.execute(
            TABLE_PRIVILEGES_CHECK_SQL,
            (table, list(privileges)),
        ).fetchone()[0]
    )
    return result
