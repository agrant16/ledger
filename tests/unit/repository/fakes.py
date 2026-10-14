"""A connection that fails, or succeeds, on demand, for repository tests that need no database.

`FakeConnection(failure)` makes every statement raise `failure`, whichever way it is run. The
methods take the same arguments as psycopg's cursor (including `executemany(..., returning=True)`),
so code that uses any of them can be tested for how it handles a failure. It deliberately does not
model results: what a function does with real rows is tested against the real database.
"""


class FakeCursor:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> bool:
        return False

    def execute(self, query, params=None, *, prepare=None, binary=None):
        self._run(("execute", params))

    def executemany(self, query, params_seq, *, returning=False):
        self._run(("executemany", list(params_seq)))

    def fetchone(self):
        raise AssertionError("fetchone must not be reached after a failed statement")

    def _run(self, record):
        if self.connection.failure is not None:
            raise self.connection.failure
        self.connection.executed.append(record)


class FakeConnection:
    def __init__(self, failure: Exception | None = None):
        self.failure = failure
        self.executed: list[tuple] = []

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)
