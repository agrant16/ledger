"""A connection that fails, or succeeds, on demand, for repository tests that need no database.

`FakeConnection(failure)` makes every statement raise `failure`. `FakeConnection(rowcounts=[1, 0])`
makes the statements succeed, one after another, reporting those row counts. It records the SQL
parameters it was given in `executed`.
"""


class FakeCursor:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection
        self.rowcount = -1

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> bool:
        return False

    def execute(self, sql, params=None):
        self._run(("execute", params))

    def executemany(self, sql, params_seq):
        self._run(("executemany", list(params_seq)))

    def fetchone(self):
        raise AssertionError("fetchone must not be reached after a failed statement")

    def _run(self, record):
        if self.connection.failure is not None:
            raise self.connection.failure
        self.connection.executed.append(record)
        if self.connection.rowcounts:
            self.rowcount = self.connection.rowcounts.pop(0)


class FakeConnection:
    def __init__(self, failure: Exception | None = None, rowcounts: list[int] | None = None):
        self.failure = failure
        self.rowcounts = list(rowcounts or [])
        self.executed: list[tuple] = []

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)
