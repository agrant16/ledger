class LedgerError(Exception):
    pass

class LedgerInvariantError(Exception):
    pass

class DuplicateAccountNameError(LedgerError):
    def __init__(self, name: str):
        self.name = name

class InvalidRequestError(LedgerError):
    pass

class UnknownAccountError(LedgerError):
    pass

class UnknownTransferError(LedgerError):
    pass

class CurrencyMismatchError(LedgerError):
    pass

class InsufficientFundsError(LedgerError):
    pass

class IdempotencyConflictError(LedgerError):
    pass

class AlreadyReversedError(LedgerError):
    pass

class ReversalOfReversalError(LedgerError):
    pass

class ServiceBusyError(LedgerError):
    pass

class UnbalancedEntriesError(LedgerError):
    pass

