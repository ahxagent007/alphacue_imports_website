"""
finance/models.py
─────────────────
M1 — the ledger core.

Three tables carry every money movement in the business:

    Account          — a bucket money sits in or flows through
    Transaction      — one event ("paid the courier", "customer settled invoice")
    TransactionLine  — where the money came from and went to

SIGN CONVENTION
───────────────
Every line stores a single signed `amount`, and the lines of one transaction
must sum to exactly zero. The convention is *debit-positive*:

    positive  →  money INTO an asset, or a cost incurred
    negative  →  money OUT of an asset, or income/liability increasing

Example — paying ৳500 courier charge in cash:

    Courier Expense   +500.00
    Cash in Hand      -500.00
                      ───────
                         0.00

Because each account type has a "normal" direction, the raw sum is not what a
person wants to read. A loan you owe accumulates negative raw amounts, but the
owner wants to see "৳20,000 owed". So `Account.balance()` flips the sign for
credit-natured accounts and always returns the number as a human would say it.
`Account.signed_balance()` returns the raw figure, used for the trial balance.

IMMUTABILITY
────────────
Posted transactions cannot be edited or deleted — mistakes are corrected by
posting a reversal. This is enforced in save()/delete() on both Transaction and
TransactionLine, so it holds even from the Django shell or admin.
"""

from decimal import Decimal, ROUND_HALF_UP

from django.db import models
from django.utils import timezone

from .exceptions import ImmutableTransaction

ZERO = Decimal('0.00')


def today():
    """
    Today's date.

    Not `timezone.localdate()`: this project runs with USE_TZ = False (see
    settings.py — cPanel MySQL often lacks the timezone tables), so
    `timezone.now()` returns a naive datetime and localdate() raises on it.
    """
    return timezone.now().date()


# ─── Account ──────────────────────────────────────────────────────────────────

class Account(models.Model):
    """
    A bucket money sits in or flows through.

    Accounts are either created by the `seed_chart_of_accounts` command
    (is_system=True) or per-party — one receivable account per credit client,
    one equity account per investor, one liability account per loan.
    """

    # ── Asset-natured (debit normal) ──────────────────────────────────────
    TYPE_CASH             = 'cash'
    TYPE_BANK             = 'bank'
    TYPE_MOBILE_MONEY     = 'mobile_money'
    TYPE_RECEIVABLE       = 'receivable'
    TYPE_INVENTORY        = 'inventory'
    TYPE_GOODS_IN_TRANSIT = 'goods_in_transit'
    TYPE_LOAN_RECEIVABLE  = 'loan_receivable'
    TYPE_EXPENSE          = 'expense'

    # ── Liability / equity / income-natured (credit normal) ───────────────
    TYPE_PAYABLE          = 'payable'
    TYPE_EQUITY           = 'equity'
    TYPE_LOAN_PAYABLE     = 'loan_payable'
    TYPE_INCOME           = 'income'

    TYPE_CHOICES = [
        (TYPE_CASH,             'Cash'),
        (TYPE_BANK,             'Bank Account'),
        (TYPE_MOBILE_MONEY,     'Mobile Money (bKash / Nagad)'),
        (TYPE_RECEIVABLE,       'Money Owed To Us'),
        (TYPE_INVENTORY,        'Stock Value'),
        (TYPE_GOODS_IN_TRANSIT, 'Goods In Transit'),
        (TYPE_LOAN_RECEIVABLE,  'Loan Given Out'),
        (TYPE_EXPENSE,          'Expense'),
        (TYPE_PAYABLE,          'Money We Owe'),
        (TYPE_EQUITY,           'Owner / Investor Capital'),
        (TYPE_LOAN_PAYABLE,     'Loan Taken'),
        (TYPE_INCOME,           'Income'),
    ]

    #: Types whose balance grows with positive (debit) amounts.
    DEBIT_NORMAL_TYPES = {
        TYPE_CASH, TYPE_BANK, TYPE_MOBILE_MONEY, TYPE_RECEIVABLE,
        TYPE_INVENTORY, TYPE_GOODS_IN_TRANSIT, TYPE_LOAN_RECEIVABLE,
        TYPE_EXPENSE,
    }

    #: Types whose balance grows with negative (credit) amounts.
    CREDIT_NORMAL_TYPES = {
        TYPE_PAYABLE, TYPE_EQUITY, TYPE_LOAN_PAYABLE, TYPE_INCOME,
    }

    #: Spendable money — what "cash on hand" means.
    MONEY_TYPES = {TYPE_CASH, TYPE_BANK, TYPE_MOBILE_MONEY}

    #: Accounts that belong to a profit-and-loss period rather than accumulating
    #: forever. Used by the P&L report in M9.
    PROFIT_LOSS_TYPES = {TYPE_INCOME, TYPE_EXPENSE}

    # ── Party linkage ─────────────────────────────────────────────────────
    # Loose FK by design, matching the existing pattern in affiliate/models.py
    # (Commission.order_id, ProductCommissionSetting.product_id). The concrete
    # Party model arrives in M3; these fields let M1 ship without a forward
    # dependency on it.
    PARTY_CLIENT   = 'client'
    PARTY_SUPPLIER = 'supplier'
    PARTY_INVESTOR = 'investor'
    PARTY_LENDER   = 'lender'
    PARTY_CHOICES = [
        (PARTY_CLIENT,   'Client'),
        (PARTY_SUPPLIER, 'Supplier'),
        (PARTY_INVESTOR, 'Investor'),
        (PARTY_LENDER,   'Lender / Borrower'),
    ]

    code = models.CharField(
        max_length=20, unique=True, db_index=True,
        help_text="Short reference, e.g. 1010. Grouped by type — see the seed command. "
                  "Per-party accounts extend a base code, e.g. 1200-14.",
    )
    name = models.CharField(max_length=120)
    type = models.CharField(max_length=20, choices=TYPE_CHOICES, db_index=True)
    description = models.CharField(max_length=255, blank=True)

    party_type = models.CharField(
        max_length=20, choices=PARTY_CHOICES, blank=True, default='', db_index=True,
        help_text="Set when this account belongs to one client / supplier / investor / lender.",
    )
    party_id = models.PositiveIntegerField(null=True, blank=True, db_index=True)

    is_active = models.BooleanField(
        default=True, db_index=True,
        help_text="Inactive accounts cannot be posted to. Existing history is kept.",
    )
    is_system = models.BooleanField(
        default=False,
        help_text="Created by the chart-of-accounts seed. Should not be deleted.",
    )

    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = 'Account'
        verbose_name_plural = 'Accounts'
        ordering = ['code']
        indexes = [
            models.Index(fields=['type', 'is_active']),
            models.Index(fields=['party_type', 'party_id']),
        ]

    def __str__(self):
        return f"{self.code} — {self.name}"

    # ── Sign handling ─────────────────────────────────────────────────────

    @property
    def normal_sign(self):
        """+1 for debit-natured accounts, -1 for credit-natured ones."""
        return 1 if self.type in self.DEBIT_NORMAL_TYPES else -1

    @property
    def is_debit_normal(self):
        return self.type in self.DEBIT_NORMAL_TYPES

    # ── Balances ──────────────────────────────────────────────────────────

    def _line_sum(self, as_of=None, since=None):
        qs = self.lines.all()
        if as_of is not None:
            qs = qs.filter(transaction__date__lte=as_of)
        if since is not None:
            qs = qs.filter(transaction__date__gte=since)
        return qs.aggregate(total=models.Sum('amount'))['total'] or ZERO

    def signed_balance(self, as_of=None, since=None):
        """
        Raw debit-positive sum. Used for the trial balance, where every
        account's signed balance must add up to zero across the whole ledger.
        """
        return self._line_sum(as_of=as_of, since=since)

    def balance(self, as_of=None, since=None):
        """
        Balance as a person would state it — always positive when the account
        holds what it normally holds.

            Cash with ৳5,000 in it        →   5000.00
            A loan of ৳20,000 owed        →  20000.00
            An investor who put in ৳1L    → 100000.00

        `as_of`  — include only entries on or before this date.
        `since`  — include only entries on or after this date (for P&L periods).
        """
        return self._line_sum(as_of=as_of, since=since) * self.normal_sign

    @property
    def current_balance(self):
        """Convenience for templates, which cannot pass arguments."""
        return self.balance()


# ─── Transaction ──────────────────────────────────────────────────────────────

class Transaction(models.Model):
    """
    One money event. Immutable once created.

    Never construct this directly — go through finance.services.post_transaction(),
    which validates the lines balance and writes everything in one atomic block.
    """

    #: The only field that may change after posting.
    MUTABLE_AFTER_POST = {'is_reversed'}

    # What produced this transaction. Loose reference, same rationale as
    # Account.party_type — lets later milestones point at their own models
    # without the ledger depending on them.
    SOURCE_MANUAL     = 'manual'
    SOURCE_EXPENSE    = 'expense'
    SOURCE_TRANSFER   = 'transfer'
    SOURCE_INVOICE    = 'invoice'
    SOURCE_PAYMENT    = 'payment'
    SOURCE_PURCHASE   = 'purchase'
    SOURCE_STOCK      = 'stock'
    SOURCE_INVESTOR     = 'investor'
    SOURCE_DISTRIBUTION = 'distribution'
    SOURCE_LOAN         = 'loan'

    #: Sources owned by a document elsewhere in the system. Reversing one of
    #: these from the generic ledger screen would leave the invoice, purchase or
    #: payment behind it saying something the ledger no longer agrees with — so
    #: those must be undone from their own screen, which keeps both in step.
    MANAGED_SOURCES = {
        'invoice', 'payment', 'purchase', 'stock',
        'investor', 'distribution', 'loan', 'affiliate',
    }

    #: Where to send someone who tried to reverse a managed entry by hand.
    MANAGED_SOURCE_ADVICE = {
        'invoice':      'Cancel the invoice from its own page instead.',
        'payment':      'Reverse the payment from the Payments page instead.',
        'purchase':     'Cancel the purchase from its own page instead.',
        'stock':        'Correct it with a stock adjustment instead.',
        'investor':     'Record an opposite capital movement instead.',
        'distribution': 'Record an opposite capital movement instead.',
        'loan':         'Record a correcting loan repayment instead.',
        'affiliate':    'Change the commission or withdrawal in the affiliate panel instead.',
    }
    SOURCE_AFFILIATE  = 'affiliate'
    SOURCE_OPENING    = 'opening'
    SOURCE_REVERSAL   = 'reversal'

    date = models.DateField(db_index=True, help_text="The date the money actually moved.")
    description = models.CharField(max_length=255)

    source_type = models.CharField(max_length=30, blank=True, default='', db_index=True)
    source_id = models.PositiveIntegerField(null=True, blank=True, db_index=True)

    created_by = models.ForeignKey(
        'auth.User', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='finance_transactions',
    )
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    is_reversed = models.BooleanField(default=False, db_index=True)
    reversal_of = models.ForeignKey(
        'self', null=True, blank=True,
        on_delete=models.PROTECT, related_name='reversals',
        help_text="Set when this transaction exists to undo another one.",
    )

    class Meta:
        verbose_name = 'Transaction'
        verbose_name_plural = 'Transactions'
        ordering = ['-date', '-id']
        indexes = [
            models.Index(fields=['date', 'id']),
            models.Index(fields=['source_type', 'source_id']),
        ]

    def __str__(self):
        return f"{self.reference_no} — {self.description}"

    @property
    def reference_no(self):
        """
        Derived rather than stored, so it can never disagree with the row it
        names and needs no second write to assign.
        """
        return f"TXN-{self.pk:06d}" if self.pk else "TXN-(unsaved)"

    # ── Immutability ──────────────────────────────────────────────────────

    def save(self, *args, **kwargs):
        if not self._state.adding:
            update_fields = kwargs.get('update_fields')
            allowed = update_fields is not None and set(update_fields).issubset(
                self.MUTABLE_AFTER_POST
            )
            if not allowed:
                raise ImmutableTransaction(
                    f"{self.reference_no} is already posted and cannot be edited. "
                    f"Post a reversal instead (finance.services.reverse_transaction)."
                )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutableTransaction(
            f"{self.reference_no} is already posted and cannot be deleted. "
            f"Post a reversal instead (finance.services.reverse_transaction)."
        )

    # ── Convenience ───────────────────────────────────────────────────────

    @property
    def total(self):
        """
        The size of the transaction — the sum of its positive side. A ৳500
        expense paid in cash has lines of +500 and -500, and a total of 500.
        """
        return sum(
            (line.amount for line in self.lines.all() if line.amount > 0),
            ZERO,
        )

    @property
    def is_balanced(self):
        return sum((line.amount for line in self.lines.all()), ZERO) == ZERO

    @property
    def debit_lines(self):
        """Where the money went — the expense incurred, or the asset that grew."""
        return [line for line in self.lines.all() if line.amount > 0]

    @property
    def credit_lines(self):
        """Where the money came from — the asset that shrank, or the income."""
        return [line for line in self.lines.all() if line.amount < 0]


# ─── Transaction Line ─────────────────────────────────────────────────────────

class TransactionLine(models.Model):
    """
    One side of a money movement. Immutable, like its parent transaction.

    `amount` is signed and debit-positive: see the module docstring.
    """

    transaction = models.ForeignKey(
        Transaction, on_delete=models.CASCADE, related_name='lines',
    )
    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name='lines',
    )
    amount = models.DecimalField(
        max_digits=14, decimal_places=2,
        help_text="Signed. Positive = into an asset or a cost; negative = out of an asset, or income.",
    )
    memo = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        verbose_name = 'Transaction Line'
        verbose_name_plural = 'Transaction Lines'
        ordering = ['-amount', 'id']
        indexes = [
            models.Index(fields=['account', 'transaction']),
        ]

    def __str__(self):
        return f"{self.account.code} {self.amount:+,.2f}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ImmutableTransaction(
                "Transaction lines cannot be edited once posted. "
                "Reverse the transaction and post a corrected one."
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutableTransaction(
            "Transaction lines cannot be deleted once posted. "
            "Reverse the transaction and post a corrected one."
        )

    @property
    def natural_amount(self):
        """This line's effect on the account, in the way a person reads it."""
        return self.amount * self.account.normal_sign


# ══════════════════════════════════════════════════════════════════════════════
#  M3 — PARTIES AND INVOICES
# ══════════════════════════════════════════════════════════════════════════════

class Party(models.Model):
    """
    Someone the business trades with — a wholesale buyer, a retail customer, a
    walk-in, or a supplier.

    Credit clients get their own receivable account so "what does Rahim Traders
    owe me?" is a balance query rather than a scan through invoices. That
    account is created lazily on first use, so casual walk-ins never clutter
    the chart of accounts.
    """

    TYPE_WHOLESALE = 'wholesale'
    TYPE_RETAIL    = 'retail'
    TYPE_WALKIN    = 'walkin'
    TYPE_SUPPLIER  = 'supplier'

    TYPE_CHOICES = [
        (TYPE_WHOLESALE, 'Wholesale / B2B Client'),
        (TYPE_RETAIL,    'Retail Customer'),
        (TYPE_WALKIN,    'Walk-in'),
        (TYPE_SUPPLIER,  'Supplier'),
    ]

    #: Types that buy on credit and therefore accumulate dues.
    CREDIT_TYPES = {TYPE_WHOLESALE}

    name = models.CharField(max_length=150, db_index=True)
    party_type = models.CharField(
        max_length=15, choices=TYPE_CHOICES, default=TYPE_WHOLESALE, db_index=True,
    )
    phone = models.CharField(max_length=30, blank=True, default='', db_index=True)
    email = models.EmailField(blank=True, default='')
    address = models.TextField(blank=True, default='')

    credit_limit = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        help_text="Warn when unpaid dues go above this. 0 = no limit set.",
    )
    notes = models.TextField(blank=True, default='')

    receivable_account = models.OneToOneField(
        Account, null=True, blank=True,
        on_delete=models.PROTECT, related_name='party_owner',
        help_text="Created automatically the first time this party is invoiced.",
    )

    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Party'
        verbose_name_plural = 'Parties'
        ordering = ['name']
        indexes = [
            models.Index(fields=['party_type', 'is_active']),
        ]

    def __str__(self):
        return self.name

    @property
    def buys_on_credit(self):
        return self.party_type in self.CREDIT_TYPES

    def get_receivable_account(self):
        """
        This party's own receivable account, created on first use.

        Codes extend the 1200 control account (1200-14), so per-client accounts
        sort together underneath it in the chart of accounts.
        """
        if self.receivable_account_id:
            return self.receivable_account

        # get_or_create rather than create: a code collision (an account made
        # by hand, or a half-finished earlier attempt) would otherwise raise an
        # IntegrityError the first time this client was invoiced.
        account, _created = Account.objects.get_or_create(
            code=f'1200-{self.pk}',
            defaults={
                'name': f'Owed by {self.name}',
                'type': Account.TYPE_RECEIVABLE,
                'description': f'Unpaid invoices for {self.name}',
                'party_type': Account.PARTY_CLIENT,
                'party_id': self.pk,
            },
        )
        Party.objects.filter(pk=self.pk).update(receivable_account=account)
        self.receivable_account = account
        return account

    @property
    def outstanding(self):
        """
        Total currently owed by this party. Zero if never invoiced.

        Picks up `outstanding_total` when the queryset was annotated by
        `services.with_outstanding()`, so a client list does not run one balance
        query per row.
        """
        annotated = getattr(self, 'outstanding_total', None)
        if annotated is not None:
            return annotated

        if not self.receivable_account_id:
            return ZERO
        return self.receivable_account.balance()

    @property
    def is_over_credit_limit(self):
        return self.credit_limit > ZERO and self.outstanding > self.credit_limit


class Invoice(models.Model):
    """
    A bill issued to a party.

    A draft has no effect on any balance and can be edited freely. Issuing it
    posts to the ledger and freezes it — from then on the only changes are
    payments (M4) and cancellation, which reverses the posting.
    """

    STATUS_DRAFT     = 'draft'
    STATUS_SENT      = 'sent'
    STATUS_PARTIAL   = 'partially_paid'
    STATUS_PAID      = 'paid'
    STATUS_CANCELLED = 'cancelled'

    STATUS_CHOICES = [
        (STATUS_DRAFT,     'Draft'),
        (STATUS_SENT,      'Sent'),
        (STATUS_PARTIAL,   'Partially Paid'),
        (STATUS_PAID,      'Paid'),
        (STATUS_CANCELLED, 'Cancelled'),
    ]

    #: Statuses where the invoice is live on the ledger and owed money.
    OPEN_STATUSES = {STATUS_SENT, STATUS_PARTIAL}

    TERMS_CHOICES = [
        (0,  'Due on receipt'),
        (7,  'Net 7 days'),
        (15, 'Net 15 days'),
        (30, 'Net 30 days'),
        (45, 'Net 45 days'),
    ]

    # Numbers are assigned when the invoice is *issued*, not when the draft is
    # created — so a discarded draft never leaves a gap in the sequence.
    # Nullable rather than blank because unique() would reject a second ''.
    number = models.CharField(
        max_length=20, unique=True, blank=True, null=True, default=None, db_index=True,
    )
    party = models.ForeignKey(Party, on_delete=models.PROTECT, related_name='invoices')

    issue_date = models.DateField(default=today, db_index=True)
    due_date = models.DateField(null=True, blank=True, db_index=True)
    payment_terms_days = models.PositiveSmallIntegerField(
        choices=TERMS_CHOICES, default=0,
        help_text="Used to work out the due date when the invoice is issued.",
    )

    discount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        help_text="Whole-invoice discount, on top of any per-line discounts.",
    )
    delivery_charge = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
    )

    notes = models.TextField(
        blank=True, default='',
        help_text="Shown at the bottom of the invoice — terms, thanks, instructions.",
    )

    status = models.CharField(
        max_length=15, choices=STATUS_CHOICES, default=STATUS_DRAFT, db_index=True,
    )

    order = models.ForeignKey(
        'store.Order', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='invoices',
        help_text="Set when this invoice was generated from a website order.",
    )
    transaction = models.ForeignKey(
        Transaction, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='invoices',
        help_text="The ledger entry created when this invoice was issued.",
    )

    share_token = models.CharField(
        max_length=64, unique=True, blank=True, null=True, db_index=True,
        help_text="Lets the client open this invoice without logging in.",
    )

    created_by = models.ForeignKey(
        'auth.User', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='finance_invoices',
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    issued_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Invoice'
        verbose_name_plural = 'Invoices'
        ordering = ['-issue_date', '-id']
        indexes = [
            models.Index(fields=['status', 'issue_date']),
            models.Index(fields=['party', 'status']),
            models.Index(fields=['due_date', 'status']),
        ]

    def __str__(self):
        return f"{self.display_number} — {self.party.name}"

    @property
    def display_number(self):
        """Drafts have no number yet, so fall back to something referable."""
        return self.number or f"Draft #{self.pk}"

    # ── Money ─────────────────────────────────────────────────────────────

    @property
    def subtotal(self):
        """Sum of the line totals, each already net of its own line discount."""
        return sum((item.line_total for item in self.items.all()), ZERO)

    @property
    def goods_total(self):
        """What the goods come to after the whole-invoice discount."""
        return self.subtotal - self.discount

    @property
    def total(self):
        return self.goods_total + self.delivery_charge

    @property
    def amount_paid(self):
        """
        Derived from payment allocations, so the figure can never drift.

        List views annotate `paid_total` through `services.with_payment_totals()`
        and this picks it up — otherwise a page showing 200 invoices would run
        200 extra queries just to fill in one column.
        """
        annotated = getattr(self, 'paid_total', None)
        if annotated is not None:
            return annotated

        from .services import invoice_amount_paid
        return invoice_amount_paid(self)

    @property
    def amount_due(self):
        return self.total - self.amount_paid

    @property
    def has_line_discounts(self):
        """Drives whether the printed invoice shows a discount column at all."""
        return any(item.discount for item in self.items.all())

    # ── State ─────────────────────────────────────────────────────────────

    @property
    def is_draft(self):
        return self.status == self.STATUS_DRAFT

    @property
    def is_editable(self):
        """Only drafts can be changed — issued invoices are on the ledger."""
        return self.status == self.STATUS_DRAFT

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES

    @property
    def is_overdue(self):
        """Derived, never stored — an invoice becomes overdue by the clock."""
        if not self.is_open or not self.due_date:
            return False
        return self.due_date < today()

    @property
    def days_overdue(self):
        if not self.is_overdue:
            return 0
        return (today() - self.due_date).days

    @property
    def display_status(self):
        """Status as the owner should read it, with overdue folded in."""
        if self.is_overdue:
            return 'Overdue'
        return self.get_status_display()


class InvoiceItem(models.Model):
    """
    One line on an invoice.

    Product name, SKU and price are copied in rather than read through the
    variant link, so a later price change or a renamed product never rewrites
    an invoice that was already sent.
    """

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='items')
    variant = models.ForeignKey(
        'store.ProductVariant', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='invoice_items',
        help_text="Optional link back to the catalogue.",
    )

    description = models.CharField(max_length=255)
    sku = models.CharField(max_length=80, blank=True, default='')
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    quantity = models.PositiveIntegerField(default=1)
    discount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        help_text="Discount on this line only, as an amount.",
    )
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        verbose_name = 'Invoice Item'
        verbose_name_plural = 'Invoice Items'
        ordering = ['sort_order', 'id']

    def __str__(self):
        return f"{self.description} x{self.quantity}"

    @property
    def gross_total(self):
        return self.unit_price * self.quantity

    @property
    def line_total(self):
        return self.gross_total - self.discount


# ══════════════════════════════════════════════════════════════════════════════
#  M4 — PAYMENTS
# ══════════════════════════════════════════════════════════════════════════════

class Payment(models.Model):
    """
    Money received from a client, or paid out to a supplier.

    One Payment is one movement of cash, even when it settles several invoices —
    which is how it actually happens. `PaymentAllocation` records which invoice
    each slice of it covers; anything left over is an advance sitting on the
    party's account.
    """

    DIRECTION_IN  = 'in'
    DIRECTION_OUT = 'out'
    DIRECTION_CHOICES = [
        (DIRECTION_IN,  'Received from client'),
        (DIRECTION_OUT, 'Paid to supplier'),
    ]

    party = models.ForeignKey(Party, on_delete=models.PROTECT, related_name='payments')
    direction = models.CharField(
        max_length=3, choices=DIRECTION_CHOICES, default=DIRECTION_IN, db_index=True,
    )
    date = models.DateField(default=today, db_index=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2)

    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name='payments',
        help_text="Where the money landed, or where it was paid from.",
    )
    reference = models.CharField(
        max_length=100, blank=True, default='',
        help_text="bKash/Nagad transaction id, cheque number, bank reference.",
    )
    notes = models.TextField(blank=True, default='')

    transaction = models.ForeignKey(
        Transaction, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='payments',
    )

    created_by = models.ForeignKey(
        'auth.User', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='finance_payments',
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = 'Payment'
        verbose_name_plural = 'Payments'
        ordering = ['-date', '-id']
        indexes = [
            models.Index(fields=['party', 'direction']),
            models.Index(fields=['date', 'direction']),
        ]

    def __str__(self):
        word = 'from' if self.direction == self.DIRECTION_IN else 'to'
        return f"৳{self.amount:,.2f} {word} {self.party.name} on {self.date}"

    @property
    def reference_no(self):
        return f"PAY-{self.pk:06d}" if self.pk else "PAY-(unsaved)"

    @property
    def allocated(self):
        return sum((alloc.amount for alloc in self.allocations.all()), ZERO)

    @property
    def unallocated(self):
        """Money received that is not against any invoice yet — an advance."""
        return self.amount - self.allocated

    @property
    def is_reversed(self):
        return bool(self.transaction_id and self.transaction.is_reversed)


class PaymentAllocation(models.Model):
    """Which invoice a slice of a payment settles."""

    payment = models.ForeignKey(
        Payment, on_delete=models.CASCADE, related_name='allocations',
    )
    invoice = models.ForeignKey(
        Invoice, on_delete=models.PROTECT, related_name='allocations',
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        verbose_name = 'Payment Allocation'
        verbose_name_plural = 'Payment Allocations'
        ordering = ['id']
        indexes = [
            models.Index(fields=['invoice', 'payment']),
        ]

    def __str__(self):
        return f"৳{self.amount:,.2f} → {self.invoice.display_number}"


# ══════════════════════════════════════════════════════════════════════════════
#  M5 — PURCHASING AND LANDED COST
# ══════════════════════════════════════════════════════════════════════════════

class Purchase(models.Model):
    """
    One shipment of stock coming in — a China consignment or a local buy.

    Import purchases are entered in two stages, because the weight charge does
    not exist until the shipment lands and the agent weighs it:

        stage 1 (ordered)   unit price and domestic shipping in RMB, FX rate
        stage 2 (received)  weight, per-kg rate, extra BD-side costs

    Between the two the cost is provisional. Only on receipt is the landed cost
    final, stock created, and the full value put on the ledger.
    """

    TYPE_IMPORT = 'import'
    TYPE_LOCAL  = 'local'
    TYPE_CHOICES = [
        (TYPE_IMPORT, 'Import (China / RMB)'),
        (TYPE_LOCAL,  'Local purchase (BDT)'),
    ]

    STATUS_DRAFT      = 'draft'
    STATUS_ORDERED    = 'ordered'
    STATUS_IN_TRANSIT = 'in_transit'
    STATUS_RECEIVED   = 'received'
    STATUS_CANCELLED  = 'cancelled'
    STATUS_CHOICES = [
        (STATUS_DRAFT,      'Draft'),
        (STATUS_ORDERED,    'Ordered'),
        (STATUS_IN_TRANSIT, 'In Transit'),
        (STATUS_RECEIVED,   'Received'),
        (STATUS_CANCELLED,  'Cancelled'),
    ]

    #: Statuses where the goods are paid for but not yet in stock.
    IN_FLIGHT_STATUSES = {STATUS_ORDERED, STATUS_IN_TRANSIT}

    purchase_no = models.CharField(max_length=20, unique=True, blank=True, db_index=True)
    purchase_type = models.CharField(
        max_length=10, choices=TYPE_CHOICES, default=TYPE_IMPORT, db_index=True,
    )
    supplier = models.ForeignKey(
        Party, on_delete=models.PROTECT, related_name='purchases',
    )
    purchase_date = models.DateField(default=today, db_index=True)

    # ── Import-only ───────────────────────────────────────────────────────
    fx_rate_rmb_to_bdt = models.DecimalField(
        max_digits=10, decimal_places=4, default=Decimal('0.0000'),
        help_text="৳ per ¥1. Only used on import purchases.",
    )
    default_per_kg_charge_bdt = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        help_text="Agent's per-kg rate. Each line can override it.",
    )
    billed_weight_kg = models.DecimalField(
        max_digits=10, decimal_places=3, default=Decimal('0.000'),
        help_text="The weight the agent actually billed. Line weights are "
                  "scaled to match it, so nothing is left unallocated.",
    )

    # ── Both types ────────────────────────────────────────────────────────
    extra_cost_bdt = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        help_text="BD-side costs — local shipping, repacking, clearing. "
                  "Spread across the lines by weight.",
    )
    correction_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('0.00'),
        help_text="Buffer added on top of everything — bank charges, breakage, "
                  "small unknowns.",
    )

    # ── Paying the supplier ───────────────────────────────────────────────
    # A purchase creates a debt to the supplier; this settles some or all of
    # it in the same step, which is how importing actually works — you pay
    # before the goods ship. Leave it blank to pay later from Payments.
    paid_from = models.ForeignKey(
        Account, null=True, blank=True,
        on_delete=models.PROTECT, related_name='purchases_paid',
        help_text="Which account the money came out of. Leave blank to pay later.",
    )
    amount_paid = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'),
        help_text="How much you paid the supplier now. Can be a part payment.",
    )
    payment = models.ForeignKey(
        'Payment', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='purchases',
        help_text="The payment record created for this purchase, if any.",
    )

    status = models.CharField(
        max_length=12, choices=STATUS_CHOICES, default=STATUS_DRAFT, db_index=True,
    )
    received_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True, default='')

    order_transaction = models.ForeignKey(
        Transaction, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='purchases_ordered',
        help_text="Posted when the purchase is marked ordered — goods in transit.",
    )
    receipt_transaction = models.ForeignKey(
        Transaction, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='purchases_received',
        help_text="Posted on receipt — moves the value into stock at landed cost.",
    )

    created_by = models.ForeignKey(
        'auth.User', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='finance_purchases',
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Purchase'
        verbose_name_plural = 'Purchases'
        ordering = ['-purchase_date', '-id']
        indexes = [
            models.Index(fields=['status', 'purchase_date']),
            models.Index(fields=['supplier', 'status']),
        ]

    def __str__(self):
        return f"{self.purchase_no or 'Draft purchase'} — {self.supplier.name}"

    def save(self, *args, **kwargs):
        if not self.purchase_no:
            # Retried: two people creating a purchase at the same moment would
            # otherwise compute the same number and one would hit the unique
            # constraint as a 500.
            from django.db import IntegrityError, transaction as db_transaction
            from .services import next_purchase_number

            for attempt in range(6):
                self.purchase_no = next_purchase_number()
                try:
                    with db_transaction.atomic():
                        return super().save(*args, **kwargs)
                except IntegrityError:
                    if attempt == 5:
                        raise
                    continue
        super().save(*args, **kwargs)

    # ── State ─────────────────────────────────────────────────────────────

    @property
    def is_import(self):
        return self.purchase_type == self.TYPE_IMPORT

    @property
    def is_editable(self):
        return self.status in (self.STATUS_DRAFT, self.STATUS_ORDERED,
                               self.STATUS_IN_TRANSIT)

    @property
    def is_received(self):
        return self.status == self.STATUS_RECEIVED

    @property
    def has_weights(self):
        """True once stage 2 has been filled in."""
        return self.billed_weight_kg > ZERO

    # ── Money ─────────────────────────────────────────────────────────────

    @property
    def goods_total_bdt(self):
        """Stage 1: everything known at order time, converted to taka."""
        return sum((item.goods_bdt for item in self.items.all()), ZERO)

    @property
    def entered_weight_total(self):
        return sum((item.entered_weight_kg for item in self.items.all()), ZERO)

    @property
    def effective_billed_weight(self):
        """
        The weight costs are spread over. Falls back to the sum of the line
        weights when the agent's billed figure has not been entered.
        """
        if self.billed_weight_kg > ZERO:
            return self.billed_weight_kg
        return self.entered_weight_total

    @property
    def weight_scale(self):
        """
        Factor that stretches the entered line weights onto the agent's billed
        weight. Agents round up and charge volumetric weight, so the two rarely
        match — this is what stops the difference going unbilled.
        """
        entered = self.entered_weight_total
        if entered <= ZERO or self.billed_weight_kg <= ZERO:
            return Decimal('1')
        return self.billed_weight_kg / entered

    @property
    def freight_total_bdt(self):
        return sum((item.freight_bdt for item in self.items.all()), ZERO)

    @property
    def shipping_total_bdt(self):
        """Weight charge plus the BD-side extras — the whole shipping pool."""
        return self.freight_total_bdt + self.extra_cost_bdt

    @property
    def subtotal_bdt(self):
        return self.goods_total_bdt + self.shipping_total_bdt

    @property
    def correction_amount_bdt(self):
        return (self.subtotal_bdt * self.correction_percent / Decimal('100'))

    @property
    def landed_total_bdt(self):
        return sum((item.landed_total_bdt for item in self.items.all()), ZERO)

    @property
    def total_quantity(self):
        return sum((item.quantity for item in self.items.all()), 0)

    @property
    def settled(self):
        """What has actually been paid against this purchase."""
        return self.payment.amount if self.payment_id else ZERO

    @property
    def still_owed(self):
        """
        What is left owing on this purchase.

        Uses the landed total once the shipment has been received, and the
        goods value before that — the freight is not known until it lands, so
        quoting a total that includes it would be a guess.
        """
        basis = self.landed_total_bdt if self.is_received else self.goods_total_bdt
        return basis - self.settled


class PurchaseItem(models.Model):
    """
    One product on a purchase.

    Every cost property recomputes from the parent's rates rather than storing
    a figure, so correcting an FX rate or a weight updates the whole shipment
    at once. On receipt the numbers are frozen into a StockBatch, which is what
    the sale side actually reads.
    """

    purchase = models.ForeignKey(
        Purchase, on_delete=models.CASCADE, related_name='items',
    )
    variant = models.ForeignKey(
        'store.ProductVariant', on_delete=models.PROTECT,
        related_name='purchase_items',
    )
    quantity = models.PositiveIntegerField(default=1)

    # ── Stage 1 — at order, in RMB (import) or BDT (local) ────────────────
    unit_price_rmb = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        help_text="Price per piece in ¥. Import purchases only.",
    )
    domestic_shipping_rmb = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        help_text="China-side shipping for this line, supplier → forwarder, in ¥.",
    )
    unit_cost_bdt = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        help_text="Price per piece in ৳. Local purchases only.",
    )
    local_transport_bdt = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        help_text="Transport for this line. Local purchases only.",
    )

    # ── Stage 2 — on arrival ──────────────────────────────────────────────
    entered_weight_kg = models.DecimalField(
        max_digits=10, decimal_places=3, default=Decimal('0.000'),
    )
    per_kg_charge_bdt = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Leave blank to use the shipment's default rate.",
    )

    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        verbose_name = 'Purchase Item'
        verbose_name_plural = 'Purchase Items'
        ordering = ['sort_order', 'id']

    def __str__(self):
        return f"{self.variant} x{self.quantity}"

    # ── Stage 1 ───────────────────────────────────────────────────────────

    @property
    def goods_rmb(self):
        return self.unit_price_rmb * self.quantity

    @property
    def line_rmb(self):
        return self.goods_rmb + self.domestic_shipping_rmb

    @property
    def goods_bdt(self):
        """Goods cost in taka — converted for imports, entered for local buys."""
        if self.purchase.is_import:
            return self.line_rmb * self.purchase.fx_rate_rmb_to_bdt
        return (self.unit_cost_bdt * self.quantity) + self.local_transport_bdt

    # ── Stage 2 ───────────────────────────────────────────────────────────

    @property
    def rate_per_kg(self):
        if self.per_kg_charge_bdt is not None:
            return self.per_kg_charge_bdt
        return self.purchase.default_per_kg_charge_bdt

    @property
    def scaled_weight_kg(self):
        """This line's weight, stretched to match what the agent billed."""
        return self.entered_weight_kg * self.purchase.weight_scale

    @property
    def freight_bdt(self):
        return self.scaled_weight_kg * self.rate_per_kg

    @property
    def extra_share_bdt(self):
        """This line's slice of the shipment's extra cost, split by weight."""
        billed = self.purchase.effective_billed_weight
        if billed <= ZERO:
            return ZERO
        return self.purchase.extra_cost_bdt * (self.scaled_weight_kg / billed)

    # ── Landed cost ───────────────────────────────────────────────────────

    @property
    def subtotal_bdt(self):
        return self.goods_bdt + self.freight_bdt + self.extra_share_bdt

    @property
    def correction_bdt(self):
        return self.subtotal_bdt * self.purchase.correction_percent / Decimal('100')

    @property
    def landed_total_bdt(self):
        """
        The authoritative figure. Per-unit cost is derived from this, never the
        other way round — otherwise rounding leaves small unexplained gaps in
        the ledger.
        """
        return (self.subtotal_bdt + self.correction_bdt).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP,
        )

    @property
    def landed_unit_bdt(self):
        if not self.quantity:
            return ZERO
        return (self.landed_total_bdt / self.quantity).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP,
        )

    @property
    def margin_bdt(self):
        """Selling price minus landed cost, per unit."""
        if not self.variant_id:
            return ZERO
        return self.variant.price - self.landed_unit_bdt

    @property
    def margin_percent(self):
        if not self.variant_id or not self.variant.price:
            return ZERO
        return (self.margin_bdt / self.variant.price * Decimal('100')).quantize(
            Decimal('0.1'),
        )


# ══════════════════════════════════════════════════════════════════════════════
#  M6 — STOCK, BATCHES AND FIFO
# ══════════════════════════════════════════════════════════════════════════════

class StockBatch(models.Model):
    """
    A quantity of one product received at one landed cost.

    FIFO consumes the oldest batch first, so each shipment's cost stays attached
    to its own units right through to the sale that uses them.
    """

    variant = models.ForeignKey(
        'store.ProductVariant', on_delete=models.PROTECT, related_name='stock_batches',
    )
    purchase_item = models.ForeignKey(
        PurchaseItem, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='batches',
        help_text="Blank for opening stock entered by hand.",
    )
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2)
    qty_received = models.PositiveIntegerField()
    qty_remaining = models.PositiveIntegerField()
    received_date = models.DateField(db_index=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = 'Stock Batch'
        verbose_name_plural = 'Stock Batches'
        # The FIFO order. Ties break by id so it is always deterministic.
        ordering = ['received_date', 'id']
        indexes = [
            models.Index(fields=['variant', 'received_date']),
            models.Index(fields=['variant', 'qty_remaining']),
        ]

    def __str__(self):
        return f"{self.variant} — {self.qty_remaining}/{self.qty_received} @ ৳{self.unit_cost}"

    @property
    def source_reference(self):
        if self.purchase_item_id and self.purchase_item.purchase_id:
            return self.purchase_item.purchase.purchase_no
        return 'Opening stock'

    @property
    def qty_used(self):
        return self.qty_received - self.qty_remaining

    @property
    def value_remaining(self):
        return self.unit_cost * self.qty_remaining


class StockMovement(models.Model):
    """
    Every change in stock, ever. Positive quantity is stock coming in,
    negative is stock going out.

    `ProductVariant.stock` is a cached sum of these, so when the number on the
    catalogue looks wrong the history is here to explain why.
    """

    REASON_PURCHASE  = 'purchase_receipt'
    REASON_SALE      = 'sale'
    REASON_RETURN    = 'sale_return'
    REASON_DAMAGE    = 'damage'
    REASON_ADJUST    = 'adjustment'
    REASON_OPENING   = 'opening'

    REASON_CHOICES = [
        (REASON_PURCHASE, 'Purchase received'),
        (REASON_SALE,     'Sold'),
        (REASON_RETURN,   'Returned by customer'),
        (REASON_DAMAGE,   'Damaged / lost'),
        (REASON_ADJUST,   'Manual adjustment'),
        (REASON_OPENING,  'Opening stock'),
    ]

    #: Reasons that bring stock in.
    INBOUND_REASONS = {REASON_PURCHASE, REASON_RETURN, REASON_OPENING}

    variant = models.ForeignKey(
        'store.ProductVariant', on_delete=models.PROTECT, related_name='stock_movements',
    )
    date = models.DateField(default=today, db_index=True)
    quantity = models.IntegerField(help_text="Signed — positive in, negative out.")
    reason = models.CharField(max_length=20, choices=REASON_CHOICES, db_index=True)
    reference = models.CharField(
        max_length=60, blank=True, default='', db_index=True,
        help_text="Order number, purchase number, or whatever explains it.",
    )
    note = models.CharField(max_length=255, blank=True, default='')

    batch = models.ForeignKey(
        StockBatch, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='inbound_movements',
        help_text="Set on inbound movements — the batch this created.",
    )
    transaction = models.ForeignKey(
        Transaction, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='stock_movements',
    )

    created_by = models.ForeignKey(
        'auth.User', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='finance_stock_movements',
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = 'Stock Movement'
        verbose_name_plural = 'Stock Movements'
        ordering = ['-date', '-id']
        indexes = [
            models.Index(fields=['variant', 'date']),
            models.Index(fields=['reason', 'date']),
        ]

    def __str__(self):
        return f"{self.variant} {self.quantity:+d} ({self.get_reason_display()})"

    @property
    def total_cost(self):
        """What this movement was worth, at the cost of the batches involved."""
        if self.quantity > 0 and self.batch_id:
            return self.batch.unit_cost * self.quantity
        return sum(
            (row.quantity * row.unit_cost for row in self.consumptions.all()), ZERO,
        )


class StockConsumption(models.Model):
    """
    Which batches an outbound movement drew from, and at what cost.

    This is what makes FIFO auditable rather than merely calculated — you can
    point at a sale and say exactly which shipment those units came from.
    """

    movement = models.ForeignKey(
        StockMovement, on_delete=models.CASCADE, related_name='consumptions',
    )
    batch = models.ForeignKey(
        StockBatch, on_delete=models.PROTECT, related_name='consumptions',
    )
    quantity = models.PositiveIntegerField()
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2)
    qty_returned = models.PositiveIntegerField(
        default=0,
        help_text="How much of this consumption has already been given back. "
                  "Stops the same sale being returned twice.",
    )

    class Meta:
        verbose_name = 'Stock Consumption'
        verbose_name_plural = 'Stock Consumptions'
        ordering = ['id']
        indexes = [
            models.Index(fields=['movement', 'batch']),
        ]

    def __str__(self):
        return f"{self.quantity} from {self.batch} @ ৳{self.unit_cost}"

    @property
    def total_cost(self):
        return self.unit_cost * self.quantity

    @property
    def qty_returnable(self):
        """How much of this consumption can still be given back."""
        return max(0, self.quantity - self.qty_returned)


# ══════════════════════════════════════════════════════════════════════════════
#  M7 — INVESTORS
# ══════════════════════════════════════════════════════════════════════════════

class Investor(models.Model):
    """
    Someone who has put money into the business.

    Each investor gets their own equity account, so "what is my partner's stake
    worth?" is a balance query rather than a spreadsheet.
    """

    name = models.CharField(max_length=150, db_index=True)
    phone = models.CharField(max_length=30, blank=True, default='')
    email = models.EmailField(blank=True, default='')
    notes = models.TextField(blank=True, default='')

    equity_account = models.OneToOneField(
        Account, null=True, blank=True,
        on_delete=models.PROTECT, related_name='investor_owner',
    )
    ownership_percent = models.DecimalField(
        max_digits=6, decimal_places=3, null=True, blank=True,
        help_text="Leave blank to work it out from capital contributed.",
    )

    is_active = models.BooleanField(default=True, db_index=True)
    joined_on = models.DateField(default=today)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = 'Investor'
        verbose_name_plural = 'Investors'
        ordering = ['name']

    def __str__(self):
        return self.name

    def get_equity_account(self):
        """This investor's capital account, created on first use."""
        if self.equity_account_id:
            return self.equity_account

        account = Account.objects.create(
            code=f'3010-{self.pk}',
            name=f'Capital — {self.name}',
            type=Account.TYPE_EQUITY,
            description=f'Money {self.name} has in the business',
            party_type=Account.PARTY_INVESTOR,
            party_id=self.pk,
        )
        Investor.objects.filter(pk=self.pk).update(equity_account=account)
        self.equity_account = account
        return account

    @property
    def current_stake(self):
        """What this investor's account stands at — capital in, less drawings."""
        if not self.equity_account_id:
            return ZERO
        return self.equity_account.balance()

    @property
    def capital_in(self):
        from .services import investor_capital_in
        return investor_capital_in(self)

    @property
    def drawings(self):
        from .services import investor_drawings
        return investor_drawings(self)

    @property
    def profit_share_total(self):
        """Excludes distributions that were undone — their entry is reversed."""
        return sum(
            (share.amount for share in self.profit_shares.select_related(
                'distribution__transaction')
             if not share.distribution.is_reversed),
            ZERO,
        )


class ProfitDistribution(models.Model):
    """
    A profit share-out for one period. Keeps the arithmetic on record so the
    split can be explained months later.
    """

    period_start = models.DateField()
    period_end = models.DateField()
    distribution_date = models.DateField(default=today, db_index=True)

    net_profit = models.DecimalField(max_digits=14, decimal_places=2)
    distributed_amount = models.DecimalField(max_digits=14, decimal_places=2)
    notes = models.TextField(blank=True, default='')

    transaction = models.ForeignKey(
        Transaction, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='profit_distributions',
    )
    created_by = models.ForeignKey(
        'auth.User', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='finance_distributions',
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = 'Profit Distribution'
        verbose_name_plural = 'Profit Distributions'
        ordering = ['-distribution_date', '-id']

    def __str__(self):
        return (f"Profit share {self.period_start} to {self.period_end} "
                f"— ৳{self.distributed_amount:,.2f}")

    @property
    def is_reversed(self):
        return bool(self.transaction_id and self.transaction.is_reversed)


class ProfitShare(models.Model):
    """One investor's slice of a distribution."""

    distribution = models.ForeignKey(
        ProfitDistribution, on_delete=models.CASCADE, related_name='shares',
    )
    investor = models.ForeignKey(
        Investor, on_delete=models.PROTECT, related_name='profit_shares',
    )
    ownership_percent = models.DecimalField(max_digits=6, decimal_places=3)
    amount = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        verbose_name = 'Profit Share'
        verbose_name_plural = 'Profit Shares'
        ordering = ['-amount']

    def __str__(self):
        return f"{self.investor.name} — ৳{self.amount:,.2f} ({self.ownership_percent}%)"


# ══════════════════════════════════════════════════════════════════════════════
#  M8 — LOANS
# ══════════════════════════════════════════════════════════════════════════════

class Loan(models.Model):
    """
    Money borrowed or lent, with an installment schedule.

    Interest is either flat (charged on the original principal for the whole
    term) or reducing balance (charged on what is still owed, the way banks
    quote EMIs).
    """

    DIRECTION_TAKEN = 'taken'
    DIRECTION_GIVEN = 'given'
    DIRECTION_CHOICES = [
        (DIRECTION_TAKEN, 'Loan taken (we borrowed)'),
        (DIRECTION_GIVEN, 'Loan given (we lent)'),
    ]

    METHOD_FLAT     = 'flat'
    METHOD_REDUCING = 'reducing'
    METHOD_CHOICES = [
        (METHOD_FLAT,     'Flat — interest on the original amount'),
        (METHOD_REDUCING, 'Reducing balance — interest on what is left (EMI)'),
    ]

    STATUS_ACTIVE    = 'active'
    STATUS_CLOSED    = 'closed'
    STATUS_CANCELLED = 'cancelled'
    STATUS_CHOICES = [
        (STATUS_ACTIVE,    'Active'),
        (STATUS_CLOSED,    'Closed'),
        (STATUS_CANCELLED, 'Cancelled'),
    ]

    loan_no = models.CharField(max_length=20, unique=True, blank=True, db_index=True)
    direction = models.CharField(
        max_length=6, choices=DIRECTION_CHOICES, default=DIRECTION_TAKEN, db_index=True,
    )
    counterparty_name = models.CharField(
        max_length=150,
        help_text="Who lent it to you, or who you lent it to.",
    )

    principal = models.DecimalField(max_digits=14, decimal_places=2)
    interest_rate = models.DecimalField(
        max_digits=6, decimal_places=3, default=Decimal('0.000'),
        help_text="Annual rate as a percentage. 0 for an interest-free loan.",
    )
    method = models.CharField(max_length=10, choices=METHOD_CHOICES, default=METHOD_FLAT)
    tenure_months = models.PositiveSmallIntegerField(
        default=12, help_text="Number of monthly installments.",
    )
    start_date = models.DateField(default=today)

    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default=STATUS_ACTIVE, db_index=True,
    )
    notes = models.TextField(blank=True, default='')

    account = models.ForeignKey(
        Account, null=True, blank=True,
        on_delete=models.PROTECT, related_name='loans',
        help_text="This loan's own account, created when it is recorded.",
    )
    transaction = models.ForeignKey(
        Transaction, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='loans',
    )

    created_by = models.ForeignKey(
        'auth.User', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='finance_loans',
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Loan'
        verbose_name_plural = 'Loans'
        ordering = ['-start_date', '-id']
        indexes = [
            models.Index(fields=['direction', 'status']),
        ]

    def __str__(self):
        return f"{self.loan_no} — {self.counterparty_name} (৳{self.principal:,.2f})"

    @property
    def is_taken(self):
        return self.direction == self.DIRECTION_TAKEN

    @property
    def outstanding(self):
        """What is still owed on this loan, principal only."""
        if not self.account_id:
            return ZERO
        return self.account.balance()

    @property
    def principal_paid(self):
        return sum((i.principal_paid for i in self.installments.all()), ZERO)

    @property
    def interest_paid(self):
        return sum((i.interest_paid for i in self.installments.all()), ZERO)

    @property
    def total_interest(self):
        return sum((i.interest_due for i in self.installments.all()), ZERO)

    @property
    def total_repayable(self):
        return self.principal + self.total_interest

    @property
    def installments_remaining(self):
        return self.installments.exclude(
            status=LoanInstallment.STATUS_PAID,
        ).count()

    @property
    def next_installment(self):
        return self.installments.exclude(
            status=LoanInstallment.STATUS_PAID,
        ).order_by('due_date').first()

    @property
    def overdue_installments(self):
        return [i for i in self.installments.all() if i.is_overdue]


class LoanInstallment(models.Model):
    """One scheduled repayment, with its principal and interest split out."""

    STATUS_DUE     = 'due'
    STATUS_PARTIAL = 'partial'
    STATUS_PAID    = 'paid'
    STATUS_CHOICES = [
        (STATUS_DUE,     'Due'),
        (STATUS_PARTIAL, 'Partly paid'),
        (STATUS_PAID,    'Paid'),
    ]

    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name='installments')
    number = models.PositiveSmallIntegerField()
    due_date = models.DateField(db_index=True)

    principal_due = models.DecimalField(max_digits=14, decimal_places=2)
    interest_due = models.DecimalField(max_digits=14, decimal_places=2)

    principal_paid = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'))
    interest_paid = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'))

    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default=STATUS_DUE, db_index=True)
    paid_date = models.DateField(null=True, blank=True)

    class Meta:
        verbose_name = 'Loan Installment'
        verbose_name_plural = 'Loan Installments'
        ordering = ['loan', 'number']
        unique_together = [['loan', 'number']]
        indexes = [
            models.Index(fields=['due_date', 'status']),
        ]

    def __str__(self):
        return f"{self.loan.loan_no} #{self.number} due {self.due_date}"

    @property
    def total_due(self):
        return self.principal_due + self.interest_due

    @property
    def total_paid(self):
        return self.principal_paid + self.interest_paid

    @property
    def outstanding(self):
        return self.total_due - self.total_paid

    @property
    def is_overdue(self):
        return self.status != self.STATUS_PAID and self.due_date < today()

    @property
    def days_overdue(self):
        if not self.is_overdue:
            return 0
        return (today() - self.due_date).days


class LoanPayment(models.Model):
    """One repayment against a loan, split between principal and interest."""

    loan = models.ForeignKey(Loan, on_delete=models.PROTECT, related_name='payments')
    installment = models.ForeignKey(
        LoanInstallment, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='payments',
    )
    date = models.DateField(default=today, db_index=True)

    principal_amount = models.DecimalField(max_digits=14, decimal_places=2)
    interest_amount = models.DecimalField(max_digits=14, decimal_places=2)

    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name='loan_payments',
        help_text="Where the money came from, or landed.",
    )
    reference = models.CharField(max_length=100, blank=True, default='')
    notes = models.TextField(blank=True, default='')

    transaction = models.ForeignKey(
        Transaction, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='loan_payments',
    )
    created_by = models.ForeignKey(
        'auth.User', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='finance_loan_payments',
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = 'Loan Payment'
        verbose_name_plural = 'Loan Payments'
        ordering = ['-date', '-id']

    def __str__(self):
        return f"{self.loan.loan_no} — ৳{self.total_amount:,.2f} on {self.date}"

    @property
    def total_amount(self):
        return self.principal_amount + self.interest_amount
