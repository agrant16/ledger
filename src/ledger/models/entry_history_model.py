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
    """One page of an account's entry history, newest first by entry id.

    `entries` holds at most the requested `limit` entries. The service learns if
    there are more entries by fetching `limit + 1` rows and trimming the extra one,
    so it needs no count query. The API layer turns `has_more` into `next_cursor`,
    encoding the id of the last entry on the page; the next request passes it back
    as `before_id`.

    Attributes:
        entries (tuple[HistoryEntry, ...]): A page of entries.
        has_more (bool): Says whether older entries exist beyond this page.
    """

    entries: tuple[HistoryEntry, ...]
    has_more: bool
