from dataclasses import dataclass
from datetime import datetime

MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50


@dataclass(frozen=True)
class HistoryEntry:
    id: int
    transaction_id: int
    account_id: int
    amount_minor: int
    currency: str
    created_at: datetime


@dataclass(frozen=True)
class EntryPage:
    """One page of an account's entry history, newest first by entry id. (ADR 0014)

    `entries` holds at most the requested `limit` entries. The API layer turns
    `has_more` into `next_cursor`.

    Attributes:
        entries (tuple[HistoryEntry, ...]): A page of entries.
        has_more (bool): Whether older entries exist beyond this page.
    """

    entries: tuple[HistoryEntry, ...]
    has_more: bool
