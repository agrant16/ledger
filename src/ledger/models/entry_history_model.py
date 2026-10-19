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
    entries: tuple[HistoryEntry, ...]
    has_more: bool
