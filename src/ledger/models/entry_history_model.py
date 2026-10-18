from dataclasses import dataclass
from datetime import datetime

from ledger.models.transfer_model import TransferEntry


@dataclass(frozen=True)
class HistoryEntry(TransferEntry):
    transaction_id: int
    created_at: datetime

@dataclass(frozen=True)
class EntryPage:
    entries: tuple[HistoryEntry]
    has_more: bool