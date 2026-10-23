from pydantic import BaseModel


class ProblemFieldError(BaseModel):
    """A problem found while validating a request.

    A problem in the request body has a `pointer`. A problem in the path, a query parameter
    or a header has a `parameter`.
    """

    detail: str
    pointer: str | None = None
    parameter: str | None = None


class ProblemDetails(BaseModel):
    """The RFC 9457 body of an error response (`application/problem+json`).

    Members other than the standard ones appear only on the errors that carry them.
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
