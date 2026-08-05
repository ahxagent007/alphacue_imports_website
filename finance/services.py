"""
finance/services.py
───────────────────
M1 — the only sanctioned way to write to the ledger.

Nothing outside this module should ever create a TransactionLine. Every view,
management command, signal and later milestone posts through
`post_transaction()`, which guarantees:

    • lines sum to exactly zero
    • amounts are Decimal, rounded to 2 places
    • no posting to a deactivated account
    • the whole write happens inside one atomic block

Corrections go through `reverse_transaction()` — the ledger is append-only.

QUICK REFERENCE
───────────────
    post_transaction(date=..., description=..., lines=[...])
    post_simple(date=..., description=..., debit_account=..., credit_account=..., amount=...)
    reverse_transaction(txn, reason='...')

    post_expense(...)                 → money spent on a cost
    post_transfer(...)                → money moved between own accounts
    post_opening_balance(...)         → what an account held on day one

    issue_invoice(invoice)            → draft onto the ledger, number assigned
    cancel_invoice(invoice)           → reverses the posting
    create_invoice_from_order(order)  → website order to draft invoice

    record_payment(...)               → money in, applied to invoices
    receive_purchase(purchase)        → landed cost frozen, stock created
    consume_stock(...)                → FIFO out, cost of sales posted

    account_balance(account)          → what a person would say it holds
    type_balance([Account.TYPE_CASH]) → same, across a set of types
    cash_on_hand()                    → spendable money right now
    daybook(since, as_of)             → the cash book, with a running balance
    trial_balance()                   → every account, and the proof it balances
"""

import calendar
import logging
import secrets
from datetime import date as date_cls, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db import IntegrityError, transaction as db_transaction
from django.db.models import (
    Case, DecimalField, ExpressionWrapper, F, IntegerField, OuterRef, Q,
    Subquery, Sum, Value, When,
)
from django.db.models.functions import Coalesce
from django.utils import timezone

from .exceptions import (
    AlreadyReversed, InactiveAccount, LedgerError, UnbalancedTransaction,
)
from .models import Account, Transaction, TransactionLine, today

logger = logging.getLogger(__name__)

ZERO = Decimal('0.00')
CENTS = Decimal('0.01')

_MONEY_FIELD = DecimalField(max_digits=16, decimal_places=2)


def money(value):
    """Coerce to Decimal rounded to 2 places. The single rounding point."""
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


# ══════════════════════════════════════════════════════════════════════════════
#  POSTING
# ══════════════════════════════════════════════════════════════════════════════

def _normalise_lines(lines):
    """
    Accept lines as dicts or tuples and return a uniform list of
    (account, amount, memo). Rounds amounts and drops nothing.

        {'account': acc, 'amount': 500, 'memo': 'courier'}
        (acc, 500)
        (acc, 500, 'courier')
    """
    normalised = []
    for raw in lines:
        if isinstance(raw, dict):
            account = raw['account']
            amount = raw['amount']
            memo = raw.get('memo', '')
        else:
            if len(raw) == 2:
                account, amount = raw
                memo = ''
            elif len(raw) == 3:
                account, amount, memo = raw
            else:
                raise LedgerError(
                    f"A line must be (account, amount) or (account, amount, memo); got {raw!r}"
                )
        normalised.append((account, money(amount), memo or ''))
    return normalised


@db_transaction.atomic
def post_transaction(*, date, description, lines,
                     source_type='', source_id=None, created_by=None):
    """
    Write one balanced money event to the ledger and return the Transaction.

    `lines` must contain at least two entries whose amounts sum to zero, using
    the debit-positive convention described in finance/models.py.

    Paying a ৳500 courier bill in cash:

        post_transaction(
            date=date.today(),
            description="Courier charge — Sundarban",
            lines=[
                (courier_expense, Decimal('500.00')),
                (cash_in_hand,   Decimal('-500.00')),
            ],
            source_type=Transaction.SOURCE_EXPENSE,
        )

    Raises UnbalancedTransaction if the lines do not sum to zero, and
    InactiveAccount if any account has been deactivated.
    """
    prepared = _normalise_lines(lines)

    if len(prepared) < 2:
        raise LedgerError(
            "A transaction needs at least two lines — money always comes from "
            "somewhere and goes somewhere."
        )

    for account, amount, _memo in prepared:
        if amount == ZERO:
            raise LedgerError(
                f"Line for account {account} has a zero amount. Remove it instead."
            )
        if not account.is_active:
            raise InactiveAccount(
                f"Account {account} is inactive and cannot be posted to."
            )

    total = sum((amount for _acc, amount, _memo in prepared), ZERO)
    if total != ZERO:
        detail = ', '.join(f"{acc.code}: {amt:+,.2f}" for acc, amt, _m in prepared)
        raise UnbalancedTransaction(
            f"Lines must sum to zero but sum to {total:+,.2f} — [{detail}]. "
            f"If this is a rounding remainder, add it to the largest line."
        )

    txn = Transaction.objects.create(
        date=date,
        description=description,
        source_type=source_type or '',
        source_id=source_id,
        created_by=created_by,
    )

    TransactionLine.objects.bulk_create([
        TransactionLine(transaction=txn, account=account, amount=amount, memo=memo)
        for account, amount, memo in prepared
    ])

    return txn


def post_simple(*, date, description, debit_account, credit_account, amount,
                source_type='', source_id=None, created_by=None, memo=''):
    """
    Two-line shorthand for the common case: one account gains, one gives up.

    `debit_account`  receives the positive amount — an expense being incurred,
                     or money arriving in an asset account.
    `credit_account` receives the negative amount — the asset money left, or
                     the income / liability that funded it.

    Recording ৳12,000 received from a client into bKash:

        post_simple(date=..., description="Payment — Rahim Traders",
                    debit_account=bkash, credit_account=ar_rahim,
                    amount=Decimal('12000.00'))
    """
    amount = money(amount)
    if amount <= ZERO:
        raise LedgerError(
            f"Amount must be positive; got {amount}. To move money the other "
            f"way, swap debit_account and credit_account."
        )
    return post_transaction(
        date=date,
        description=description,
        lines=[
            (debit_account, amount, memo),
            (credit_account, -amount, memo),
        ],
        source_type=source_type,
        source_id=source_id,
        created_by=created_by,
    )


@db_transaction.atomic
def reverse_transaction(txn, *, reason='', date=None, created_by=None, force=False):
    """
    Undo a posted transaction by posting its mirror image, and return the new
    Transaction. The original stays in the ledger, marked `is_reversed`.

    The reversal is dated today by default, not backdated to the original —
    the ledger should show when the correction was actually made. Pass `date`
    to override (e.g. to keep a closed month tidy).

    Entries created by a document — an invoice, a payment, a purchase — are
    refused unless `force` is set. Reversing one directly would leave the
    document saying something the ledger no longer agrees with: an invoice
    still reading "Sent" while the client owes nothing. The document's own
    cancel/reverse action does both halves, and passes `force=True` here.
    """
    if txn.is_reversed:
        raise AlreadyReversed(
            f"{txn.reference_no} has already been reversed."
        )

    if not force and txn.source_type in Transaction.MANAGED_SOURCES:
        advice = Transaction.MANAGED_SOURCE_ADVICE.get(
            txn.source_type, 'Undo it from the screen that created it.'
        )
        raise LedgerError(
            f"{txn.reference_no} was created by a {txn.source_type} and cannot "
            f"be reversed from the ledger — doing so would leave that record "
            f"disagreeing with the books. {advice}"
        )

    original_lines = list(txn.lines.select_related('account').all())
    if not original_lines:
        raise LedgerError(f"{txn.reference_no} has no lines to reverse.")

    suffix = f" — {reason}" if reason else ""
    reversal = post_transaction(
        date=date or timezone.now().date(),
        description=f"Reversal of {txn.reference_no}{suffix}"[:255],
        lines=[
            (line.account, -line.amount, line.memo)
            for line in original_lines
        ],
        source_type=Transaction.SOURCE_REVERSAL,
        source_id=txn.pk,
        created_by=created_by,
    )

    reversal.reversal_of = txn
    Transaction.objects.filter(pk=reversal.pk).update(reversal_of=txn)

    txn.is_reversed = True
    txn.save(update_fields=['is_reversed'])

    return reversal


# ══════════════════════════════════════════════════════════════════════════════
#  M2 — EVERYDAY MONEY MOVEMENTS
# ══════════════════════════════════════════════════════════════════════════════

OPENING_BALANCE_CODE = '3900'
PAYMENT_CHARGES_CODE = '5160'


def _require_type(account, allowed, label):
    if account.type not in allowed:
        names = ', '.join(sorted(allowed))
        raise LedgerError(
            f"{label} must be one of [{names}], but {account} is "
            f"'{account.type}'."
        )


def get_opening_balance_account():
    """The counterpart account for starting balances (3900)."""
    try:
        return Account.objects.get(code=OPENING_BALANCE_CODE)
    except Account.DoesNotExist:
        raise LedgerError(
            f"Account {OPENING_BALANCE_CODE} (Opening Balances) is missing. "
            f"Run: python manage.py seed_chart_of_accounts"
        )


def post_expense(*, date, expense_account, paid_from, amount, description,
                 memo='', created_by=None):
    """
    Record money spent. The expense account is the category (Rent, Packaging,
    Marketing…); `paid_from` is the cash, bank or mobile-money account it left.

        Rent            +15,000
        Cash in Hand    -15,000
    """
    _require_type(expense_account, {Account.TYPE_EXPENSE}, 'Expense category')
    _require_type(paid_from, Account.MONEY_TYPES, 'Paid-from account')

    return post_simple(
        date=date,
        description=description,
        debit_account=expense_account,
        credit_account=paid_from,
        amount=amount,
        memo=memo,
        source_type=Transaction.SOURCE_EXPENSE,
        created_by=created_by,
    )


def post_transfer(*, date, from_account, to_account, amount, fee=ZERO,
                  fee_account=None, description='', memo='', created_by=None):
    """
    Move money between two accounts you own — cash to bank, bKash to cash.

    `fee` covers the bKash/Nagad cash-out charge or bank fee. It is taken from
    the source account *on top of* the transferred amount, which is how those
    charges actually work:

        Cash in Hand              +5,000     (received)
        Bank & Payment Charges       +93     (the cash-out fee)
        bKash                     -5,093     (what actually left)
    """
    _require_type(from_account, Account.MONEY_TYPES, 'From account')
    _require_type(to_account, Account.MONEY_TYPES, 'To account')

    if from_account.pk == to_account.pk:
        raise LedgerError("Cannot transfer money to the same account.")

    amount = money(amount)
    fee = money(fee or ZERO)

    if amount <= ZERO:
        raise LedgerError(f"Transfer amount must be positive; got {amount}.")
    if fee < ZERO:
        raise LedgerError(f"Fee cannot be negative; got {fee}.")

    if not description:
        description = f"Transfer — {from_account.name} → {to_account.name}"

    lines = [(to_account, amount, memo)]
    if fee > ZERO:
        if fee_account is None:
            try:
                fee_account = Account.objects.get(code=PAYMENT_CHARGES_CODE)
            except Account.DoesNotExist:
                raise LedgerError(
                    f"Account {PAYMENT_CHARGES_CODE} (Bank & Payment Charges) is "
                    f"missing and no fee_account was given. "
                    f"Run: python manage.py seed_chart_of_accounts"
                )
        _require_type(fee_account, {Account.TYPE_EXPENSE}, 'Fee account')
        lines.append((fee_account, fee, 'Transfer charge'))

    lines.append((from_account, -(amount + fee), memo))

    return post_transaction(
        date=date,
        description=description,
        lines=lines,
        source_type=Transaction.SOURCE_TRANSFER,
        created_by=created_by,
    )


def has_opening_balance(account):
    """True if a starting balance has already been recorded for this account."""
    return account.lines.filter(
        transaction__source_type=Transaction.SOURCE_OPENING,
        transaction__is_reversed=False,
    ).exists()


def post_opening_balance(*, account, date, amount, created_by=None):
    """
    Record what an account already held when the books started, against
    3900 Opening Balances.

    `amount` is stated the natural way — ৳50,000 of cash and ৳20,000 owed to a
    supplier are both entered as positive numbers, and the sign is worked out
    from the account type.
    """
    amount = money(amount)
    if amount == ZERO:
        raise LedgerError("Opening balance cannot be zero.")

    if has_opening_balance(account):
        raise LedgerError(
            f"{account} already has an opening balance. Reverse the existing "
            f"one before entering a different figure."
        )

    counterpart = get_opening_balance_account()
    if account.pk == counterpart.pk:
        raise LedgerError(
            "The Opening Balances account cannot have its own opening balance."
        )

    signed = amount * account.normal_sign

    return post_transaction(
        date=date,
        description=f"Opening balance — {account.name}",
        lines=[
            (account, signed, 'Starting balance'),
            (counterpart, -signed, f'Opening balance for {account.code}'),
        ],
        source_type=Transaction.SOURCE_OPENING,
        created_by=created_by,
    )


def daybook(since=None, as_of=None, account=None):
    """
    The cash book: every movement of spendable money in a date range, in order,
    with a running balance.

    Pass `account` to follow one wallet; leave it out to see all cash, bank and
    mobile money combined. `opening_balance` is the position before `since`, so
    the running balance is true and not just a total of the window.
    """
    if account is not None:
        _require_type(account, Account.MONEY_TYPES, 'Day book account')
        accounts = [account]
    else:
        accounts = list(Account.objects.filter(type__in=Account.MONEY_TYPES))

    account_ids = [a.pk for a in accounts]

    opening = ZERO
    if since is not None and account_ids:
        prior = TransactionLine.objects.filter(
            account_id__in=account_ids,
            transaction__date__lt=since,
        ).aggregate(total=Sum('amount'))['total'] or ZERO
        opening = prior  # money accounts are debit-normal, so raw == natural

    qs = TransactionLine.objects.filter(
        account_id__in=account_ids,
    ).select_related('transaction', 'account').order_by(
        'transaction__date', 'transaction_id', 'id',
    )
    if since is not None:
        qs = qs.filter(transaction__date__gte=since)
    if as_of is not None:
        qs = qs.filter(transaction__date__lte=as_of)

    running = opening
    total_in = ZERO
    total_out = ZERO
    rows = []

    for line in qs:
        running += line.amount
        if line.amount > ZERO:
            total_in += line.amount
        else:
            total_out += -line.amount
        rows.append({
            'line': line,
            'transaction': line.transaction,
            'account': line.account,
            'money_in': line.amount if line.amount > ZERO else None,
            'money_out': -line.amount if line.amount < ZERO else None,
            'running_balance': running,
        })

    return {
        'rows': rows,
        'opening_balance': opening,
        'closing_balance': running,
        'total_in': total_in,
        'total_out': total_out,
        'accounts': accounts,
        'account': account,
        'since': since,
        'as_of': as_of,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  M3 — INVOICING
# ══════════════════════════════════════════════════════════════════════════════

SALES_CODE = '4010'
DELIVERY_INCOME_CODE = '4020'


def get_account(code, label=''):
    """Fetch a standard account by code, with a useful error if it is missing."""
    try:
        return Account.objects.get(code=code)
    except Account.DoesNotExist:
        raise LedgerError(
            f"Account {code}{f' ({label})' if label else ''} is missing. "
            f"Run: python manage.py seed_chart_of_accounts"
        )


def next_invoice_number(year=None, today=None):
    """
    The next number in the INV-YYYY-NNNN series, restarting each January.

    Ordered by id rather than by the number string: once the counter passes
    9999 the text sort would put INV-2026-10000 before INV-2026-9999, but
    insertion order is always right.
    """
    from .models import Invoice

    if year is None:
        year = (today or today()).year

    prefix = f'INV-{year}-'
    last = Invoice.objects.filter(
        number__startswith=prefix,
    ).order_by('-id').values_list('number', flat=True).first()

    sequence = 1
    if last:
        try:
            sequence = int(last.rsplit('-', 1)[1]) + 1
        except (IndexError, ValueError):
            sequence = Invoice.objects.filter(number__startswith=prefix).count() + 1

    return f'{prefix}{sequence:04d}'


def invoice_amount_paid(invoice):
    """
    How much of this invoice has been settled.

    Derived from payment allocations, not stored — so it can never drift from
    the money actually received. A reversed payment stops counting immediately,
    because the filter walks through to the ledger entry behind it.
    """
    from .models import PaymentAllocation

    if not invoice.pk:
        return ZERO

    return PaymentAllocation.objects.filter(
        invoice=invoice,
        payment__transaction__is_reversed=False,
    ).aggregate(total=Sum('amount'))['total'] or ZERO


@db_transaction.atomic
def issue_invoice(invoice, *, issue_date=None, created_by=None):
    """
    Put a draft invoice onto the ledger and freeze it.

    Assigns the invoice number, works out the due date from the payment terms,
    mints the share token, and posts:

        Owed by <client>          +total
        Product Sales             -(goods after discount)
        Delivery Charges          -(delivery charge)

    After this the invoice cannot be edited — only paid or cancelled.
    """
    from .models import Invoice

    if invoice.status != Invoice.STATUS_DRAFT:
        raise LedgerError(
            f"{invoice.display_number} is already {invoice.get_status_display().lower()} "
            f"and cannot be issued again."
        )

    if not invoice.items.exists():
        raise LedgerError("Add at least one line before issuing this invoice.")

    goods = money(invoice.goods_total)
    delivery = money(invoice.delivery_charge)
    total = money(invoice.total)

    if goods < ZERO:
        raise LedgerError(
            f"The discount (৳{invoice.discount}) is larger than the goods total "
            f"(৳{invoice.subtotal}). Reduce the discount before issuing."
        )
    if total <= ZERO:
        raise LedgerError("An invoice must come to more than zero to be issued.")

    receivable = invoice.party.get_receivable_account()
    issue_date = issue_date or invoice.issue_date or today()

    # Claim the number before anything else. Two invoices issued at the same
    # moment would otherwise compute the same one, and the loser would die on
    # the unique constraint after its ledger entry had already been written.
    number = allocate_invoice_number(invoice, year=issue_date.year)

    lines = [(receivable, total, f'Invoice to {invoice.party.name}')]
    if goods > ZERO:
        lines.append((get_account(SALES_CODE, 'Product Sales'), -goods, 'Goods'))
    if delivery > ZERO:
        lines.append(
            (get_account(DELIVERY_INCOME_CODE, 'Delivery Charges Collected'),
             -delivery, 'Delivery charge')
        )

    txn = post_transaction(
        date=issue_date,
        description=f'Invoice {number} — {invoice.party.name}',
        lines=lines,
        source_type=Transaction.SOURCE_INVOICE,
        source_id=invoice.pk,
        created_by=created_by,
    )

    invoice.status = Invoice.STATUS_SENT
    invoice.issue_date = issue_date
    invoice.issued_at = timezone.now()
    invoice.transaction = txn
    if invoice.due_date is None:
        invoice.due_date = issue_date + timedelta(days=invoice.payment_terms_days)
    if not invoice.share_token:
        invoice.share_token = secrets.token_urlsafe(32)

    invoice.save(update_fields=[
        'status', 'issue_date', 'issued_at', 'transaction',
        'due_date', 'share_token', 'updated_at',
    ])
    invoice.number = number

    consume_stock_for_invoice(invoice, created_by=created_by)
    return invoice


def consume_stock_for_invoice(invoice, created_by=None):
    """
    Take the goods on an issued invoice out of stock.

    Skipped for invoices raised from a website order — checkout already took
    that stock, and taking it again would halve the figure for every online
    sale.

    Shortfalls are allowed through on purpose. Selling something the system
    thinks it has none of drives the count negative, which is the signal that
    the product was never received into stock. Blocking the invoice would hide
    the problem instead of showing it.
    """
    from .models import StockMovement

    if invoice.order_id:
        return []

    movements = []
    for item in invoice.items.select_related('variant'):
        if not item.variant_id or item.quantity <= 0:
            continue

        available = variant_stock(item.variant)
        note = ''
        if item.quantity > available:
            note = (
                f'Sold {item.quantity} with {available} on hand — '
                f'this product may never have been received into stock'
            )

        movements.append(consume_stock(
            variant=item.variant,
            quantity=item.quantity,
            reason=StockMovement.REASON_SALE,
            reference=invoice.number or invoice.display_number,
            date=invoice.issue_date,
            note=note,
            created_by=created_by,
            allow_short=True,
            # Issuing an invoice must not fail because the accounts are not
            # set up — the same reasoning as the storefront.
            cogs_required=False,
        ))
    return movements


def return_stock_for_invoice(invoice, created_by=None):
    """Put an invoice's goods back when it is cancelled."""
    from .models import StockMovement

    if invoice.order_id:
        return []

    reference = invoice.number or invoice.display_number
    returned = []

    for movement in StockMovement.objects.filter(
        reference=reference, reason=StockMovement.REASON_SALE,
    ).select_related('variant'):
        already = sum(c.qty_returned for c in movement.consumptions.all())
        sold = sum(c.quantity for c in movement.consumptions.all())
        quantity = -movement.quantity

        returned.append(return_stock(
            variant=movement.variant,
            quantity=quantity,
            reference=reference,
            date=today(),
            note=f'Invoice {reference} cancelled',
            created_by=created_by,
            original_movement=movement if sold > already else None,
        ))
    return returned


def allocate_invoice_number(invoice, year=None):
    """
    Claim the next number in the series for this invoice, retrying if someone
    else takes it first.

    The write goes through `.update()` so the unique constraint is what settles
    the race — whoever commits first keeps the number, and the loser simply
    tries the next one.
    """
    from .models import Invoice

    for attempt in range(8):
        candidate = next_invoice_number(year=year)
        try:
            with db_transaction.atomic():
                Invoice.objects.filter(pk=invoice.pk).update(number=candidate)
            return candidate
        except IntegrityError:
            if attempt == 7:
                raise LedgerError(
                    'Could not allocate an invoice number — too many invoices '
                    'were issued at once. Try again.'
                )
    return None


@db_transaction.atomic
def cancel_invoice(invoice, *, reason='', created_by=None):
    """
    Cancel an invoice. If it was issued, its ledger entry is reversed so the
    client stops owing the money — both entries stay visible.

    An invoice with money already received against it cannot be cancelled:
    refund or reverse the payment first, so the cash position stays honest.
    """
    from .models import Invoice

    if invoice.status == Invoice.STATUS_CANCELLED:
        raise LedgerError(f"{invoice.display_number} is already cancelled.")

    paid = invoice_amount_paid(invoice)
    if paid != ZERO:
        raise LedgerError(
            f"৳{paid:,.2f} has already been received against "
            f"{invoice.display_number}. Reverse the payment before cancelling."
        )

    if invoice.transaction_id and not invoice.transaction.is_reversed:
        reverse_transaction(
            invoice.transaction,
            reason=reason or f'Invoice {invoice.display_number} cancelled',
            created_by=created_by,
            force=True,
        )

    if invoice.status != Invoice.STATUS_DRAFT:
        return_stock_for_invoice(invoice, created_by=created_by)

    invoice.status = Invoice.STATUS_CANCELLED
    invoice.cancelled_at = timezone.now()
    invoice.save(update_fields=['status', 'cancelled_at', 'updated_at'])
    return invoice


def refresh_invoice_status(invoice):
    """
    Move an open invoice between sent / partially paid / paid based on what has
    actually been received. Called by M4 after every payment.
    """
    from .models import Invoice

    if invoice.status not in Invoice.OPEN_STATUSES:
        return invoice

    paid = invoice_amount_paid(invoice)
    total = money(invoice.total)

    if paid >= total:
        status = Invoice.STATUS_PAID
    elif paid > ZERO:
        status = Invoice.STATUS_PARTIAL
    else:
        status = Invoice.STATUS_SENT

    if status != invoice.status:
        invoice.status = status
        invoice.save(update_fields=['status', 'updated_at'])
    return invoice


@db_transaction.atomic
def create_invoice_from_order(order, *, created_by=None, party=None):
    """
    Turn a website order into a draft invoice, copying the customer, the lines
    and the delivery fee. Left as a draft so it can be checked and adjusted
    before it goes onto the ledger.
    """
    from .models import Invoice, InvoiceItem, Party

    existing = order.invoices.exclude(status=Invoice.STATUS_CANCELLED).first()
    if existing:
        raise LedgerError(
            f"Order {order.order_number} already has invoice "
            f"{existing.display_number}. Cancel it first to re-invoice."
        )

    if party is None:
        party = find_or_create_party_for_order(order)

    invoice = Invoice.objects.create(
        party=party,
        order=order,
        issue_date=today(),
        delivery_charge=order.delivery_fee or ZERO,
        notes=f'Website order {order.order_number}',
        created_by=created_by,
    )

    for index, item in enumerate(order.items.all()):
        InvoiceItem.objects.create(
            invoice=invoice,
            variant=item.variant,
            description=f'{item.product_name} — {item.variant_name}'.strip(' —'),
            sku=item.sku,
            unit_price=money(item.unit_price),
            quantity=item.quantity,
            sort_order=index,
        )

    return invoice


def find_or_create_party_for_order(order):
    """
    Match a website order to an existing party by phone, or create a retail
    party for them. Phone is the identifier customers actually reuse — email is
    optional at checkout and names are typed inconsistently.
    """
    from .models import Party

    phone = (order.customer_phone or '').strip()
    if phone:
        existing = Party.objects.filter(phone=phone).order_by('pk').first()
        if existing:
            return existing

    return Party.objects.create(
        name=order.customer_name or 'Website customer',
        party_type=Party.TYPE_RETAIL,
        phone=phone,
        email=order.customer_email or '',
        address=f'{order.address_line}, {order.city}'.strip(', '),
    )


# ══════════════════════════════════════════════════════════════════════════════
#  M4 — PAYMENTS, DUES AND AGEING
# ══════════════════════════════════════════════════════════════════════════════

PAYABLE_CODE = '2010'

#: Ageing buckets, as (label, from_days, to_days). `None` means open-ended.
AGEING_BUCKETS = [
    ('Not yet due', None, -1),
    ('0–30 days',    0,   30),
    ('31–60 days',  31,   60),
    ('61–90 days',  61,   90),
    ('90+ days',    91, None),
]


def with_payment_totals(queryset):
    """
    Annotate invoices with `paid_total` so `amount_paid` costs no extra query.

    Without this a list of 200 invoices ran 200 extra queries to fill in one
    column. `Invoice.amount_paid` picks the annotation up automatically.
    """
    from .models import PaymentAllocation

    paid = (
        PaymentAllocation.objects
        .filter(invoice=OuterRef('pk'), payment__transaction__is_reversed=False)
        .values('invoice')
        .annotate(total=Sum('amount'))
        .values('total')[:1]
    )
    return queryset.annotate(
        paid_total=Coalesce(
            Subquery(paid, output_field=_MONEY_FIELD),
            Value(ZERO),
            output_field=_MONEY_FIELD,
        ),
    )


def with_outstanding(queryset):
    """
    Annotate parties with `outstanding_total`, read straight from their
    receivable account. Receivables are debit-normal, so the raw sum is already
    the figure a person would quote.
    """
    balance = (
        TransactionLine.objects
        .filter(account=OuterRef('receivable_account'))
        .values('account')
        .annotate(total=Sum('amount'))
        .values('total')[:1]
    )
    return queryset.annotate(
        outstanding_total=Coalesce(
            Subquery(balance, output_field=_MONEY_FIELD),
            Value(ZERO),
            output_field=_MONEY_FIELD,
        ),
    )


def open_invoices_for(party, as_of=None):
    """
    This party's unsettled invoices, oldest first — the order payments are
    applied in unless the owner says otherwise.
    """
    from .models import Invoice

    qs = with_payment_totals(party.invoices.filter(status__in=Invoice.OPEN_STATUSES))
    if as_of is not None:
        qs = qs.filter(issue_date__lte=as_of)
    return [
        invoice for invoice in qs.prefetch_related('items').order_by('issue_date', 'id')
        if invoice.amount_due > ZERO
    ]


def auto_allocate(party, amount, as_of=None):
    """
    Spread a payment across the party's oldest open invoices.

    Returns [(invoice, amount), …] and never allocates more than each invoice
    still owes. Anything left over is the caller's advance to deal with.
    """
    remaining = money(amount)
    plan = []

    for invoice in open_invoices_for(party, as_of=as_of):
        if remaining <= ZERO:
            break
        take = min(remaining, money(invoice.amount_due))
        if take > ZERO:
            plan.append((invoice, take))
            remaining -= take

    return plan


@db_transaction.atomic
def record_payment(*, party, date, amount, account, allocations=None,
                   reference='', notes='', created_by=None):
    """
    Record money received from a client and apply it to their invoices.

        Cash / bKash / bank      +amount     (money arrives)
        Owed by <client>         -amount     (they owe that much less)

    `allocations` is [(invoice, amount), …]; leave it out to settle the oldest
    invoices first. Allocating less than the full amount is allowed — the
    remainder sits as an advance on the client's account, which is exactly what
    a deposit is.
    """
    from .models import Invoice, Payment, PaymentAllocation

    amount = money(amount)
    if amount <= ZERO:
        raise LedgerError(f"A payment must be more than zero; got {amount}.")

    _require_type(account, Account.MONEY_TYPES, 'Deposit account')

    if allocations is None:
        allocations = auto_allocate(party, amount)

    allocations = [(invoice, money(value)) for invoice, value in allocations]

    for invoice, value in allocations:
        if value <= ZERO:
            raise LedgerError(
                f"Allocation to {invoice.display_number} must be more than zero."
            )
        if invoice.party_id != party.pk:
            raise LedgerError(
                f"{invoice.display_number} belongs to {invoice.party.name}, "
                f"not {party.name}."
            )
        if invoice.status == Invoice.STATUS_CANCELLED:
            raise LedgerError(
                f"{invoice.display_number} is cancelled and cannot take a payment."
            )
        if invoice.status == Invoice.STATUS_DRAFT:
            raise LedgerError(
                f"{invoice.display_number} has not been issued yet."
            )
        if value > money(invoice.amount_due):
            raise LedgerError(
                f"৳{value:,.2f} is more than the ৳{invoice.amount_due:,.2f} still "
                f"owed on {invoice.display_number}."
            )

    allocated = sum((value for _invoice, value in allocations), ZERO)
    if allocated > amount:
        raise LedgerError(
            f"Allocations come to ৳{allocated:,.2f}, more than the ৳{amount:,.2f} "
            f"received."
        )

    receivable = party.get_receivable_account()

    payment = Payment.objects.create(
        party=party,
        direction=Payment.DIRECTION_IN,
        date=date,
        amount=amount,
        account=account,
        reference=reference,
        notes=notes,
        created_by=created_by,
    )

    for invoice, value in allocations:
        PaymentAllocation.objects.create(
            payment=payment, invoice=invoice, amount=value,
        )

    txn = post_transaction(
        date=date,
        description=f'Payment from {party.name}',
        lines=[
            (account, amount, reference or ''),
            (receivable, -amount, f'Payment from {party.name}'),
        ],
        source_type=Transaction.SOURCE_PAYMENT,
        source_id=payment.pk,
        created_by=created_by,
    )
    Payment.objects.filter(pk=payment.pk).update(transaction=txn)
    payment.transaction = txn

    for invoice, _value in allocations:
        refresh_invoice_status(invoice)

    return payment


@db_transaction.atomic
def reverse_payment(payment, *, reason='', created_by=None):
    """
    Undo a payment — a bounced cheque, a mistaken entry, a refund.

    Reverses the ledger entry and lets every invoice it touched fall back to
    unpaid. The allocations stay in place as history; they simply stop counting
    because the payment behind them is reversed.
    """
    from .models import Invoice

    if not payment.transaction_id:
        raise LedgerError(f"{payment.reference_no} has no ledger entry to reverse.")
    if payment.transaction.is_reversed:
        raise LedgerError(f"{payment.reference_no} has already been reversed.")

    reverse_transaction(
        payment.transaction,
        reason=reason or f'Payment {payment.reference_no} reversed',
        created_by=created_by,
        force=True,
    )
    payment.transaction.refresh_from_db()

    for allocation in payment.allocations.select_related('invoice'):
        invoice = allocation.invoice
        if invoice.status == Invoice.STATUS_PAID:
            invoice.status = Invoice.STATUS_SENT
            invoice.save(update_fields=['status', 'updated_at'])
        refresh_invoice_status(invoice)

    return payment


@db_transaction.atomic
def record_supplier_payment(*, party, date, amount, paid_from, reference='',
                            notes='', created_by=None, payable_account=None):
    """
    Pay a supplier.

        Money We Owe        +amount     (the debt shrinks)
        Cash / bank         -amount     (money leaves)
    """
    from .models import Payment

    amount = money(amount)
    if amount <= ZERO:
        raise LedgerError(f"A payment must be more than zero; got {amount}.")

    _require_type(paid_from, Account.MONEY_TYPES, 'Paid-from account')
    payable = payable_account or get_account(PAYABLE_CODE, 'Money Owed To Suppliers')

    payment = Payment.objects.create(
        party=party,
        direction=Payment.DIRECTION_OUT,
        date=date,
        amount=amount,
        account=paid_from,
        reference=reference,
        notes=notes,
        created_by=created_by,
    )

    txn = post_transaction(
        date=date,
        description=f'Payment to {party.name}',
        lines=[
            (payable, amount, f'Paid {party.name}'),
            (paid_from, -amount, reference or ''),
        ],
        source_type=Transaction.SOURCE_PAYMENT,
        source_id=payment.pk,
        created_by=created_by,
    )
    Payment.objects.filter(pk=payment.pk).update(transaction=txn)
    payment.transaction = txn
    return payment


def party_statement(party, since=None, as_of=None):
    """
    Every invoice and payment for one party in date order, with a running
    balance — what you hand a client who asks "what do I actually owe you?".
    """
    from .models import Invoice

    rows = []

    invoices = party.invoices.exclude(status=Invoice.STATUS_DRAFT).exclude(
        status=Invoice.STATUS_CANCELLED,
    )
    for invoice in invoices:
        rows.append({
            'date': invoice.issue_date,
            'kind': 'invoice',
            'reference': invoice.display_number,
            'description': f'Invoice — {invoice.items.count()} item(s)',
            'charge': money(invoice.total),
            'credit': ZERO,
            'object': invoice,
        })

    for payment in party.payments.filter(direction='in').select_related('transaction'):
        if payment.transaction_id and payment.transaction.is_reversed:
            continue
        rows.append({
            'date': payment.date,
            'kind': 'payment',
            'reference': payment.reference_no,
            'description': f'Payment received{f" — {payment.reference}" if payment.reference else ""}',
            'charge': ZERO,
            'credit': money(payment.amount),
            'object': payment,
        })

    rows.sort(key=lambda row: (row['date'], row['kind'] == 'payment'))

    opening = ZERO
    if since is not None:
        for row in rows:
            if row['date'] < since:
                opening += row['charge'] - row['credit']
        rows = [row for row in rows if row['date'] >= since]
    if as_of is not None:
        rows = [row for row in rows if row['date'] <= as_of]

    running = opening
    for row in rows:
        running += row['charge'] - row['credit']
        row['running_balance'] = running

    return {
        'party': party,
        'rows': rows,
        'opening_balance': opening,
        'closing_balance': running,
        'total_charged': sum((row['charge'] for row in rows), ZERO),
        'total_paid': sum((row['credit'] for row in rows), ZERO),
        'since': since,
        'as_of': as_of,
    }


def ageing_report(as_of=None):
    """
    Outstanding money grouped by how late it is.

    Each open invoice lands in one bucket based on its due date, so the owner
    can see at a glance whether the receivables are healthy or rotting.
    """
    from .models import Invoice

    as_of = as_of or today()

    invoices = with_payment_totals(
        Invoice.objects.filter(status__in=Invoice.OPEN_STATUSES)
    ).select_related('party').prefetch_related('items').order_by('due_date', 'id')

    bucket_totals = {label: ZERO for label, _lo, _hi in AGEING_BUCKETS}
    party_rows = {}
    grand_total = ZERO

    for invoice in invoices:
        due = invoice.amount_due
        if due <= ZERO:
            continue

        if invoice.due_date is None:
            days_late = 0
        else:
            days_late = (as_of - invoice.due_date).days

        label = AGEING_BUCKETS[0][0]
        for bucket_label, low, high in AGEING_BUCKETS:
            if low is None:
                if days_late < 0:
                    label = bucket_label
                    break
                continue
            if days_late >= low and (high is None or days_late <= high):
                label = bucket_label
                break

        bucket_totals[label] += due
        grand_total += due

        row = party_rows.setdefault(invoice.party_id, {
            'party': invoice.party,
            'buckets': {bucket: ZERO for bucket, _lo, _hi in AGEING_BUCKETS},
            'total': ZERO,
            'invoices': [],
        })
        row['buckets'][label] += due
        row['total'] += due
        row['invoices'].append(invoice)

    return {
        'as_of': as_of,
        'buckets': [label for label, _lo, _hi in AGEING_BUCKETS],
        'bucket_totals': bucket_totals,
        'parties': sorted(party_rows.values(), key=lambda r: -r['total']),
        'grand_total': grand_total,
    }


def receivables_summary(as_of=None):
    """Headline numbers for the dues dashboard."""
    from .models import Invoice

    as_of = as_of or today()
    open_qs = Invoice.objects.filter(status__in=Invoice.OPEN_STATUSES)

    overdue_total = ZERO
    open_total = ZERO
    overdue_count = 0

    for invoice in with_payment_totals(open_qs).prefetch_related('items'):
        due = invoice.amount_due
        if due <= ZERO:
            continue
        open_total += due
        if invoice.due_date and invoice.due_date < as_of:
            overdue_total += due
            overdue_count += 1

    return {
        'open_total': open_total,
        'overdue_total': overdue_total,
        'overdue_count': overdue_count,
        'open_count': open_qs.count(),
        'ledger_total': type_balance([Account.TYPE_RECEIVABLE], as_of=as_of),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  M5 — PURCHASING AND LANDED COST
# ══════════════════════════════════════════════════════════════════════════════

INVENTORY_CODE       = '1300'
GOODS_IN_TRANSIT_CODE = '1350'
ACCRUED_LANDED_CODE  = '2150'
COGS_CODE            = '5010'
STOCK_WRITE_OFF_CODE = '5170'


def next_purchase_number():
    """Sequential PUR-#### number, assigned when the purchase is created."""
    from .models import Purchase

    last = Purchase.objects.order_by('-id').values_list(
        'purchase_no', flat=True,
    ).first()

    sequence = 1
    if last:
        try:
            sequence = int(last.rsplit('-', 1)[1]) + 1
        except (IndexError, ValueError):
            sequence = Purchase.objects.count() + 1

    return f'PUR-{sequence:04d}'


def _allocate_with_remainder(total, weights):
    """
    Split `total` across `weights` so the parts add up to exactly `total`.

    Rounding each share independently loses or gains a paisa or two; the
    remainder goes to the largest share, which keeps the ledger balanced and
    the shipment fully allocated.
    """
    total = money(total)
    weight_sum = sum(weights, ZERO)

    if weight_sum <= ZERO or not weights:
        return [ZERO for _ in weights]

    shares = [money(total * (weight / weight_sum)) for weight in weights]
    drift = total - sum(shares, ZERO)

    if drift != ZERO:
        biggest = max(range(len(shares)), key=lambda index: weights[index])
        shares[biggest] += drift

    return shares


def settle_purchase_payment(purchase, created_by=None):
    """
    Pay the supplier for a purchase, if the owner said which account it came
    from when they entered it.

    Runs once — at whichever point the purchase first reaches the ledger, which
    is confirming the order for an import (you pay before it ships) or receiving
    it for a local buy. `purchase.payment` is the guard, so re-confirming or
    re-saving cannot pay twice.
    """
    from .models import Purchase

    if purchase.payment_id or not purchase.paid_from_id:
        return None

    amount = money(purchase.amount_paid)
    if amount <= ZERO:
        return None

    payment = record_supplier_payment(
        party=purchase.supplier,
        date=purchase.purchase_date,
        amount=amount,
        paid_from=purchase.paid_from,
        reference=purchase.purchase_no,
        notes=f'Paid against purchase {purchase.purchase_no}',
        created_by=created_by,
    )
    Purchase.objects.filter(pk=purchase.pk).update(payment=payment)
    purchase.payment = payment
    return payment


@db_transaction.atomic
def mark_purchase_ordered(purchase, *, created_by=None):
    """
    Confirm a purchase: the goods are paid for or owed, but not here yet.

        Goods in Transit        +goods
        Money We Owe            -goods
    """
    from .models import Purchase

    if purchase.status != Purchase.STATUS_DRAFT:
        raise LedgerError(
            f"{purchase.purchase_no} is already "
            f"{purchase.get_status_display().lower()}."
        )
    if not purchase.items.exists():
        raise LedgerError("Add at least one product before confirming this purchase.")

    goods = money(purchase.goods_total_bdt)
    if goods <= ZERO:
        raise LedgerError(
            "The goods value comes to zero. Check the prices"
            f"{' and the FX rate' if purchase.is_import else ''}."
        )

    txn = post_transaction(
        date=purchase.purchase_date,
        description=f'Purchase {purchase.purchase_no} ordered — {purchase.supplier.name}',
        lines=[
            (get_account(GOODS_IN_TRANSIT_CODE, 'Goods in Transit'), goods, 'Goods ordered'),
            (get_account(PAYABLE_CODE, 'Money Owed To Suppliers'), -goods,
             f'Owed to {purchase.supplier.name}'),
        ],
        source_type=Transaction.SOURCE_PURCHASE,
        source_id=purchase.pk,
        created_by=created_by,
    )

    purchase.status = Purchase.STATUS_ORDERED
    purchase.order_transaction = txn
    purchase.save(update_fields=['status', 'order_transaction', 'updated_at'])

    settle_purchase_payment(purchase, created_by=created_by)
    return purchase


@db_transaction.atomic
def receive_purchase(purchase, *, received_date=None, created_by=None):
    """
    Land the shipment: freeze the landed cost, create stock batches, and move
    the value onto the ledger.

        Stock on Hand            +landed total
        Goods in Transit         -goods already booked in transit
        Money We Owe             -(freight + extra costs)
        Accrued Landed Costs     -(the correction buffer)

    The correction goes to an accrual rather than straight to payables: it is
    money you expect to spend on bank charges, breakage and small unknowns, not
    an invoice anybody has sent you.
    """
    from .models import Purchase, StockBatch, StockMovement

    if purchase.status == Purchase.STATUS_RECEIVED:
        raise LedgerError(f"{purchase.purchase_no} has already been received.")
    if purchase.status == Purchase.STATUS_CANCELLED:
        raise LedgerError(f"{purchase.purchase_no} was cancelled.")

    items = list(purchase.items.select_related('variant'))
    if not items:
        raise LedgerError("This purchase has no products on it.")

    if purchase.is_import and purchase.fx_rate_rmb_to_bdt <= ZERO:
        raise LedgerError("Enter the RMB exchange rate before receiving.")
    if purchase.is_import and purchase.effective_billed_weight <= ZERO:
        raise LedgerError(
            "Enter the weights and the agent's billed weight before receiving — "
            "the shipping cost cannot be worked out without them."
        )

    received_date = received_date or purchase.received_date or today()

    if sum((money(item.landed_total_bdt) for item in items), ZERO) <= ZERO:
        raise LedgerError("The landed cost comes to zero. Check the figures.")

    # FIFO needs a per-unit cost, but a line total rarely divides evenly by the
    # quantity — ৳11,313.75 across 10 units rounds to ৳1,131.38 each, which
    # multiplies back to ৳11,313.80. The batch value is what stock is actually
    # worth, so that is what goes on the ledger; the few paisa of difference is
    # absorbed by the supplier line rather than left to drift.
    unit_costs = [
        money(money(item.landed_total_bdt) / item.quantity) if item.quantity else ZERO
        for item in items
    ]
    batch_values = [
        money(unit_cost * item.quantity)
        for unit_cost, item in zip(unit_costs, items)
    ]
    inventory_total = sum(batch_values, ZERO)

    goods_in_transit = money(purchase.goods_total_bdt) if purchase.order_transaction_id else ZERO
    correction = money(purchase.correction_amount_bdt)
    supplier_side = inventory_total - goods_in_transit - correction

    lines = [(get_account(INVENTORY_CODE, 'Stock on Hand'), inventory_total, 'Stock received')]
    if goods_in_transit > ZERO:
        lines.append(
            (get_account(GOODS_IN_TRANSIT_CODE, 'Goods in Transit'),
             -goods_in_transit, 'Goods arrived')
        )
    if supplier_side != ZERO:
        lines.append(
            (get_account(PAYABLE_CODE, 'Money Owed To Suppliers'),
             -supplier_side, 'Goods and shipping owed')
        )
    if correction > ZERO:
        lines.append(
            (get_account(ACCRUED_LANDED_CODE, 'Accrued Landed Costs'),
             -correction, f'{purchase.correction_percent}% buffer')
        )

    txn = post_transaction(
        date=received_date,
        description=f'Purchase {purchase.purchase_no} received — {purchase.supplier.name}',
        lines=lines,
        source_type=Transaction.SOURCE_PURCHASE,
        source_id=purchase.pk,
        created_by=created_by,
    )

    for item, unit_cost in zip(items, unit_costs):
        batch = StockBatch.objects.create(
            variant=item.variant,
            purchase_item=item,
            unit_cost=unit_cost,
            qty_received=item.quantity,
            qty_remaining=item.quantity,
            received_date=received_date,
        )
        StockMovement.objects.create(
            variant=item.variant,
            date=received_date,
            quantity=item.quantity,
            reason=StockMovement.REASON_PURCHASE,
            reference=purchase.purchase_no,
            note=f'Landed cost ৳{unit_cost:,.2f} each',
            batch=batch,
            transaction=txn,
            created_by=created_by,
        )
        sync_variant_stock(item.variant)

    purchase.status = Purchase.STATUS_RECEIVED
    purchase.received_date = received_date
    purchase.receipt_transaction = txn
    purchase.save(update_fields=[
        'status', 'received_date', 'receipt_transaction', 'updated_at',
    ])

    # Covers a purchase received without being confirmed first — a local buy
    # that was paid for and carried home the same day.
    settle_purchase_payment(purchase, created_by=created_by)
    return purchase


@db_transaction.atomic
def cancel_purchase(purchase, *, reason='', created_by=None):
    """
    Cancel a purchase, reversing whatever has been posted so far.

    A received purchase cannot be cancelled once its stock has started selling —
    unpicking that would rewrite the cost of sales that already happened.
    """
    from .models import Purchase

    if purchase.status == Purchase.STATUS_CANCELLED:
        raise LedgerError(f"{purchase.purchase_no} is already cancelled.")

    if purchase.status == Purchase.STATUS_RECEIVED:
        for batch in purchase_batches(purchase):
            if batch.qty_used:
                raise LedgerError(
                    f"Stock from {purchase.purchase_no} has already been sold. "
                    f"Record a stock adjustment instead of cancelling it."
                )

    for txn in (purchase.receipt_transaction, purchase.order_transaction):
        if txn and not txn.is_reversed:
            reverse_transaction(
                txn,
                reason=reason or f'Purchase {purchase.purchase_no} cancelled',
                created_by=created_by,
                force=True,
            )

    # Take the units back out of the movement history too. Zeroing the batch
    # alone would leave the stock ledger still showing them as received, and
    # ProductVariant.stock is summed from movements.
    from .models import StockMovement

    for batch in purchase_batches(purchase):
        variant = batch.variant
        if batch.qty_remaining:
            StockMovement.objects.create(
                variant=variant,
                date=today(),
                quantity=-batch.qty_remaining,
                reason=StockMovement.REASON_ADJUST,
                reference=purchase.purchase_no,
                note=f'Purchase cancelled — {reason}' if reason else 'Purchase cancelled',
                created_by=created_by,
            )
            batch.qty_remaining = 0
            batch.save(update_fields=['qty_remaining'])
        sync_variant_stock(variant)

    purchase.status = Purchase.STATUS_CANCELLED
    purchase.save(update_fields=['status', 'updated_at'])
    return purchase


def purchase_batches(purchase):
    """Every stock batch this purchase created."""
    from .models import StockBatch
    return StockBatch.objects.filter(
        purchase_item__purchase=purchase,
    ).select_related('variant')


def margin_report(only_active=True):
    """
    Landed cost against selling price, per variant.

    Reads the weighted average of the stock actually on hand, so it answers
    "what is the stuff in my warehouse costing me right now?" rather than what
    the last shipment happened to cost.
    """
    from store.models import ProductVariant
    from .models import StockBatch

    variants = list(ProductVariant.objects.select_related('product'))
    if only_active:
        variants = [v for v in variants if v.is_active]

    # One pass over every batch rather than two queries per product — the old
    # shape ran 2N queries and made the catalogue and margin pages crawl.
    on_hand = {}
    last_cost = {}
    for batch in StockBatch.objects.filter(
        variant_id__in=[v.pk for v in variants],
    ).order_by('variant_id', 'received_date', 'id').only(
        'variant_id', 'unit_cost', 'qty_remaining',
    ):
        last_cost[batch.variant_id] = batch.unit_cost
        if batch.qty_remaining:
            entry = on_hand.setdefault(batch.variant_id, {'qty': 0, 'value': ZERO})
            entry['qty'] += batch.qty_remaining
            entry['value'] += batch.unit_cost * batch.qty_remaining

    rows = []
    for variant in variants:
        held = on_hand.get(variant.pk)
        qty = held['qty'] if held else 0
        value = held['value'] if held else ZERO

        if qty:
            avg_cost = money(value / qty)
        else:
            avg_cost = last_cost.get(variant.pk, ZERO)

        margin = variant.price - avg_cost
        margin_pct = (
            (margin / variant.price * Decimal('100')).quantize(Decimal('0.1'))
            if variant.price else ZERO
        )

        rows.append({
            'variant': variant,
            'product': variant.product,
            'price': variant.price,
            'cost': avg_cost,
            'margin': margin,
            'margin_percent': margin_pct,
            'qty_on_hand': qty,
            'stock_value': value,
            'has_cost': avg_cost > ZERO,
        })

    rows.sort(key=lambda row: (row['has_cost'], -row['margin_percent']))
    return rows


# ══════════════════════════════════════════════════════════════════════════════
#  M6 — STOCK, BATCHES AND FIFO
# ══════════════════════════════════════════════════════════════════════════════

def variant_stock(variant):
    """Stock on hand, summed from the movement history."""
    from .models import StockMovement
    return StockMovement.objects.filter(variant=variant).aggregate(
        total=Sum('quantity'),
    )['total'] or 0


def sync_variant_stock(variant):
    """
    Refresh the cached `ProductVariant.stock` from the movements.

    The catalogue reads that integer on every product page, so it stays
    denormalised — but it is derived, and `reconcile_stock` can rebuild it.
    """
    from store.models import ProductVariant

    total = max(0, variant_stock(variant))
    ProductVariant.objects.filter(pk=variant.pk).update(stock=total)
    variant.stock = total
    return total


def available_batches(variant):
    """Batches with stock left, oldest first — the FIFO queue."""
    from .models import StockBatch
    return StockBatch.objects.filter(
        variant=variant, qty_remaining__gt=0,
    ).order_by('received_date', 'id')


@db_transaction.atomic
def receive_opening_stock(*, variant, quantity, unit_cost, date=None,
                          created_by=None, post_to_ledger=True):
    """
    Stock you already had when the system started, at whatever it cost you.
    Creates a batch so FIFO has something to consume.
    """
    from .models import StockBatch, StockMovement

    quantity = int(quantity)
    unit_cost = money(unit_cost)

    if quantity <= 0:
        raise LedgerError("Opening stock quantity must be more than zero.")
    if unit_cost < ZERO:
        raise LedgerError("Cost cannot be negative.")

    date = date or today()
    txn = None

    if post_to_ledger and unit_cost > ZERO:
        value = money(unit_cost * quantity)
        txn = post_transaction(
            date=date,
            description=f'Opening stock — {variant}',
            lines=[
                (get_account(INVENTORY_CODE, 'Stock on Hand'), value, 'Opening stock'),
                (get_opening_balance_account(), -value, f'Opening stock for {variant}'),
            ],
            source_type=Transaction.SOURCE_OPENING,
            created_by=created_by,
        )

    batch = StockBatch.objects.create(
        variant=variant, unit_cost=unit_cost,
        qty_received=quantity, qty_remaining=quantity, received_date=date,
    )
    StockMovement.objects.create(
        variant=variant, date=date, quantity=quantity,
        reason=StockMovement.REASON_OPENING,
        note='Opening stock', batch=batch, transaction=txn,
        created_by=created_by,
    )
    sync_variant_stock(variant)
    return batch


@db_transaction.atomic
def consume_stock(*, variant, quantity, reason, reference='', date=None,
                  note='', created_by=None, post_cogs=True, allow_short=False,
                  cogs_required=True):
    """
    Take stock out, oldest batch first. The single path for every outbound
    movement — sales, damage, write-offs.

    Returns the StockMovement, whose `total_cost` is what those units actually
    cost, taken from the batches consumed rather than an average.

    Raises if there is not enough stock, unless `allow_short` is set — which
    exists for corrections, not for selling stock you do not have.

    `cogs_required=False` lets the movement stand even when the cost cannot be
    posted, which is what the storefront needs: a missing account should not
    stop a customer buying. The shortfall is written into the movement's note
    so it can be found and fixed rather than silently lost.
    """
    from .models import StockConsumption, StockMovement

    quantity = int(quantity)
    if quantity <= 0:
        raise LedgerError(f"Quantity must be more than zero; got {quantity}.")

    date = date or today()

    # Lock the batches *before* deciding whether there is enough. Reading the
    # figure first left a window where two sales of the last unit both saw it
    # available and both went through.
    batches = list(
        available_batches(variant).select_for_update()
    )
    available = sum(batch.qty_remaining for batch in batches)

    if quantity > available and not allow_short:
        raise LedgerError(
            f"Only {available} of {variant} in stock, but {quantity} requested."
        )

    movement = StockMovement.objects.create(
        variant=variant, date=date, quantity=-quantity, reason=reason,
        reference=reference, note=note, created_by=created_by,
    )

    outstanding = quantity
    total_cost = ZERO

    for batch in batches:
        if outstanding <= 0:
            break
        take = min(outstanding, batch.qty_remaining)
        if take <= 0:
            continue

        StockConsumption.objects.create(
            movement=movement, batch=batch, quantity=take, unit_cost=batch.unit_cost,
        )
        batch.qty_remaining -= take
        batch.save(update_fields=['qty_remaining'])

        total_cost += batch.unit_cost * take
        outstanding -= take

    total_cost = money(total_cost)

    if post_cogs and total_cost > ZERO:
        expense_code = (
            STOCK_WRITE_OFF_CODE
            if reason == StockMovement.REASON_DAMAGE
            else COGS_CODE
        )
        try:
            with db_transaction.atomic():
                txn = post_transaction(
                    date=date,
                    description=f'{movement.get_reason_display()} — {variant}',
                    lines=[
                        (get_account(expense_code, 'Cost of Goods Sold'), total_cost,
                         f'{quantity} x {variant}'),
                        (get_account(INVENTORY_CODE, 'Stock on Hand'), -total_cost,
                         'Stock consumed'),
                    ],
                    source_type=Transaction.SOURCE_STOCK,
                    source_id=movement.pk,
                    created_by=created_by,
                )
        except LedgerError:
            # The stock genuinely moved. Refusing to record that because the
            # accounts are misconfigured would be worse than recording it and
            # flagging the gap — especially on the storefront, where raising
            # here would fail the customer's checkout.
            if cogs_required:
                raise
            shortfall = f'Cost of ৳{total_cost:,.2f} not posted — check the chart of accounts'
            movement.note = f'{movement.note} · {shortfall}'.strip(' ·')[:255]
            StockMovement.objects.filter(pk=movement.pk).update(note=movement.note)
            logger.warning(
                'Stock movement %s recorded without its cost: %s',
                movement.pk, shortfall,
            )
        else:
            StockMovement.objects.filter(pk=movement.pk).update(transaction=txn)
            movement.transaction = txn

    sync_variant_stock(variant)
    return movement


@db_transaction.atomic
def return_stock(*, variant, quantity, reference='', date=None, note='',
                 created_by=None, original_movement=None):
    """
    Put stock back after a customer return.

    Where the original sale is known, the units go back to the batches they
    came from at their original cost — so a return does not quietly revalue
    your stock. Otherwise they land in a new batch at the current average.
    """
    from .models import StockBatch, StockMovement

    quantity = int(quantity)
    if quantity <= 0:
        raise LedgerError("Return quantity must be more than zero.")

    date = date or today()
    outstanding = quantity
    restored_cost = ZERO

    if original_movement is not None:
        already = sum(c.qty_returned for c in original_movement.consumptions.all())
        sold = sum(c.quantity for c in original_movement.consumptions.all())
        if already + quantity > sold:
            raise LedgerError(
                f"Only {sold - already} of that sale can still be returned "
                f"({sold} sold, {already} already returned)."
            )

        for consumption in original_movement.consumptions.select_related('batch'):
            if outstanding <= 0:
                break
            give_back = min(outstanding, consumption.qty_returnable)
            if give_back <= 0:
                continue

            batch = consumption.batch
            batch.qty_remaining += give_back
            batch.save(update_fields=['qty_remaining'])

            consumption.qty_returned += give_back
            consumption.save(update_fields=['qty_returned'])

            restored_cost += consumption.unit_cost * give_back
            outstanding -= give_back

    batch = None
    if outstanding > 0:
        unit_cost = current_unit_cost(variant)
        batch = StockBatch.objects.create(
            variant=variant, unit_cost=unit_cost,
            qty_received=outstanding, qty_remaining=outstanding, received_date=date,
        )
        restored_cost += unit_cost * outstanding

    restored_cost = money(restored_cost)

    movement = StockMovement.objects.create(
        variant=variant, date=date, quantity=quantity,
        reason=StockMovement.REASON_RETURN,
        reference=reference, note=note, batch=batch, created_by=created_by,
    )

    if restored_cost > ZERO:
        txn = post_transaction(
            date=date,
            description=f'Customer return — {variant}',
            lines=[
                (get_account(INVENTORY_CODE, 'Stock on Hand'), restored_cost,
                 'Stock returned'),
                (get_account(COGS_CODE, 'Cost of Goods Sold'), -restored_cost,
                 'Reversing cost of sale'),
            ],
            source_type=Transaction.SOURCE_STOCK,
            source_id=movement.pk,
            created_by=created_by,
        )
        StockMovement.objects.filter(pk=movement.pk).update(transaction=txn)
        movement.transaction = txn

    sync_variant_stock(variant)
    return movement


def current_unit_cost(variant):
    """
    What one unit of this product is currently worth — the weighted average of
    the batches on hand, falling back to the last known cost.
    """
    batches = list(available_batches(variant))
    qty = sum(batch.qty_remaining for batch in batches)
    if qty:
        value = sum((batch.value_remaining for batch in batches), ZERO)
        return money(value / qty)

    from .models import StockBatch
    latest = StockBatch.objects.filter(variant=variant).order_by(
        '-received_date', '-id',
    ).first()
    return latest.unit_cost if latest else ZERO


@db_transaction.atomic
def adjust_stock(*, variant, quantity, reason, note='', date=None,
                 created_by=None, unit_cost=None):
    """
    Correct the stock figure by hand — a stocktake difference, breakage found
    in the box, units that never arrived.

    Positive adds a batch at `unit_cost` (or the current average); negative
    consumes FIFO like any other outbound movement.
    """
    from .models import StockBatch, StockMovement

    quantity = int(quantity)
    if quantity == 0:
        raise LedgerError("An adjustment of zero changes nothing.")

    date = date or today()

    if quantity < 0:
        return consume_stock(
            variant=variant, quantity=-quantity, reason=reason,
            date=date, note=note, created_by=created_by, allow_short=True,
        )

    cost = money(unit_cost) if unit_cost is not None else current_unit_cost(variant)
    value = money(cost * quantity)

    batch = StockBatch.objects.create(
        variant=variant, unit_cost=cost,
        qty_received=quantity, qty_remaining=quantity, received_date=date,
    )
    txn = None
    if value > ZERO:
        txn = post_transaction(
            date=date,
            description=f'Stock adjustment — {variant}',
            lines=[
                (get_account(INVENTORY_CODE, 'Stock on Hand'), value, note or 'Adjustment'),
                (get_account(STOCK_WRITE_OFF_CODE, 'Stock Write-off & Damage'),
                 -value, 'Adjustment gain'),
            ],
            source_type=Transaction.SOURCE_STOCK,
            created_by=created_by,
        )

    movement = StockMovement.objects.create(
        variant=variant, date=date, quantity=quantity, reason=reason,
        note=note, batch=batch, transaction=txn, created_by=created_by,
    )
    sync_variant_stock(variant)
    return movement


def stock_cost_history(variant):
    """
    The per-shipment cost table — what each purchase of this product cost, and
    how much of it is left.
    """
    from .models import StockBatch

    rows = []
    for batch in StockBatch.objects.filter(variant=variant).order_by('received_date', 'id'):
        rows.append({
            'batch': batch,
            'reference': batch.source_reference,
            'date': batch.received_date,
            'qty_received': batch.qty_received,
            'qty_remaining': batch.qty_remaining,
            'unit_cost': batch.unit_cost,
            'value_remaining': batch.value_remaining,
        })
    return rows


def stock_valuation():
    """Total value of everything on hand, and the per-product breakdown."""
    from .models import StockBatch

    rows = {}
    total_value = ZERO
    total_units = 0

    for batch in StockBatch.objects.filter(
        qty_remaining__gt=0,
    ).select_related('variant__product'):
        row = rows.setdefault(batch.variant_id, {
            'variant': batch.variant,
            'quantity': 0,
            'value': ZERO,
        })
        row['quantity'] += batch.qty_remaining
        row['value'] += batch.value_remaining
        total_value += batch.value_remaining
        total_units += batch.qty_remaining

    for row in rows.values():
        row['unit_cost'] = (
            money(row['value'] / row['quantity']) if row['quantity'] else ZERO
        )

    return {
        'rows': sorted(rows.values(), key=lambda row: -row['value']),
        'total_value': total_value,
        'total_units': total_units,
        'ledger_value': type_balance([Account.TYPE_INVENTORY]),
    }


def oversold_variants():
    """
    Products whose movement history has gone negative — sold more than was
    ever received.

    Almost always means the product was never entered into stock rather than
    that anything was stolen, which is why it is worth surfacing loudly: the
    fix is to record the purchase or the opening stock.
    """
    from store.models import ProductVariant
    from .models import StockMovement

    negative = (
        StockMovement.objects
        .values('variant')
        .annotate(total=Sum('quantity'))
        .filter(total__lt=0)
    )
    shortfalls = {row['variant']: row['total'] for row in negative}
    if not shortfalls:
        return []

    variants = ProductVariant.objects.filter(
        pk__in=shortfalls,
    ).select_related('product')

    rows = [
        {'variant': variant, 'quantity': shortfalls[variant.pk]}
        for variant in variants
    ]
    rows.sort(key=lambda row: row['quantity'])
    return rows


def low_stock(threshold=None):
    """Products at or below the reorder level, worst first."""
    from django.conf import settings
    from store.models import ProductVariant

    if threshold is None:
        threshold = getattr(settings, 'FINANCE_LOW_STOCK_THRESHOLD', 5)

    return list(
        ProductVariant.objects.filter(
            is_active=True, track_stock=True, stock__lte=threshold,
        ).select_related('product').order_by('stock')[:50]
    )


# ══════════════════════════════════════════════════════════════════════════════
#  M7 — INVESTORS
# ══════════════════════════════════════════════════════════════════════════════

RETAINED_EARNINGS_CODE = '3100'
INTEREST_EXPENSE_CODE  = '5180'
INTEREST_INCOME_CODE   = '4910'


def investor_capital_in(investor):
    """
    Money the investor has actually put in.

    Filtered to capital movements only — a profit share also credits this
    account, and counting that as contributed capital would inflate their
    ownership percentage every time profit was distributed.
    """
    if not investor.equity_account_id:
        return ZERO
    total = TransactionLine.objects.filter(
        account_id=investor.equity_account_id,
        amount__lt=ZERO,
        transaction__source_type=Transaction.SOURCE_INVESTOR,
        transaction__is_reversed=False,
    ).aggregate(total=Sum('amount'))['total'] or ZERO
    return -total


def investor_drawings(investor):
    """Total the investor has taken back out."""
    if not investor.equity_account_id:
        return ZERO
    return TransactionLine.objects.filter(
        account_id=investor.equity_account_id,
        amount__gt=ZERO,
        transaction__source_type=Transaction.SOURCE_INVESTOR,
        transaction__is_reversed=False,
    ).aggregate(total=Sum('amount'))['total'] or ZERO


@db_transaction.atomic
def record_capital(*, investor, date, amount, account, notes='', created_by=None):
    """
    Money the investor puts into the business.

        Cash / bank         +amount
        Capital — <name>    -amount     (their claim grows)
    """
    amount = money(amount)
    if amount <= ZERO:
        raise LedgerError(f"Capital must be more than zero; got {amount}.")
    _require_type(account, Account.MONEY_TYPES, 'Deposit account')

    return post_simple(
        date=date,
        description=f'Capital from {investor.name}'
                    + (f' — {notes}' if notes else ''),
        debit_account=account,
        credit_account=investor.get_equity_account(),
        amount=amount,
        source_type=Transaction.SOURCE_INVESTOR,
        source_id=investor.pk,
        created_by=created_by,
    )


@db_transaction.atomic
def record_drawing(*, investor, date, amount, account, notes='', created_by=None):
    """
    Money the investor takes out — a withdrawal against their stake.

        Capital — <name>    +amount     (their claim shrinks)
        Cash / bank         -amount
    """
    amount = money(amount)
    if amount <= ZERO:
        raise LedgerError(f"A drawing must be more than zero; got {amount}.")
    _require_type(account, Account.MONEY_TYPES, 'Paid-from account')

    return post_simple(
        date=date,
        description=f'Drawing by {investor.name}'
                    + (f' — {notes}' if notes else ''),
        debit_account=investor.get_equity_account(),
        credit_account=account,
        amount=amount,
        source_type=Transaction.SOURCE_INVESTOR,
        source_id=investor.pk,
        created_by=created_by,
    )


def ownership_split():
    """
    Each active investor's share of the business.

    Uses the percentage set by hand where there is one; otherwise works it out
    from capital contributed. Returns [(investor, percent), …] summing to 100
    when anyone has invested at all.
    """
    from .models import Investor

    investors = list(Investor.objects.filter(is_active=True))
    if not investors:
        return []

    manual = [inv for inv in investors if inv.ownership_percent is not None]
    if manual and len(manual) == len(investors):
        return [(inv, inv.ownership_percent) for inv in investors]

    contributions = [(inv, inv.capital_in) for inv in investors]
    total = sum((value for _inv, value in contributions), ZERO)

    if total <= ZERO:
        return [(inv, ZERO) for inv in investors]

    return [
        (inv, (value / total * Decimal('100')).quantize(Decimal('0.001')))
        for inv, value in contributions
    ]


@db_transaction.atomic
def distribute_profit(*, period_start, period_end, distribution_date=None,
                      amount=None, notes='', created_by=None):
    """
    Share out profit for a period between the investors.

    Moves value from Retained Earnings into each investor's capital account —
    it makes the claim real without moving any cash. Paying it out is a
    separate drawing, which is how it works in practice.
    """
    from .models import ProfitDistribution, ProfitShare

    distribution_date = distribution_date or today()

    result = period_profit(since=period_start, as_of=period_end)
    net_profit = money(result['profit'])
    to_share = money(amount) if amount is not None else net_profit

    if to_share <= ZERO:
        raise LedgerError(
            f"There is no profit to share for this period "
            f"(net ৳{net_profit:,.2f})."
        )

    split = [(inv, percent) for inv, percent in ownership_split() if percent > ZERO]
    if not split:
        raise LedgerError(
            "No investor has a share to distribute to. Record their capital first, "
            "or set ownership percentages by hand."
        )

    percents = [percent for _inv, percent in split]
    shares = _allocate_with_remainder(to_share, percents)

    distribution = ProfitDistribution.objects.create(
        period_start=period_start,
        period_end=period_end,
        distribution_date=distribution_date,
        net_profit=net_profit,
        distributed_amount=to_share,
        notes=notes,
        created_by=created_by,
    )

    lines = [(get_account(RETAINED_EARNINGS_CODE, 'Retained Earnings'),
              to_share, 'Profit shared out')]

    for (investor, percent), share in zip(split, shares):
        if share <= ZERO:
            continue
        ProfitShare.objects.create(
            distribution=distribution, investor=investor,
            ownership_percent=percent, amount=share,
        )
        lines.append(
            (investor.get_equity_account(), -share, f'{percent}% share')
        )

    txn = post_transaction(
        date=distribution_date,
        description=f'Profit share {period_start} to {period_end}',
        lines=lines,
        source_type=Transaction.SOURCE_DISTRIBUTION,
        source_id=distribution.pk,
        created_by=created_by,
    )
    ProfitDistribution.objects.filter(pk=distribution.pk).update(transaction=txn)
    distribution.transaction = txn
    return distribution


# ══════════════════════════════════════════════════════════════════════════════
#  UNDOING THINGS
# ══════════════════════════════════════════════════════════════════════════════
#
#  Two different situations, deliberately kept apart:
#
#  * Nothing was posted — a draft, or a record with no history. Really deleted,
#    because there is nothing to explain.
#  * Something was posted — the money moved. Undone by posting the opposite,
#    so the balances go back and the history still says what happened.
#
#  The second is what keeps the trial balance meaningful. A ledger you can
#  quietly delete rows from is a ledger nobody can rely on, and in an audit or
#  a dispute the deleted row is exactly the one you needed.

@db_transaction.atomic
def reverse_investor_movement(txn, *, reason='', created_by=None):
    """Undo a capital contribution or a drawing."""
    if txn.source_type != Transaction.SOURCE_INVESTOR:
        raise LedgerError(
            f"{txn.reference_no} is not a capital movement."
        )
    return reverse_transaction(
        txn, reason=reason or 'Capital movement undone',
        created_by=created_by, force=True,
    )


@db_transaction.atomic
def reverse_distribution(distribution, *, reason='', created_by=None):
    """
    Undo a profit share-out. The shares stay on record but stop counting,
    because the entry behind them is reversed.
    """
    if not distribution.transaction_id:
        raise LedgerError('That distribution was never posted.')
    if distribution.transaction.is_reversed:
        raise LedgerError('That distribution has already been undone.')

    return reverse_transaction(
        distribution.transaction,
        reason=reason or 'Profit distribution undone',
        created_by=created_by, force=True,
    )


@db_transaction.atomic
def cancel_loan(loan, *, reason='', created_by=None):
    """
    Undo a loan entered by mistake.

    Refused once repayments exist — those moved real money, and unpicking them
    silently would leave the cash position wrong. Reverse the repayments first.
    """
    from .models import Loan

    if loan.status == Loan.STATUS_CANCELLED:
        raise LedgerError(f"{loan.loan_no} is already cancelled.")

    if loan.payments.exists():
        live = [p for p in loan.payments.all()
                if not (p.transaction_id and p.transaction.is_reversed)]
        if live:
            raise LedgerError(
                f"{loan.loan_no} has {len(live)} repayment(s) recorded. Undo "
                f"those first — they moved real money."
            )

    if loan.transaction_id and not loan.transaction.is_reversed:
        reverse_transaction(
            loan.transaction,
            reason=reason or f'Loan {loan.loan_no} cancelled',
            created_by=created_by, force=True,
        )

    loan.installments.all().delete()
    loan.status = Loan.STATUS_CANCELLED
    loan.save(update_fields=['status', 'updated_at'])
    return loan


@db_transaction.atomic
def reverse_loan_payment(payment, *, reason='', created_by=None):
    """Undo a repayment, putting the principal and interest back."""
    from .models import LoanInstallment

    if not payment.transaction_id:
        raise LedgerError('That repayment was never posted.')
    if payment.transaction.is_reversed:
        raise LedgerError('That repayment has already been undone.')

    reverse_transaction(
        payment.transaction,
        reason=reason or f'Repayment on {payment.loan.loan_no} undone',
        created_by=created_by, force=True,
    )

    installment = payment.installment
    if installment is not None:
        installment.principal_paid = max(
            ZERO, installment.principal_paid - payment.principal_amount)
        installment.interest_paid = max(
            ZERO, installment.interest_paid - payment.interest_amount)

        if installment.total_paid <= ZERO:
            installment.status = LoanInstallment.STATUS_DUE
            installment.paid_date = None
        elif installment.total_paid < installment.total_due:
            installment.status = LoanInstallment.STATUS_PARTIAL
        installment.save(update_fields=[
            'principal_paid', 'interest_paid', 'status', 'paid_date',
        ])

    refresh_loan_status(payment.loan)
    return payment


#: Movements the owner entered by hand, and can therefore take back here.
#: Sales come from an invoice or an order — undo those from the document, so
#: the paperwork and the stock stay in step.
UNDOABLE_STOCK_REASONS = {'adjustment', 'damage', 'opening'}


@db_transaction.atomic
def reverse_stock_movement(movement, *, reason='', created_by=None):
    """Take back a stock adjustment, write-off or opening-stock entry."""
    from .models import StockBatch, StockMovement

    if movement.reason not in UNDOABLE_STOCK_REASONS:
        raise LedgerError(
            f"{movement.get_reason_display()} came from a sale or a purchase. "
            f"Undo it from the invoice, order or purchase that created it."
        )

    if StockMovement.objects.filter(
        variant=movement.variant,
        reference=f'undo-{movement.pk}',
    ).exists():
        raise LedgerError('That movement has already been taken back.')

    quantity = movement.quantity

    if quantity > 0:
        # It brought stock in. Those exact units must still be sitting in the
        # batch it created, otherwise some of them have already been sold.
        batch = movement.batch
        if batch is None:
            raise LedgerError('That movement has no batch to take back.')
        if batch.qty_remaining < quantity:
            raise LedgerError(
                f"{quantity - batch.qty_remaining} of those units have already "
                f"been sold, so the entry cannot be taken back. Record a stock "
                f"adjustment instead."
            )
        batch.qty_remaining -= quantity
        batch.save(update_fields=['qty_remaining'])
        restored_cost = money(batch.unit_cost * quantity)
        undo_quantity = -quantity
    else:
        # It took stock out. Put it back where it came from.
        restored_cost = ZERO
        for consumption in movement.consumptions.select_related('batch'):
            give_back = consumption.qty_returnable
            if give_back <= 0:
                continue
            consumption.batch.qty_remaining += give_back
            consumption.batch.save(update_fields=['qty_remaining'])
            consumption.qty_returned += give_back
            consumption.save(update_fields=['qty_returned'])
            restored_cost += consumption.unit_cost * give_back
        restored_cost = money(restored_cost)
        undo_quantity = -quantity

    undo = StockMovement.objects.create(
        variant=movement.variant,
        date=today(),
        quantity=undo_quantity,
        reason=StockMovement.REASON_ADJUST,
        reference=f'undo-{movement.pk}',
        note=(reason or f'Took back {movement.get_reason_display().lower()}')[:255],
        created_by=created_by,
    )

    if movement.transaction_id and not movement.transaction.is_reversed:
        reverse_transaction(
            movement.transaction,
            reason=reason or 'Stock entry taken back',
            created_by=created_by, force=True,
        )

    sync_variant_stock(movement.variant)
    return undo


# ── Deleting things that never touched the ledger ─────────────────────────────

def why_investor_cannot_be_deleted(investor):
    """Reason this investor must be undone rather than deleted, or None."""
    if investor.equity_account_id and investor.equity_account.lines.exists():
        return (
            'Money has moved on this investor\'s account. Undo each capital '
            'movement first, then the investor can be removed.'
        )
    if investor.profit_shares.exists():
        return 'This investor has been included in a profit distribution.'
    return None


@db_transaction.atomic
def delete_investor(investor):
    """Remove an investor who never had any money move."""
    blocked = why_investor_cannot_be_deleted(investor)
    if blocked:
        raise LedgerError(blocked)

    account = investor.equity_account
    investor.delete()
    if account is not None:
        account.delete()
    return True


def why_party_cannot_be_deleted(party):
    """Reason this client or supplier must be kept, or None."""
    from .models import Invoice

    if party.invoices.exclude(status=Invoice.STATUS_DRAFT).exists():
        return 'This client has invoices that were issued.'
    if party.payments.exists():
        return 'Payments have been recorded against this client.'
    if party.purchases.exists():
        return 'Purchases have been recorded against this supplier.'
    if party.receivable_account_id and party.receivable_account.lines.exists():
        return 'Money has moved on this client\'s account.'
    return None


@db_transaction.atomic
def delete_party(party):
    """Remove a client or supplier who never traded."""
    blocked = why_party_cannot_be_deleted(party)
    if blocked:
        raise LedgerError(blocked)

    account = party.receivable_account
    party.invoices.all().delete()      # drafts only, by the check above
    party.delete()
    if account is not None:
        account.delete()
    return True


@db_transaction.atomic
def delete_draft_invoice(invoice):
    """Throw away a draft. Nothing was posted, so nothing is lost."""
    from .models import Invoice

    if invoice.status != Invoice.STATUS_DRAFT:
        raise LedgerError(
            f"{invoice.display_number} has been issued. Cancel it instead — "
            f"that reverses the entry and keeps the record."
        )
    invoice.items.all().delete()
    invoice.delete()
    return True


@db_transaction.atomic
def delete_draft_purchase(purchase):
    """Throw away a purchase that was never confirmed."""
    from .models import Purchase

    if purchase.status != Purchase.STATUS_DRAFT:
        raise LedgerError(
            f"{purchase.purchase_no} has been confirmed or received. Cancel it "
            f"instead — that reverses the entries and keeps the record."
        )
    purchase.items.all().delete()
    purchase.delete()
    return True


def investor_statement(investor):
    """Every movement on one investor's capital account, with a running total."""
    if not investor.equity_account_id:
        return {
            'investor': investor, 'rows': [], 'closing_balance': ZERO,
            'capital_in': ZERO, 'drawings': ZERO, 'profit_share': ZERO,
        }

    rows = account_ledger(investor.equity_account)
    return {
        'investor': investor,
        'rows': list(reversed(rows)),
        'closing_balance': investor.current_stake,
        'capital_in': investor.capital_in,
        'drawings': investor.drawings,
        'profit_share': investor.profit_share_total,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  M8 — LOANS
# ══════════════════════════════════════════════════════════════════════════════

def next_loan_number():
    from .models import Loan

    last = Loan.objects.order_by('-id').values_list('loan_no', flat=True).first()
    sequence = 1
    if last:
        try:
            sequence = int(last.rsplit('-', 1)[1]) + 1
        except (IndexError, ValueError):
            sequence = Loan.objects.count() + 1
    return f'LOAN-{sequence:04d}'


def _add_months(start, months):
    """Same day of the month, `months` later, clamped to the month's length."""
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date_cls(year, month, day)


def build_schedule(*, principal, interest_rate, method, tenure_months, start_date):
    """
    Work out the installment schedule.

    Flat: interest is charged on the original amount for the whole term and
    split evenly — the way most informal lending in Bangladesh is quoted.

    Reducing balance: interest each month is charged on what is still owed, so
    the split shifts from interest to principal as the loan runs down. This is
    the standard bank EMI.

    Returns [(number, due_date, principal_due, interest_due), …] with the
    principal parts summing exactly to the principal.
    """
    from .models import Loan

    principal = money(principal)
    tenure = int(tenure_months)
    if tenure <= 0:
        raise LedgerError("A loan needs at least one installment.")
    if principal <= ZERO:
        raise LedgerError("The loan amount must be more than zero.")

    rate = Decimal(str(interest_rate)) / Decimal('100')
    rows = []

    if method == Loan.METHOD_FLAT or rate == ZERO:
        years = Decimal(tenure) / Decimal('12')
        total_interest = money(principal * rate * years)

        principal_parts = _allocate_with_remainder(
            principal, [Decimal('1')] * tenure)
        interest_parts = _allocate_with_remainder(
            total_interest, [Decimal('1')] * tenure)

        for index in range(tenure):
            rows.append((
                index + 1,
                _add_months(start_date, index + 1),
                principal_parts[index],
                interest_parts[index],
            ))
        return rows

    # Reducing balance — the standard EMI formula.
    monthly = rate / Decimal('12')
    growth = (Decimal('1') + monthly) ** tenure
    emi = money(principal * monthly * growth / (growth - Decimal('1')))

    balance = principal
    for index in range(tenure):
        interest = money(balance * monthly)
        principal_part = emi - interest

        if index == tenure - 1:
            # Last installment clears whatever is left, so rounding across the
            # term never leaves a stray taka behind.
            principal_part = balance

        principal_part = money(principal_part)
        rows.append((
            index + 1,
            _add_months(start_date, index + 1),
            principal_part,
            interest,
        ))
        balance -= principal_part

    return rows


@db_transaction.atomic
def create_loan(*, direction, counterparty_name, principal, interest_rate,
                method, tenure_months, start_date, account, notes='',
                created_by=None):
    """
    Record a loan and generate its schedule.

    Taken:  Cash        +principal   /  Loan account  -principal  (you owe it)
    Given:  Loan account +principal  /  Cash          -principal  (they owe you)
    """
    from .models import Loan, LoanInstallment

    principal = money(principal)
    _require_type(account, Account.MONEY_TYPES, 'Cash account')

    schedule = build_schedule(
        principal=principal, interest_rate=interest_rate, method=method,
        tenure_months=tenure_months, start_date=start_date,
    )

    loan = Loan.objects.create(
        loan_no=next_loan_number(),
        direction=direction,
        counterparty_name=counterparty_name,
        principal=principal,
        interest_rate=Decimal(str(interest_rate)),
        method=method,
        tenure_months=tenure_months,
        start_date=start_date,
        notes=notes,
        created_by=created_by,
    )

    is_taken = direction == Loan.DIRECTION_TAKEN
    loan_account = Account.objects.create(
        code=f'{"2200" if is_taken else "1400"}-{loan.pk}',
        name=f'{"Loan from" if is_taken else "Loan to"} {counterparty_name}',
        type=Account.TYPE_LOAN_PAYABLE if is_taken else Account.TYPE_LOAN_RECEIVABLE,
        description=f'{loan.loan_no} — {counterparty_name}',
        party_type=Account.PARTY_LENDER,
        party_id=loan.pk,
    )

    txn = post_simple(
        date=start_date,
        description=f'{loan.loan_no} — {"borrowed from" if is_taken else "lent to"} {counterparty_name}',
        debit_account=account if is_taken else loan_account,
        credit_account=loan_account if is_taken else account,
        amount=principal,
        source_type=Transaction.SOURCE_LOAN,
        source_id=loan.pk,
        created_by=created_by,
    )

    for number, due_date, principal_due, interest_due in schedule:
        LoanInstallment.objects.create(
            loan=loan, number=number, due_date=due_date,
            principal_due=principal_due, interest_due=interest_due,
        )

    Loan.objects.filter(pk=loan.pk).update(account=loan_account, transaction=txn)
    loan.account = loan_account
    loan.transaction = txn
    return loan


@db_transaction.atomic
def record_loan_payment(*, loan, date, principal_amount, interest_amount,
                        account, installment=None, reference='', notes='',
                        created_by=None):
    """
    Record a repayment, splitting it between principal and interest.

    On a loan taken:

        Loan from <lender>   +principal   (the debt shrinks)
        Interest Expense     +interest    (the cost of borrowing)
        Cash                 -total

    On a loan given, the mirror image, with the interest booked as income.
    """
    from .models import Loan, LoanInstallment, LoanPayment

    principal_amount = money(principal_amount)
    interest_amount = money(interest_amount)
    total = principal_amount + interest_amount

    if total <= ZERO:
        raise LedgerError("A repayment must be more than zero.")
    if principal_amount < ZERO or interest_amount < ZERO:
        raise LedgerError("Principal and interest cannot be negative.")
    if loan.status == Loan.STATUS_CANCELLED:
        raise LedgerError(f"{loan.loan_no} has been cancelled.")

    outstanding = money(loan.outstanding)
    if principal_amount > outstanding:
        raise LedgerError(
            f"৳{principal_amount:,.2f} is more principal than the "
            f"৳{outstanding:,.2f} still owed on {loan.loan_no}. "
            f"Interest goes in the interest field, not the principal one."
        )

    _require_type(account, Account.MONEY_TYPES, 'Cash account')

    payment = LoanPayment.objects.create(
        loan=loan, installment=installment, date=date,
        principal_amount=principal_amount, interest_amount=interest_amount,
        account=account, reference=reference, notes=notes, created_by=created_by,
    )

    if loan.is_taken:
        lines = [(loan.account, principal_amount, 'Principal repaid')]
        if interest_amount > ZERO:
            lines.append(
                (get_account(INTEREST_EXPENSE_CODE, 'Interest Expense'),
                 interest_amount, 'Interest')
            )
        lines.append((account, -total, reference or ''))
    else:
        lines = [(account, total, reference or '')]
        lines.append((loan.account, -principal_amount, 'Principal returned'))
        if interest_amount > ZERO:
            lines.append(
                (get_account(INTEREST_INCOME_CODE, 'Interest Income'),
                 -interest_amount, 'Interest earned')
            )

    txn = post_transaction(
        date=date,
        description=f'{loan.loan_no} repayment — {loan.counterparty_name}',
        lines=lines,
        source_type=Transaction.SOURCE_LOAN,
        source_id=loan.pk,
        created_by=created_by,
    )
    LoanPayment.objects.filter(pk=payment.pk).update(transaction=txn)
    payment.transaction = txn

    if installment is not None:
        installment.principal_paid += principal_amount
        installment.interest_paid += interest_amount
        if installment.total_paid >= installment.total_due:
            installment.status = LoanInstallment.STATUS_PAID
            installment.paid_date = date
        elif installment.total_paid > ZERO:
            installment.status = LoanInstallment.STATUS_PARTIAL
        installment.save(update_fields=[
            'principal_paid', 'interest_paid', 'status', 'paid_date',
        ])

    refresh_loan_status(loan)
    return payment


def refresh_loan_status(loan):
    """Close a loan once every installment has been settled."""
    from .models import Loan, LoanInstallment

    if loan.status == Loan.STATUS_CANCELLED:
        return loan

    # Closed once every installment is settled, or once the principal is fully
    # repaid — a loan cleared by payments that were not tied to installments
    # would otherwise stay Active forever.
    unpaid = loan.installments.exclude(status=LoanInstallment.STATUS_PAID).exists()
    principal_cleared = money(loan.outstanding) <= ZERO
    status = Loan.STATUS_ACTIVE if (unpaid and not principal_cleared) else Loan.STATUS_CLOSED

    if status != loan.status:
        loan.status = status
        loan.save(update_fields=['status', 'updated_at'])
    return loan


def loan_summary():
    """Headline numbers for the loans dashboard."""
    from .models import Loan, LoanInstallment

    taken = Loan.objects.filter(
        direction=Loan.DIRECTION_TAKEN, status=Loan.STATUS_ACTIVE)
    given = Loan.objects.filter(
        direction=Loan.DIRECTION_GIVEN, status=Loan.STATUS_ACTIVE)

    overdue = [
        installment for installment in LoanInstallment.objects.filter(
            due_date__lt=today(),
        ).exclude(status=LoanInstallment.STATUS_PAID).select_related('loan')
        if installment.loan.status == Loan.STATUS_ACTIVE
    ]

    upcoming = list(
        LoanInstallment.objects.filter(
            due_date__gte=today(), due_date__lte=today() + timedelta(days=30),
        ).exclude(status=LoanInstallment.STATUS_PAID)
        .select_related('loan').order_by('due_date')[:10]
    )

    return {
        'owed': type_balance([Account.TYPE_LOAN_PAYABLE]),
        'owed_to_us': type_balance([Account.TYPE_LOAN_RECEIVABLE]),
        'taken_count': taken.count(),
        'given_count': given.count(),
        'overdue': overdue,
        'overdue_total': sum((i.outstanding for i in overdue), ZERO),
        'upcoming': upcoming,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  BALANCES
# ══════════════════════════════════════════════════════════════════════════════

def account_balance(account, as_of=None, since=None):
    """What this account holds, stated the way a person would say it."""
    return account.balance(as_of=as_of, since=since)


def _date_filter(as_of=None, since=None, prefix='lines__transaction__date'):
    """
    Build the date condition for a filtered aggregate, or None when there is no
    date restriction — an empty Q() is not a valid `filter=` argument to Sum().
    """
    conditions = []
    if as_of is not None:
        conditions.append(Q(**{f'{prefix}__lte': as_of}))
    if since is not None:
        conditions.append(Q(**{f'{prefix}__gte': since}))
    if not conditions:
        return None
    combined = conditions[0]
    for extra in conditions[1:]:
        combined &= extra
    return combined


def accounts_with_balances(types=None, as_of=None, since=None,
                           include_inactive=False, party_type=None, party_id=None):
    """
    Accounts annotated with `signed_total` and `natural_total`, computed in one
    query. Used by the account list and dashboard so N accounts do not become
    N balance queries.
    """
    qs = Account.objects.all()

    if types is not None:
        qs = qs.filter(type__in=list(types))
    if not include_inactive:
        qs = qs.filter(is_active=True)
    if party_type is not None:
        qs = qs.filter(party_type=party_type)
    if party_id is not None:
        qs = qs.filter(party_id=party_id)

    sign = Case(
        When(type__in=sorted(Account.DEBIT_NORMAL_TYPES), then=Value(1)),
        default=Value(-1),
        output_field=IntegerField(),
    )

    return qs.annotate(
        signed_total=Coalesce(
            Sum('lines__amount', filter=_date_filter(as_of, since)),
            Value(ZERO),
            output_field=_MONEY_FIELD,
        ),
    ).annotate(
        natural_total=ExpressionWrapper(F('signed_total') * sign, output_field=_MONEY_FIELD),
    )


def type_balance(types, as_of=None, since=None):
    """
    Combined natural balance across a set of account types.

        type_balance([Account.TYPE_RECEIVABLE])  → total owed to the business
        type_balance(Account.MONEY_TYPES)        → total spendable cash
    """
    result = TransactionLine.objects.filter(
        account__type__in=list(types),
    )
    if as_of is not None:
        result = result.filter(transaction__date__lte=as_of)
    if since is not None:
        result = result.filter(transaction__date__gte=since)

    signed = result.aggregate(total=Sum('amount'))['total'] or ZERO

    # Every type in one call must share a normal direction, or the combined
    # figure is meaningless — adding income to expenses and flipping the sign
    # once gives a number that means nothing. Caught here rather than returning
    # a plausible-looking wrong answer.
    types = list(types)
    debit_normal = {t in Account.DEBIT_NORMAL_TYPES for t in types}
    if len(debit_normal) > 1:
        raise LedgerError(
            f"type_balance() was given types that run in opposite directions "
            f"({', '.join(sorted(types))}). Ask for them separately, or use "
            f"accounts_with_balances() which handles each account's own sign."
        )

    sign = 1 if debit_normal.pop() else -1
    return signed * sign


def cash_on_hand(as_of=None):
    """Total spendable money across cash, bank and mobile money accounts."""
    return type_balance(Account.MONEY_TYPES, as_of=as_of)


def period_profit(since=None, as_of=None):
    """
    Net profit for a period: income earned minus expenses incurred.
    Returns a dict so callers can show the parts, not just the total.
    """
    income = type_balance([Account.TYPE_INCOME], as_of=as_of, since=since)
    expense = type_balance([Account.TYPE_EXPENSE], as_of=as_of, since=since)
    return {
        'income': income,
        'expense': expense,
        'profit': income - expense,
    }


def trial_balance(as_of=None):
    """
    Every account with a non-zero balance, plus the proof that the ledger is
    internally consistent: `total_signed` must be exactly zero.

    If it is ever non-zero, something wrote to TransactionLine without going
    through post_transaction().
    """
    accounts = [
        acc for acc in accounts_with_balances(as_of=as_of, include_inactive=True)
        if acc.signed_total != ZERO
    ]
    total_signed = sum((acc.signed_total for acc in accounts), ZERO)
    return {
        'accounts': accounts,
        'total_signed': total_signed,
        'is_balanced': total_signed == ZERO,
        'as_of': as_of,
    }


def account_ledger(account, as_of=None, since=None, limit=None):
    """
    One account's entries in date order with a running balance — the view
    behind "show me everything that touched bKash".
    """
    qs = account.lines.select_related('transaction').order_by(
        'transaction__date', 'transaction_id', 'id',
    )
    if as_of is not None:
        qs = qs.filter(transaction__date__lte=as_of)
    if since is not None:
        qs = qs.filter(transaction__date__gte=since)
    if limit is not None:
        qs = qs[:limit]

    sign = account.normal_sign
    running = ZERO
    rows = []
    for line in qs:
        running += line.amount * sign
        rows.append({
            'line': line,
            'transaction': line.transaction,
            'change': line.amount * sign,
            'running_balance': running,
        })
    return rows
