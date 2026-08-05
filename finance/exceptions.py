"""
finance/exceptions.py
─────────────────────
Errors raised by the ledger. All inherit from LedgerError so callers can
catch the whole family with one except clause.
"""


class LedgerError(Exception):
    """Base class for every ledger rule violation."""


class UnbalancedTransaction(LedgerError):
    """The lines of a transaction do not sum to exactly zero."""


class ImmutableTransaction(LedgerError):
    """Attempt to modify or delete a transaction that has already been posted."""


class InactiveAccount(LedgerError):
    """Attempt to post to an account that has been deactivated."""


class AlreadyReversed(LedgerError):
    """Attempt to reverse a transaction that was already reversed."""
