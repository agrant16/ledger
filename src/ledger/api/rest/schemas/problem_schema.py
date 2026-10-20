from pydantic import BaseModel


class ProblemFieldError(BaseModel):
    """One problem found while validating a request: a body problem has a `pointer`, a path,
    query or header problem has a `parameter`."""

    detail: str
    pointer: str | None = None
    parameter: str | None = None


class ProblemDetails(BaseModel):
    """The RFC 9457 body of every error response (`application/problem+json`).

    Documentation only: the error handlers build the body themselves, and nothing validates
    against this model. It is what `/openapi.json` shows clients. The extension members are
    present only on the errors that carry them.
    """

    type: str
    title: str
    status: int
    detail: str
    instance: str
    errors: list[ProblemFieldError] | None = None
    account_id: int | None = None
    balance_minor: int | None = None
    net_change_minor: int | None = None
    account_name: str | None = None
