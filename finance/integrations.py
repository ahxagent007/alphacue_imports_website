"""
finance/integrations.py
───────────────────────
M10 — where the finance ledger meets the rest of the site.

The store and affiliate apps were working long before the ledger existed, so
these hooks are written defensively: every one of them returns a result object
instead of raising, and the caller decides what to do. A missing chart of
accounts must never stop a staff member approving a commission or marking an
order delivered — it should tell them the accounting side did not post, and let
them fix it afterwards.

Each hook is also idempotent. Posting is skipped if a live (non-reversed) entry
already exists for that source object, so a double-clicked button or a retried
request cannot book the same money twice.
"""

import logging

from django.conf import settings
from django.db import transaction as db_transaction

from .exceptions import LedgerError
from .models import Account, Transaction
from .services import get_account, money, post_transaction

logger = logging.getLogger(__name__)

ZERO = money(0)

COMMISSION_EXPENSE_CODE = '5150'
COMMISSION_PAYABLE_CODE = '2100'

#: bKash and Nagad payouts land in the matching wallet account.
PAYMENT_METHOD_ACCOUNTS = {
    'bkash': '1030',
    'nagad': '1040',
}


class HookResult:
    """
    What a hook did. Truthy when it posted, falsy otherwise — but `failed`
    is what callers should check before warning the user, because "skipped
    because it was already posted" is a success, not a problem.
    """

    __slots__ = ('posted', 'transaction', 'skipped_reason', 'error')

    def __init__(self, posted=False, transaction=None, skipped_reason='', error=''):
        self.posted = posted
        self.transaction = transaction
        self.skipped_reason = skipped_reason
        self.error = error

    def __bool__(self):
        return self.posted

    @property
    def failed(self):
        return bool(self.error)

    def __repr__(self):
        if self.error:
            return f'<HookResult error={self.error!r}>'
        if self.posted:
            return f'<HookResult posted={self.transaction.reference_no}>'
        return f'<HookResult skipped={self.skipped_reason!r}>'


def _enabled(flag, default=True):
    return getattr(settings, flag, default)


def already_posted(source_type, source_id):
    """True if a live ledger entry already exists for this source object."""
    return Transaction.objects.filter(
        source_type=source_type, source_id=source_id, is_reversed=False,
    ).exists()


def _guarded(fn):
    """
    Run a hook, turning any ledger problem into a reported failure rather than
    an exception that would roll back the caller's own work.
    """
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except LedgerError as exc:
            logger.warning('finance hook %s did not post: %s', fn.__name__, exc)
            return HookResult(error=str(exc))
        except Exception as exc:                      # noqa: BLE001
            logger.exception('finance hook %s crashed', fn.__name__)
            return HookResult(error=f'Unexpected error: {exc}')
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


# ══════════════════════════════════════════════════════════════════════════════
#  AFFILIATE
# ══════════════════════════════════════════════════════════════════════════════

@_guarded
def on_commission_approved(commission, created_by=None):
    """
    An affiliate's commission has been approved, so the business now owes it.

        Affiliate Commission          +amount   (the cost)
        Affiliate Commission Payable  -amount   (what is owed)

    Nothing is posted while a commission is merely pending — it might still be
    rejected as fraud, and booking a liability for money that may never be owed
    would overstate what the business owes.
    """
    if not _enabled('FINANCE_POST_AFFILIATE'):
        return HookResult(skipped_reason='FINANCE_POST_AFFILIATE is off')

    amount = money(commission.commission_amount)
    if amount <= ZERO:
        return HookResult(skipped_reason='Commission is zero')

    if already_posted(Transaction.SOURCE_AFFILIATE, commission.pk):
        return HookResult(skipped_reason='Already posted')

    with db_transaction.atomic():
        txn = post_transaction(
            date=commission.created_at.date(),
            description=(
                f'Affiliate commission — {commission.affiliate.referral_code} '
                f'(order #{commission.order_id})'
            ),
            lines=[
                (get_account(COMMISSION_EXPENSE_CODE, 'Affiliate Commission'),
                 amount, f'Commission #{commission.pk}'),
                (get_account(COMMISSION_PAYABLE_CODE, 'Affiliate Commission Payable'),
                 -amount, f'Owed to {commission.affiliate.referral_code}'),
            ],
            source_type=Transaction.SOURCE_AFFILIATE,
            source_id=commission.pk,
            created_by=created_by,
        )
    return HookResult(posted=True, transaction=txn)


@_guarded
def on_withdrawal_paid(withdrawal, created_by=None, account=None):
    """
    An affiliate has actually been paid out.

        Affiliate Commission Payable  +amount   (the debt clears)
        bKash / Nagad                 -amount   (money leaves)

    The wallet is picked from the withdrawal's own payment method, so a bKash
    payout shows up as money leaving bKash rather than a generic cash figure.
    """
    if not _enabled('FINANCE_POST_AFFILIATE'):
        return HookResult(skipped_reason='FINANCE_POST_AFFILIATE is off')

    amount = money(withdrawal.amount)
    if amount <= ZERO:
        return HookResult(skipped_reason='Withdrawal is zero')

    # Withdrawals share the affiliate source type with commissions, so the id
    # space is offset to keep them apart.
    source_id = _withdrawal_source_id(withdrawal)
    if already_posted(Transaction.SOURCE_AFFILIATE, source_id):
        return HookResult(skipped_reason='Already posted')

    if account is None:
        code = PAYMENT_METHOD_ACCOUNTS.get(withdrawal.payment_method)
        if code is None:
            return HookResult(
                error=f'No money account is mapped to payment method '
                      f'"{withdrawal.payment_method}".'
            )
        account = get_account(code, 'Payout wallet')

    reference = withdrawal.transaction_id or ''
    with db_transaction.atomic():
        txn = post_transaction(
            date=(withdrawal.processed_at or withdrawal.requested_at).date(),
            description=(
                f'Affiliate payout — {withdrawal.affiliate.referral_code} '
                f'({withdrawal.get_payment_method_display()})'
            ),
            lines=[
                (get_account(COMMISSION_PAYABLE_CODE, 'Affiliate Commission Payable'),
                 amount, f'Withdrawal #{withdrawal.pk}'),
                (account, -amount, reference),
            ],
            source_type=Transaction.SOURCE_AFFILIATE,
            source_id=source_id,
            created_by=created_by,
        )
    return HookResult(posted=True, transaction=txn)


#: Withdrawal ids are offset so they cannot collide with commission ids inside
#: the shared 'affiliate' source type.
WITHDRAWAL_ID_OFFSET = 1_000_000


def _withdrawal_source_id(withdrawal):
    return WITHDRAWAL_ID_OFFSET + withdrawal.pk


def affiliate_commission_owed():
    """What the business currently owes affiliates, straight from the ledger."""
    try:
        return get_account(COMMISSION_PAYABLE_CODE).balance()
    except LedgerError:
        return ZERO


# ══════════════════════════════════════════════════════════════════════════════
#  STORE ORDERS
# ══════════════════════════════════════════════════════════════════════════════

@_guarded
def on_order_delivered(order, created_by=None):
    """
    A website order has been delivered.

    Off by default. Turning `FINANCE_AUTO_INVOICE_ON_DELIVERY` on makes the
    system raise and issue an invoice automatically, which is what closes the
    gap between stock leaving at checkout and revenue being recognised.

    Left off, the owner invoices website orders by hand from the order page —
    which is the right default, because auto-issuing creates numbered documents
    the owner may not want for every small retail sale.
    """
    from .services import create_invoice_from_order, issue_invoice

    if not _enabled('FINANCE_AUTO_INVOICE_ON_DELIVERY', default=False):
        return HookResult(skipped_reason='FINANCE_AUTO_INVOICE_ON_DELIVERY is off')

    from .models import Invoice

    existing = order.invoices.exclude(status=Invoice.STATUS_CANCELLED).first()
    if existing:
        return HookResult(skipped_reason=f'Already invoiced as {existing.display_number}')

    with db_transaction.atomic():
        invoice = create_invoice_from_order(order, created_by=created_by)
        issue_invoice(invoice, created_by=created_by)

    return HookResult(posted=True, transaction=invoice.transaction)


# ══════════════════════════════════════════════════════════════════════════════
#  BACKFILL
# ══════════════════════════════════════════════════════════════════════════════

def backfill_affiliate_history(dry_run=True, created_by=None):
    """
    Post ledger entries for affiliate activity that happened before the finance
    module existed.

    Returns a summary. Nothing is written when `dry_run` is set, so the owner
    can see what would happen before committing to it.
    """
    from affiliate.models import Commission, WithdrawalRequest

    approved = Commission.objects.filter(
        status__in=[Commission.STATUS_APPROVED, Commission.STATUS_PAID],
    ).select_related('affiliate')
    paid_out = WithdrawalRequest.objects.filter(
        status=WithdrawalRequest.STATUS_PAID,
    ).select_related('affiliate')

    summary = {
        'commissions_found': approved.count(),
        'commissions_posted': 0,
        'commissions_skipped': 0,
        'withdrawals_found': paid_out.count(),
        'withdrawals_posted': 0,
        'withdrawals_skipped': 0,
        'errors': [],
        'dry_run': dry_run,
    }

    for commission in approved:
        if already_posted(Transaction.SOURCE_AFFILIATE, commission.pk):
            summary['commissions_skipped'] += 1
            continue
        if dry_run:
            summary['commissions_posted'] += 1
            continue
        result = on_commission_approved(commission, created_by=created_by)
        if result.failed:
            summary['errors'].append(f'Commission #{commission.pk}: {result.error}')
        elif result.posted:
            summary['commissions_posted'] += 1
        else:
            summary['commissions_skipped'] += 1

    for withdrawal in paid_out:
        if already_posted(Transaction.SOURCE_AFFILIATE, _withdrawal_source_id(withdrawal)):
            summary['withdrawals_skipped'] += 1
            continue
        if dry_run:
            summary['withdrawals_posted'] += 1
            continue
        result = on_withdrawal_paid(withdrawal, created_by=created_by)
        if result.failed:
            summary['errors'].append(f'Withdrawal #{withdrawal.pk}: {result.error}')
        elif result.posted:
            summary['withdrawals_posted'] += 1
        else:
            summary['withdrawals_skipped'] += 1

    return summary
