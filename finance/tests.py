"""
finance/tests.py
────────────────
M1 — the ledger's guarantees, proved.

Every balance the business will ever read is derived from these three tables,
so the invariants below are the foundation everything after M1 stands on:

    1. A transaction cannot be saved unless its lines sum to exactly zero.
    2. A posted transaction cannot be edited or deleted.
    3. Corrections are reversals, and a reversal cancels the original exactly.
    4. Balances read the way a person would say them, per account type.
    5. The trial balance sums to zero — always.

Run with:
    python manage.py test finance
"""

from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from .decorators import user_can_access_finance
from .exceptions import (
    AlreadyReversed, ImmutableTransaction, InactiveAccount, LedgerError,
    UnbalancedTransaction,
)
from .integrations import (
    affiliate_commission_owed, backfill_affiliate_history,
    on_commission_approved, on_order_delivered, on_withdrawal_paid,
)
from .models import (
    Account, Investor, Invoice, InvoiceItem, Loan, LoanInstallment, LoanPayment,
    Party, Payment, PaymentAllocation, ProfitDistribution, ProfitShare,
    Purchase, PurchaseItem, StockBatch, StockConsumption, StockMovement,
    Transaction, TransactionLine, today,
)
from .services import (
    account_ledger, accounts_with_balances, adjust_stock, ageing_report,
    auto_allocate, build_schedule, cancel_invoice, cancel_purchase,
    cash_on_hand, consume_stock, create_invoice_from_order, create_loan,
    current_unit_cost, daybook, distribute_profit, investor_statement,
    loan_summary, ownership_split, record_capital, record_drawing,
    record_loan_payment,
    get_opening_balance_account, has_opening_balance, invoice_amount_paid,
    issue_invoice, low_stock, margin_report, mark_purchase_ordered, money,
    next_invoice_number, next_purchase_number, open_invoices_for,
    party_statement, period_profit, post_expense, post_opening_balance,
    post_simple, post_transaction, post_transfer, purchase_batches,
    receivables_summary, receive_opening_stock, receive_purchase,
    record_payment, record_supplier_payment, refresh_invoice_status,
    return_stock, reverse_payment, reverse_transaction, stock_cost_history,
    stock_valuation, trial_balance, type_balance, variant_stock,
)

TODAY = date(2026, 8, 5)
YESTERDAY = TODAY - timedelta(days=1)
LAST_MONTH = TODAY - timedelta(days=35)
ZERO_D = Decimal('0.00')


class LedgerTestCase(TestCase):
    """Shared chart of accounts for the ledger tests."""

    def setUp(self):
        self.cash = Account.objects.create(
            code='1010', name='Cash in Hand', type=Account.TYPE_CASH)
        self.bkash = Account.objects.create(
            code='1030', name='bKash', type=Account.TYPE_MOBILE_MONEY)
        self.receivable = Account.objects.create(
            code='1200', name='Owed By Customers', type=Account.TYPE_RECEIVABLE)
        self.payable = Account.objects.create(
            code='2010', name='Owed To Suppliers', type=Account.TYPE_PAYABLE)
        self.loan = Account.objects.create(
            code='2200', name='Loans Taken', type=Account.TYPE_LOAN_PAYABLE)
        self.capital = Account.objects.create(
            code='3010', name="Owner's Capital", type=Account.TYPE_EQUITY)
        self.sales = Account.objects.create(
            code='4010', name='Product Sales', type=Account.TYPE_INCOME)
        self.rent = Account.objects.create(
            code='5110', name='Rent', type=Account.TYPE_EXPENSE)


# ══════════════════════════════════════════════════════════════════════════════
#  1. Balanced entries
# ══════════════════════════════════════════════════════════════════════════════

class BalanceEnforcementTests(LedgerTestCase):

    def test_balanced_transaction_posts(self):
        txn = post_transaction(
            date=TODAY,
            description='Paid August rent',
            lines=[
                (self.rent, Decimal('15000.00')),
                (self.cash, Decimal('-15000.00')),
            ],
        )
        self.assertEqual(txn.lines.count(), 2)
        self.assertTrue(txn.is_balanced)
        self.assertEqual(txn.total, Decimal('15000.00'))

    def test_unbalanced_transaction_is_rejected(self):
        with self.assertRaises(UnbalancedTransaction):
            post_transaction(
                date=TODAY,
                description='Typo — 1500 vs 15000',
                lines=[
                    (self.rent, Decimal('15000.00')),
                    (self.cash, Decimal('-1500.00')),
                ],
            )

    def test_nothing_is_written_when_unbalanced(self):
        """The atomic block must roll back the Transaction row too."""
        with self.assertRaises(UnbalancedTransaction):
            post_transaction(
                date=TODAY,
                description='Should leave no trace',
                lines=[(self.rent, Decimal('100.00')), (self.cash, Decimal('-90.00'))],
            )
        self.assertEqual(Transaction.objects.count(), 0)
        self.assertEqual(TransactionLine.objects.count(), 0)

    def test_single_line_is_rejected(self):
        with self.assertRaises(LedgerError):
            post_transaction(
                date=TODAY, description='Money from nowhere',
                lines=[(self.cash, Decimal('500.00'))],
            )

    def test_zero_amount_line_is_rejected(self):
        with self.assertRaises(LedgerError):
            post_transaction(
                date=TODAY, description='Pointless line',
                lines=[
                    (self.rent, Decimal('100.00')),
                    (self.cash, Decimal('-100.00')),
                    (self.bkash, Decimal('0.00')),
                ],
            )

    def test_multi_line_transaction_balances(self):
        """A sale split across two payment methods."""
        txn = post_transaction(
            date=TODAY,
            description='Sale settled part cash, part bKash',
            lines=[
                (self.cash, Decimal('300.00')),
                (self.bkash, Decimal('700.00')),
                (self.sales, Decimal('-1000.00')),
            ],
        )
        self.assertEqual(txn.lines.count(), 3)
        self.assertTrue(txn.is_balanced)

    def test_inactive_account_cannot_be_posted_to(self):
        self.rent.is_active = False
        self.rent.save()
        with self.assertRaises(InactiveAccount):
            post_simple(
                date=TODAY, description='Rent to a closed account',
                debit_account=self.rent, credit_account=self.cash,
                amount=Decimal('1000.00'),
            )

    def test_rounding_happens_before_the_balance_check(self):
        """
        Amounts are quantized to 2 places first. Values that only balance
        before rounding must be rejected, which is what forces M5 to allocate
        its remainders explicitly.
        """
        with self.assertRaises(UnbalancedTransaction):
            post_transaction(
                date=TODAY, description='Thirds of a taka',
                lines=[
                    (self.rent, Decimal('33.333')),
                    (self.rent, Decimal('33.333')),
                    (self.cash, Decimal('-66.666')),
                ],
            )

    def test_money_rounds_half_up(self):
        self.assertEqual(money('10.005'), Decimal('10.01'))
        self.assertEqual(money(1000), Decimal('1000.00'))
        self.assertEqual(money(Decimal('7.994')), Decimal('7.99'))


# ══════════════════════════════════════════════════════════════════════════════
#  2. Immutability
# ══════════════════════════════════════════════════════════════════════════════

class ImmutabilityTests(LedgerTestCase):

    def setUp(self):
        super().setUp()
        self.txn = post_simple(
            date=TODAY, description='Paid rent',
            debit_account=self.rent, credit_account=self.cash,
            amount=Decimal('15000.00'),
        )

    def test_transaction_cannot_be_edited(self):
        self.txn.description = 'Changed my mind'
        with self.assertRaises(ImmutableTransaction):
            self.txn.save()

    def test_transaction_cannot_be_deleted(self):
        with self.assertRaises(ImmutableTransaction):
            self.txn.delete()

    def test_line_cannot_be_edited(self):
        line = self.txn.lines.first()
        line.amount = Decimal('1.00')
        with self.assertRaises(ImmutableTransaction):
            line.save()

    def test_line_cannot_be_deleted(self):
        with self.assertRaises(ImmutableTransaction):
            self.txn.lines.first().delete()

    def test_is_reversed_flag_may_still_be_set(self):
        """The one permitted post-write change, used by reverse_transaction()."""
        self.txn.is_reversed = True
        self.txn.save(update_fields=['is_reversed'])
        self.txn.refresh_from_db()
        self.assertTrue(self.txn.is_reversed)

    def test_other_fields_cannot_sneak_through_update_fields(self):
        self.txn.description = 'Sneaky'
        with self.assertRaises(ImmutableTransaction):
            self.txn.save(update_fields=['description'])


# ══════════════════════════════════════════════════════════════════════════════
#  3. Reversals
# ══════════════════════════════════════════════════════════════════════════════

class ReversalTests(LedgerTestCase):

    def setUp(self):
        super().setUp()
        self.txn = post_simple(
            date=YESTERDAY, description='Rent paid twice by mistake',
            debit_account=self.rent, credit_account=self.cash,
            amount=Decimal('15000.00'),
        )

    def test_reversal_cancels_the_original_exactly(self):
        self.assertEqual(self.cash.balance(), Decimal('-15000.00'))

        reverse_transaction(self.txn, reason='Duplicate entry', date=TODAY)

        self.assertEqual(self.cash.balance(), Decimal('0.00'))
        self.assertEqual(self.rent.balance(), Decimal('0.00'))

    def test_original_is_marked_reversed_and_linked(self):
        reversal = reverse_transaction(self.txn, reason='Duplicate entry', date=TODAY)
        self.txn.refresh_from_db()
        reversal.refresh_from_db()

        self.assertTrue(self.txn.is_reversed)
        self.assertEqual(reversal.reversal_of_id, self.txn.pk)
        self.assertEqual(self.txn.reversals.count(), 1)
        self.assertIn(self.txn.reference_no, reversal.description)
        self.assertIn('Duplicate entry', reversal.description)

    def test_reversal_keeps_both_entries_in_history(self):
        reverse_transaction(self.txn, date=TODAY)
        self.assertEqual(Transaction.objects.count(), 2)
        self.assertEqual(TransactionLine.objects.count(), 4)

    def test_reversal_is_dated_today_by_default(self):
        reversal = reverse_transaction(self.txn)
        self.assertNotEqual(reversal.date, self.txn.date)

    def test_double_reversal_is_rejected(self):
        reverse_transaction(self.txn, date=TODAY)
        with self.assertRaises(AlreadyReversed):
            reverse_transaction(self.txn, date=TODAY)

    def test_reversal_source_points_back_at_the_original(self):
        reversal = reverse_transaction(self.txn, date=TODAY)
        self.assertEqual(reversal.source_type, Transaction.SOURCE_REVERSAL)
        self.assertEqual(reversal.source_id, self.txn.pk)


# ══════════════════════════════════════════════════════════════════════════════
#  4. Balances read the way a person says them
# ══════════════════════════════════════════════════════════════════════════════

class BalanceReadingTests(LedgerTestCase):

    def test_asset_balance_is_positive_when_money_is_present(self):
        post_simple(
            date=TODAY, description='Owner puts in capital',
            debit_account=self.cash, credit_account=self.capital,
            amount=Decimal('100000.00'),
        )
        self.assertEqual(self.cash.balance(), Decimal('100000.00'))

    def test_equity_balance_is_positive_when_capital_invested(self):
        """Raw sum is negative; the owner should still read +100000."""
        post_simple(
            date=TODAY, description='Owner puts in capital',
            debit_account=self.cash, credit_account=self.capital,
            amount=Decimal('100000.00'),
        )
        self.assertEqual(self.capital.signed_balance(), Decimal('-100000.00'))
        self.assertEqual(self.capital.balance(), Decimal('100000.00'))

    def test_loan_taken_reads_as_amount_owed(self):
        post_simple(
            date=TODAY, description='Borrowed from bank',
            debit_account=self.cash, credit_account=self.loan,
            amount=Decimal('20000.00'),
        )
        self.assertEqual(self.loan.balance(), Decimal('20000.00'))

    def test_income_reads_as_revenue_earned(self):
        post_simple(
            date=TODAY, description='Sold goods',
            debit_account=self.cash, credit_account=self.sales,
            amount=Decimal('5000.00'),
        )
        self.assertEqual(self.sales.balance(), Decimal('5000.00'))

    def test_payable_reads_as_amount_owed(self):
        post_simple(
            date=TODAY, description='Supplier bill received',
            debit_account=self.rent, credit_account=self.payable,
            amount=Decimal('7500.00'),
        )
        self.assertEqual(self.payable.balance(), Decimal('7500.00'))

    def test_cash_on_hand_spans_cash_bank_and_mobile_money(self):
        post_simple(date=TODAY, description='Capital in cash',
                    debit_account=self.cash, credit_account=self.capital,
                    amount=Decimal('5000.00'))
        post_simple(date=TODAY, description='Capital in bKash',
                    debit_account=self.bkash, credit_account=self.capital,
                    amount=Decimal('3000.00'))
        self.assertEqual(cash_on_hand(), Decimal('8000.00'))

    def test_type_balance_totals_receivables(self):
        post_simple(date=TODAY, description='Invoice issued',
                    debit_account=self.receivable, credit_account=self.sales,
                    amount=Decimal('12000.00'))
        self.assertEqual(type_balance([Account.TYPE_RECEIVABLE]), Decimal('12000.00'))

    def test_period_profit_is_income_minus_expense(self):
        post_simple(date=TODAY, description='Sale',
                    debit_account=self.cash, credit_account=self.sales,
                    amount=Decimal('10000.00'))
        post_simple(date=TODAY, description='Rent',
                    debit_account=self.rent, credit_account=self.cash,
                    amount=Decimal('4000.00'))
        result = period_profit()
        self.assertEqual(result['income'], Decimal('10000.00'))
        self.assertEqual(result['expense'], Decimal('4000.00'))
        self.assertEqual(result['profit'], Decimal('6000.00'))


# ══════════════════════════════════════════════════════════════════════════════
#  5. Date filtering
# ══════════════════════════════════════════════════════════════════════════════

class DateFilteringTests(LedgerTestCase):

    def setUp(self):
        super().setUp()
        post_simple(date=LAST_MONTH, description='Old sale',
                    debit_account=self.cash, credit_account=self.sales,
                    amount=Decimal('1000.00'))
        post_simple(date=TODAY, description='Recent sale',
                    debit_account=self.cash, credit_account=self.sales,
                    amount=Decimal('500.00'))

    def test_as_of_excludes_later_entries(self):
        self.assertEqual(self.cash.balance(as_of=YESTERDAY), Decimal('1000.00'))
        self.assertEqual(self.cash.balance(), Decimal('1500.00'))

    def test_since_excludes_earlier_entries(self):
        self.assertEqual(self.cash.balance(since=YESTERDAY), Decimal('500.00'))

    def test_since_and_as_of_bracket_a_period(self):
        self.assertEqual(
            self.cash.balance(since=YESTERDAY, as_of=TODAY),
            Decimal('500.00'),
        )

    def test_period_profit_respects_the_window(self):
        result = period_profit(since=YESTERDAY)
        self.assertEqual(result['income'], Decimal('500.00'))

    def test_annotated_balances_match_per_account_balances(self):
        annotated = {a.code: a.natural_total for a in accounts_with_balances()}
        self.assertEqual(annotated['1010'], self.cash.balance())
        self.assertEqual(annotated['4010'], self.sales.balance())


# ══════════════════════════════════════════════════════════════════════════════
#  6. Trial balance — the whole-ledger proof
# ══════════════════════════════════════════════════════════════════════════════

class TrialBalanceTests(LedgerTestCase):

    def test_empty_ledger_is_balanced(self):
        result = trial_balance()
        self.assertTrue(result['is_balanced'])
        self.assertEqual(result['total_signed'], Decimal('0.00'))

    def test_ledger_stays_balanced_across_many_transactions(self):
        post_simple(date=TODAY, description='Capital',
                    debit_account=self.cash, credit_account=self.capital,
                    amount=Decimal('100000.00'))
        post_simple(date=TODAY, description='Loan',
                    debit_account=self.bkash, credit_account=self.loan,
                    amount=Decimal('20000.00'))
        post_simple(date=TODAY, description='Rent',
                    debit_account=self.rent, credit_account=self.cash,
                    amount=Decimal('15000.00'))
        post_transaction(
            date=TODAY, description='Split sale',
            lines=[
                (self.cash, Decimal('300.00')),
                (self.bkash, Decimal('700.00')),
                (self.sales, Decimal('-1000.00')),
            ],
        )

        result = trial_balance()
        self.assertTrue(result['is_balanced'])
        self.assertEqual(result['total_signed'], Decimal('0.00'))

    def test_reversal_leaves_the_ledger_balanced(self):
        txn = post_simple(date=TODAY, description='Rent',
                          debit_account=self.rent, credit_account=self.cash,
                          amount=Decimal('15000.00'))
        reverse_transaction(txn, date=TODAY)
        self.assertTrue(trial_balance()['is_balanced'])

    def test_only_accounts_with_movement_are_listed(self):
        post_simple(date=TODAY, description='Rent',
                    debit_account=self.rent, credit_account=self.cash,
                    amount=Decimal('15000.00'))
        codes = {a.code for a in trial_balance()['accounts']}
        self.assertEqual(codes, {'1010', '5110'})


# ══════════════════════════════════════════════════════════════════════════════
#  7. Account ledger view helper
# ══════════════════════════════════════════════════════════════════════════════

class AccountLedgerTests(LedgerTestCase):

    def test_running_balance_accumulates_in_date_order(self):
        post_simple(date=LAST_MONTH, description='Capital',
                    debit_account=self.cash, credit_account=self.capital,
                    amount=Decimal('10000.00'))
        post_simple(date=YESTERDAY, description='Rent',
                    debit_account=self.rent, credit_account=self.cash,
                    amount=Decimal('4000.00'))
        post_simple(date=TODAY, description='Sale',
                    debit_account=self.cash, credit_account=self.sales,
                    amount=Decimal('1500.00'))

        rows = account_ledger(self.cash)
        self.assertEqual(
            [row['running_balance'] for row in rows],
            [Decimal('10000.00'), Decimal('6000.00'), Decimal('7500.00')],
        )
        self.assertEqual(rows[-1]['running_balance'], self.cash.balance())

    def test_running_balance_reads_naturally_for_credit_accounts(self):
        post_simple(date=YESTERDAY, description='Borrowed',
                    debit_account=self.cash, credit_account=self.loan,
                    amount=Decimal('20000.00'))
        post_simple(date=TODAY, description='Repaid part',
                    debit_account=self.loan, credit_account=self.cash,
                    amount=Decimal('5000.00'))

        rows = account_ledger(self.loan)
        self.assertEqual(
            [row['running_balance'] for row in rows],
            [Decimal('20000.00'), Decimal('15000.00')],
        )


# ══════════════════════════════════════════════════════════════════════════════
#  8. Metadata and access control
# ══════════════════════════════════════════════════════════════════════════════

class TransactionMetadataTests(LedgerTestCase):

    def test_reference_no_is_derived_from_pk(self):
        txn = post_simple(date=TODAY, description='Rent',
                          debit_account=self.rent, credit_account=self.cash,
                          amount=Decimal('100.00'))
        self.assertEqual(txn.reference_no, f'TXN-{txn.pk:06d}')

    def test_created_by_and_source_are_recorded(self):
        user = User.objects.create_user('owner', password='x', is_staff=True)
        txn = post_simple(
            date=TODAY, description='Rent',
            debit_account=self.rent, credit_account=self.cash,
            amount=Decimal('100.00'),
            source_type=Transaction.SOURCE_EXPENSE, source_id=42,
            created_by=user,
        )
        self.assertEqual(txn.created_by, user)
        self.assertEqual(txn.source_type, 'expense')
        self.assertEqual(txn.source_id, 42)

    def test_post_simple_rejects_a_negative_amount(self):
        with self.assertRaises(LedgerError):
            post_simple(date=TODAY, description='Backwards',
                        debit_account=self.rent, credit_account=self.cash,
                        amount=Decimal('-100.00'))


TEST_STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    # Production uses WhiteNoise's manifest storage, which requires
    # collectstatic to have run. Tests render real templates, so swap in the
    # plain backend rather than making the suite depend on a build step.
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}


@override_settings(STORAGES=TEST_STORAGES)
class AccessControlTests(TestCase):

    def test_finance_pages_reject_anonymous_visitors(self):
        response = self.client.get('/manage/finance/')
        self.assertEqual(response.status_code, 302)

    def test_finance_pages_reject_non_staff_users(self):
        User.objects.create_user('shopper', password='pw12345!')
        self.client.login(username='shopper', password='pw12345!')
        response = self.client.get('/manage/finance/')
        self.assertEqual(response.status_code, 302)

    def test_staff_can_open_every_finance_page(self):
        User.objects.create_user('owner', password='pw12345!', is_staff=True)
        self.client.login(username='owner', password='pw12345!')

        account = Account.objects.create(
            code='1010', name='Cash in Hand', type=Account.TYPE_CASH)
        other = Account.objects.create(
            code='3010', name="Owner's Capital", type=Account.TYPE_EQUITY)
        txn = post_simple(date=TODAY, description='Opening cash',
                          debit_account=account, credit_account=other,
                          amount=Decimal('5000.00'))

        for url in [
            '/manage/finance/',
            '/manage/finance/accounts/',
            f'/manage/finance/accounts/{account.pk}/',
            '/manage/finance/transactions/',
            f'/manage/finance/transactions/{txn.pk}/',
            '/manage/finance/trial-balance/',
        ]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)


# ══════════════════════════════════════════════════════════════════════════════
#  9. M2 — expenses, transfers, opening balances
# ══════════════════════════════════════════════════════════════════════════════

class M2TestCase(LedgerTestCase):
    """Adds the accounts M2's helpers look up by code."""

    def setUp(self):
        super().setUp()
        self.opening_equity = Account.objects.create(
            code='3900', name='Opening Balances', type=Account.TYPE_EQUITY)
        self.charges = Account.objects.create(
            code='5160', name='Bank & Payment Charges', type=Account.TYPE_EXPENSE)


class ExpenseTests(M2TestCase):

    def test_expense_moves_money_out_and_records_the_cost(self):
        post_opening_balance(account=self.cash, date=LAST_MONTH,
                             amount=Decimal('50000.00'))

        post_expense(
            date=TODAY, expense_account=self.rent, paid_from=self.cash,
            amount=Decimal('15000.00'), description='August shop rent',
        )

        self.assertEqual(self.cash.balance(), Decimal('35000.00'))
        self.assertEqual(self.rent.balance(), Decimal('15000.00'))

    def test_expense_is_tagged_so_the_expense_list_can_find_it(self):
        txn = post_expense(
            date=TODAY, expense_account=self.rent, paid_from=self.cash,
            amount=Decimal('100.00'), description='Rent',
        )
        self.assertEqual(txn.source_type, Transaction.SOURCE_EXPENSE)

    def test_expense_category_must_be_an_expense_account(self):
        with self.assertRaises(LedgerError):
            post_expense(
                date=TODAY, expense_account=self.sales, paid_from=self.cash,
                amount=Decimal('100.00'), description='Wrong category type',
            )

    def test_expense_must_be_paid_from_a_money_account(self):
        with self.assertRaises(LedgerError):
            post_expense(
                date=TODAY, expense_account=self.rent, paid_from=self.receivable,
                amount=Decimal('100.00'), description='Cannot pay from receivables',
            )

    def test_expense_reduces_profit(self):
        post_simple(date=TODAY, description='Sale',
                    debit_account=self.cash, credit_account=self.sales,
                    amount=Decimal('10000.00'))
        post_expense(date=TODAY, expense_account=self.rent, paid_from=self.cash,
                     amount=Decimal('4000.00'), description='Rent')

        self.assertEqual(period_profit()['profit'], Decimal('6000.00'))


class TransferTests(M2TestCase):

    def setUp(self):
        super().setUp()
        post_opening_balance(account=self.bkash, date=LAST_MONTH,
                             amount=Decimal('20000.00'))

    def test_transfer_moves_money_without_changing_total_cash(self):
        before = cash_on_hand()

        post_transfer(date=TODAY, from_account=self.bkash,
                      to_account=self.cash, amount=Decimal('5000.00'))

        self.assertEqual(self.bkash.balance(), Decimal('15000.00'))
        self.assertEqual(self.cash.balance(), Decimal('5000.00'))
        self.assertEqual(cash_on_hand(), before)

    def test_charge_is_taken_from_the_source_on_top_of_the_amount(self):
        post_transfer(date=TODAY, from_account=self.bkash, to_account=self.cash,
                      amount=Decimal('5000.00'), fee=Decimal('93.00'))

        self.assertEqual(self.cash.balance(), Decimal('5000.00'))
        self.assertEqual(self.bkash.balance(), Decimal('14907.00'))
        self.assertEqual(self.charges.balance(), Decimal('93.00'))

    def test_charge_reduces_total_cash_by_exactly_the_fee(self):
        before = cash_on_hand()
        post_transfer(date=TODAY, from_account=self.bkash, to_account=self.cash,
                      amount=Decimal('5000.00'), fee=Decimal('93.00'))
        self.assertEqual(cash_on_hand(), before - Decimal('93.00'))

    def test_transfer_to_the_same_account_is_rejected(self):
        with self.assertRaises(LedgerError):
            post_transfer(date=TODAY, from_account=self.bkash,
                          to_account=self.bkash, amount=Decimal('100.00'))

    def test_transfer_endpoints_must_be_money_accounts(self):
        with self.assertRaises(LedgerError):
            post_transfer(date=TODAY, from_account=self.bkash,
                          to_account=self.rent, amount=Decimal('100.00'))

    def test_negative_fee_is_rejected(self):
        with self.assertRaises(LedgerError):
            post_transfer(date=TODAY, from_account=self.bkash, to_account=self.cash,
                          amount=Decimal('100.00'), fee=Decimal('-5.00'))

    def test_description_is_filled_in_when_left_blank(self):
        txn = post_transfer(date=TODAY, from_account=self.bkash,
                            to_account=self.cash, amount=Decimal('100.00'))
        self.assertIn('bKash', txn.description)
        self.assertIn('Cash in Hand', txn.description)

    def test_transfer_keeps_the_ledger_balanced(self):
        post_transfer(date=TODAY, from_account=self.bkash, to_account=self.cash,
                      amount=Decimal('5000.00'), fee=Decimal('93.00'))
        self.assertTrue(trial_balance()['is_balanced'])


class OpeningBalanceTests(M2TestCase):

    def test_asset_opening_balance_reads_as_money_held(self):
        post_opening_balance(account=self.cash, date=LAST_MONTH,
                             amount=Decimal('50000.00'))
        self.assertEqual(self.cash.balance(), Decimal('50000.00'))
        self.assertEqual(self.opening_equity.balance(), Decimal('50000.00'))

    def test_liability_opening_balance_reads_as_money_owed(self):
        """Entered as a positive number even though it is a debt."""
        post_opening_balance(account=self.payable, date=LAST_MONTH,
                             amount=Decimal('20000.00'))
        self.assertEqual(self.payable.balance(), Decimal('20000.00'))

    def test_assets_and_liabilities_net_off_in_opening_equity(self):
        post_opening_balance(account=self.cash, date=LAST_MONTH,
                             amount=Decimal('50000.00'))
        post_opening_balance(account=self.payable, date=LAST_MONTH,
                             amount=Decimal('20000.00'))
        self.assertEqual(self.opening_equity.balance(), Decimal('30000.00'))

    def test_opening_balance_cannot_be_entered_twice(self):
        post_opening_balance(account=self.cash, date=LAST_MONTH,
                             amount=Decimal('50000.00'))
        with self.assertRaises(LedgerError):
            post_opening_balance(account=self.cash, date=LAST_MONTH,
                                 amount=Decimal('60000.00'))

    def test_reversing_an_opening_balance_frees_it_to_be_re_entered(self):
        txn = post_opening_balance(account=self.cash, date=LAST_MONTH,
                                   amount=Decimal('50000.00'))
        reverse_transaction(txn, reason='Wrong figure', date=TODAY)

        self.assertFalse(has_opening_balance(self.cash))
        post_opening_balance(account=self.cash, date=LAST_MONTH,
                             amount=Decimal('60000.00'))
        self.assertEqual(self.cash.balance(), Decimal('60000.00'))

    def test_zero_opening_balance_is_rejected(self):
        with self.assertRaises(LedgerError):
            post_opening_balance(account=self.cash, date=LAST_MONTH,
                                 amount=Decimal('0.00'))

    def test_opening_account_cannot_open_itself(self):
        with self.assertRaises(LedgerError):
            post_opening_balance(account=self.opening_equity, date=LAST_MONTH,
                                 amount=Decimal('100.00'))

    def test_opening_balances_leave_the_ledger_balanced(self):
        post_opening_balance(account=self.cash, date=LAST_MONTH,
                             amount=Decimal('50000.00'))
        post_opening_balance(account=self.bkash, date=LAST_MONTH,
                             amount=Decimal('20000.00'))
        self.assertTrue(trial_balance()['is_balanced'])

    def test_helper_finds_the_seeded_counterpart_account(self):
        self.assertEqual(get_opening_balance_account(), self.opening_equity)


class DaybookTests(M2TestCase):

    def setUp(self):
        super().setUp()
        post_opening_balance(account=self.cash, date=LAST_MONTH,
                             amount=Decimal('50000.00'))
        post_expense(date=YESTERDAY, expense_account=self.rent,
                     paid_from=self.cash, amount=Decimal('15000.00'),
                     description='July rent')
        post_simple(date=TODAY, description='Cash sale',
                    debit_account=self.cash, credit_account=self.sales,
                    amount=Decimal('3000.00'))

    def test_running_balance_ends_at_the_real_cash_position(self):
        book = daybook()
        self.assertEqual(book['closing_balance'], Decimal('38000.00'))
        self.assertEqual(book['closing_balance'], cash_on_hand())

    def test_totals_split_money_in_from_money_out(self):
        book = daybook()
        self.assertEqual(book['total_in'], Decimal('53000.00'))
        self.assertEqual(book['total_out'], Decimal('15000.00'))

    def test_opening_balance_reflects_the_position_before_the_window(self):
        book = daybook(since=YESTERDAY)
        self.assertEqual(book['opening_balance'], Decimal('50000.00'))
        self.assertEqual(book['closing_balance'], Decimal('38000.00'))

    def test_window_excludes_entries_outside_the_date_range(self):
        book = daybook(since=YESTERDAY, as_of=YESTERDAY)
        self.assertEqual(len(book['rows']), 1)
        self.assertEqual(book['closing_balance'], Decimal('35000.00'))

    def test_filtering_to_one_account_follows_only_that_wallet(self):
        post_opening_balance(account=self.bkash, date=LAST_MONTH,
                             amount=Decimal('9000.00'))
        book = daybook(account=self.bkash)
        self.assertEqual(book['closing_balance'], Decimal('9000.00'))
        self.assertEqual(len(book['rows']), 1)

    def test_daybook_rejects_a_non_money_account(self):
        with self.assertRaises(LedgerError):
            daybook(account=self.rent)

    def test_rows_carry_in_out_and_running_balance(self):
        rows = daybook()['rows']
        self.assertEqual(rows[0]['money_in'], Decimal('50000.00'))
        self.assertIsNone(rows[0]['money_out'])
        self.assertEqual(rows[1]['money_out'], Decimal('15000.00'))
        self.assertIsNone(rows[1]['money_in'])
        self.assertEqual(
            [r['running_balance'] for r in rows],
            [Decimal('50000.00'), Decimal('35000.00'), Decimal('38000.00')],
        )


# ══════════════════════════════════════════════════════════════════════════════
#  10. M2 — screens
# ══════════════════════════════════════════════════════════════════════════════

@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class M2ViewTests(TestCase):

    def setUp(self):
        from django.core.management import call_command
        from io import StringIO
        call_command('seed_chart_of_accounts', stdout=StringIO())

        self.user = User.objects.create_user('owner', password='pw12345!', is_staff=True)
        self.client.login(username='owner', password='pw12345!')

        self.cash = Account.objects.get(code='1010')
        self.bkash = Account.objects.get(code='1030')
        self.rent = Account.objects.get(code='5110')

    def test_all_m2_pages_load(self):
        for url in [
            '/manage/finance/daybook/',
            '/manage/finance/expenses/',
            '/manage/finance/expenses/new/',
            '/manage/finance/transfers/new/',
            '/manage/finance/accounts/new/',
            f'/manage/finance/accounts/{self.cash.pk}/edit/',
            f'/manage/finance/accounts/{self.cash.pk}/opening-balance/',
        ]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_pages_render_with_real_data_in_them(self):
        """Empty-state rendering is not proof the populated tables work."""
        post_opening_balance(account=self.cash, date=date(2026, 7, 1),
                             amount=Decimal('50000.00'))
        post_expense(date=TODAY, expense_account=self.rent, paid_from=self.cash,
                     amount=Decimal('15000.00'), description='August rent')
        post_transfer(date=TODAY, from_account=self.cash, to_account=self.bkash,
                      amount=Decimal('2000.00'), fee=Decimal('20.00'))

        for url in [
            '/manage/finance/',
            '/manage/finance/daybook/',
            '/manage/finance/expenses/',
            f'/manage/finance/expenses/?category={self.rent.pk}',
            '/manage/finance/accounts/',
            f'/manage/finance/accounts/{self.cash.pk}/',
            '/manage/finance/transactions/',
            '/manage/finance/trial-balance/',
        ]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_daybook_filters_apply_from_the_query_string(self):
        post_opening_balance(account=self.cash, date=date(2026, 7, 1),
                             amount=Decimal('50000.00'))
        response = self.client.get('/manage/finance/daybook/', {
            'since': '2026-08-01', 'as_of': '2026-08-31',
            'account': self.cash.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context['book']['opening_balance'], Decimal('50000.00'),
        )

    def test_daybook_rejects_a_backwards_date_range(self):
        response = self.client.get('/manage/finance/daybook/', {
            'since': '2026-08-31', 'as_of': '2026-08-01',
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['form'].is_valid())

    def test_m2_pages_reject_non_staff(self):
        self.client.logout()
        User.objects.create_user('shopper', password='pw12345!')
        self.client.login(username='shopper', password='pw12345!')
        self.assertEqual(
            self.client.get('/manage/finance/expenses/new/').status_code, 302,
        )

    def test_recording_an_expense_through_the_form_posts_to_the_ledger(self):
        response = self.client.post('/manage/finance/expenses/new/', {
            'date': '2026-08-05',
            'expense_account': self.rent.pk,
            'paid_from': self.cash.pk,
            'amount': '15000.00',
            'description': 'August shop rent',
            'memo': 'Receipt 4412',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.rent.balance(), Decimal('15000.00'))
        self.assertEqual(self.cash.balance(), Decimal('-15000.00'))

    def test_transfer_form_posts_with_a_charge(self):
        response = self.client.post('/manage/finance/transfers/new/', {
            'date': '2026-08-05',
            'from_account': self.bkash.pk,
            'to_account': self.cash.pk,
            'amount': '5000.00',
            'fee': '93.00',
            'description': '',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.cash.balance(), Decimal('5000.00'))
        self.assertEqual(self.bkash.balance(), Decimal('-5093.00'))
        self.assertEqual(Account.objects.get(code='5160').balance(), Decimal('93.00'))

    def test_transfer_form_rejects_same_account_both_sides(self):
        response = self.client.post('/manage/finance/transfers/new/', {
            'date': '2026-08-05',
            'from_account': self.cash.pk,
            'to_account': self.cash.pk,
            'amount': '100.00',
            'fee': '0',
        })
        self.assertEqual(response.status_code, 200)      # redisplayed with errors
        self.assertEqual(Transaction.objects.count(), 0)

    def test_opening_balance_form_posts(self):
        response = self.client.post(
            f'/manage/finance/accounts/{self.cash.pk}/opening-balance/',
            {'date': '2026-07-01', 'amount': '50000.00'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.cash.balance(), Decimal('50000.00'))

    def test_second_opening_balance_is_refused_by_the_view(self):
        self.client.post(
            f'/manage/finance/accounts/{self.cash.pk}/opening-balance/',
            {'date': '2026-07-01', 'amount': '50000.00'},
        )
        response = self.client.post(
            f'/manage/finance/accounts/{self.cash.pk}/opening-balance/',
            {'date': '2026-07-01', 'amount': '99999.00'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.cash.balance(), Decimal('50000.00'))

    def test_creating_an_expense_category_through_the_account_form(self):
        response = self.client.post('/manage/finance/accounts/new/', {
            'code': '5195',
            'name': 'Courier Charges',
            'type': Account.TYPE_EXPENSE,
            'description': 'Delivery partner fees',
            'is_active': 'on',
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Account.objects.filter(code='5195').exists())

    def test_system_account_code_and_type_are_locked(self):
        """Renaming is allowed; repurposing a seeded account is not."""
        self.client.post(f'/manage/finance/accounts/{self.cash.pk}/edit/', {
            'code': '9999',
            'name': 'Cash Drawer',
            'type': Account.TYPE_EXPENSE,
            'description': '',
            'is_active': 'on',
        })
        self.cash.refresh_from_db()
        self.assertEqual(self.cash.code, '1010')
        self.assertEqual(self.cash.type, Account.TYPE_CASH)
        self.assertEqual(self.cash.name, 'Cash Drawer')

    def test_reversing_through_the_view_cancels_the_entry(self):
        self.client.post('/manage/finance/expenses/new/', {
            'date': '2026-08-05',
            'expense_account': self.rent.pk,
            'paid_from': self.cash.pk,
            'amount': '15000.00',
            'description': 'Duplicate rent',
            'memo': '',
        })
        txn = Transaction.objects.get(source_type=Transaction.SOURCE_EXPENSE)

        response = self.client.post(
            f'/manage/finance/transactions/{txn.pk}/reverse/',
            {'reason': 'Entered twice'},
        )
        self.assertEqual(response.status_code, 302)

        txn.refresh_from_db()
        self.assertTrue(txn.is_reversed)
        self.assertEqual(self.rent.balance(), Decimal('0.00'))
        self.assertEqual(self.cash.balance(), Decimal('0.00'))

    def test_reversal_requires_a_reason(self):
        self.client.post('/manage/finance/expenses/new/', {
            'date': '2026-08-05', 'expense_account': self.rent.pk,
            'paid_from': self.cash.pk, 'amount': '100.00',
            'description': 'Rent', 'memo': '',
        })
        txn = Transaction.objects.get(source_type=Transaction.SOURCE_EXPENSE)

        self.client.post(f'/manage/finance/transactions/{txn.pk}/reverse/', {'reason': ''})
        txn.refresh_from_db()
        self.assertFalse(txn.is_reversed)


# ══════════════════════════════════════════════════════════════════════════════
#  11. M3 — parties and invoices
# ══════════════════════════════════════════════════════════════════════════════

class InvoiceTestCase(TestCase):
    """Seeded chart of accounts plus a wholesale client to bill."""

    def setUp(self):
        from django.core.management import call_command
        from io import StringIO
        call_command('seed_chart_of_accounts', stdout=StringIO())

        self.sales = Account.objects.get(code='4010')
        self.delivery_income = Account.objects.get(code='4020')
        self.cash = Account.objects.get(code='1010')

        self.client_party = Party.objects.create(
            name='Rahim Traders',
            party_type=Party.TYPE_WHOLESALE,
            phone='01711000000',
            credit_limit=Decimal('50000.00'),
        )

    def make_invoice(self, *, party=None, lines=None, discount='0.00',
                     delivery='0.00', terms=0, issue_date=TODAY):
        invoice = Invoice.objects.create(
            party=party or self.client_party,
            issue_date=issue_date,
            payment_terms_days=terms,
            discount=Decimal(discount),
            delivery_charge=Decimal(delivery),
        )
        for index, (description, price, qty) in enumerate(lines or [('Widget', '1000.00', 2)]):
            InvoiceItem.objects.create(
                invoice=invoice, description=description,
                unit_price=Decimal(price), quantity=qty, sort_order=index,
            )
        return invoice


class InvoiceNumberingTests(InvoiceTestCase):

    def test_first_number_of_the_year(self):
        self.assertEqual(next_invoice_number(year=2026), 'INV-2026-0001')

    def test_numbers_increment(self):
        issue_invoice(self.make_invoice())
        self.assertEqual(next_invoice_number(year=2026), 'INV-2026-0002')

    def test_series_restarts_each_january(self):
        issue_invoice(self.make_invoice(issue_date=date(2026, 12, 20)))
        issue_invoice(self.make_invoice(issue_date=date(2027, 1, 3)))

        numbers = list(Invoice.objects.order_by('id').values_list('number', flat=True))
        self.assertEqual(numbers, ['INV-2026-0001', 'INV-2027-0001'])

    def test_number_uses_the_issue_date_year_not_today(self):
        invoice = issue_invoice(self.make_invoice(issue_date=date(2025, 6, 1)))
        self.assertEqual(invoice.number, 'INV-2025-0001')

    def test_drafts_do_not_consume_numbers(self):
        self.make_invoice()
        self.make_invoice()
        invoice = issue_invoice(self.make_invoice())
        self.assertEqual(invoice.number, 'INV-2026-0001')

    def test_cancelled_draft_leaves_no_gap(self):
        discarded = self.make_invoice()
        cancel_invoice(discarded, reason='Changed mind')
        issued = issue_invoice(self.make_invoice())
        self.assertEqual(issued.number, 'INV-2026-0001')

    def test_numbers_stay_unique(self):
        for _ in range(5):
            issue_invoice(self.make_invoice())
        numbers = list(Invoice.objects.values_list('number', flat=True))
        self.assertEqual(len(numbers), len(set(numbers)))


class InvoiceTotalsTests(InvoiceTestCase):

    def test_subtotal_sums_the_lines(self):
        invoice = self.make_invoice(lines=[
            ('Cable', '250.00', 4),      # 1000
            ('Adapter', '600.00', 2),    # 1200
        ])
        self.assertEqual(invoice.subtotal, Decimal('2200.00'))

    def test_line_discount_reduces_that_line_only(self):
        invoice = self.make_invoice(lines=[('Cable', '250.00', 4)])
        item = invoice.items.first()
        item.discount = Decimal('100.00')
        item.save()

        self.assertEqual(item.gross_total, Decimal('1000.00'))
        self.assertEqual(item.line_total, Decimal('900.00'))
        self.assertEqual(invoice.subtotal, Decimal('900.00'))

    def test_invoice_discount_and_delivery_reach_the_total(self):
        invoice = self.make_invoice(
            lines=[('Widget', '1000.00', 12)], discount='500.00', delivery='100.00',
        )
        self.assertEqual(invoice.subtotal, Decimal('12000.00'))
        self.assertEqual(invoice.goods_total, Decimal('11500.00'))
        self.assertEqual(invoice.total, Decimal('11600.00'))

    def test_nothing_is_paid_before_any_payment_exists(self):
        invoice = self.make_invoice()
        self.assertEqual(invoice.amount_paid, ZERO_D)
        self.assertEqual(invoice.amount_due, invoice.total)


class InvoiceIssueTests(InvoiceTestCase):

    def test_issuing_posts_receivable_and_income(self):
        invoice = self.make_invoice(
            lines=[('Widget', '1000.00', 12)], discount='500.00', delivery='100.00',
        )
        issue_invoice(invoice)

        self.assertEqual(self.client_party.outstanding, Decimal('11600.00'))
        self.assertEqual(self.sales.balance(), Decimal('11500.00'))
        self.assertEqual(self.delivery_income.balance(), Decimal('100.00'))

    def test_issuing_creates_the_party_receivable_account(self):
        self.assertIsNone(self.client_party.receivable_account_id)
        issue_invoice(self.make_invoice())

        self.client_party.refresh_from_db()
        self.assertIsNotNone(self.client_party.receivable_account_id)
        self.assertEqual(self.client_party.receivable_account.code,
                         f'1200-{self.client_party.pk}')

    def test_issuing_sets_status_number_token_and_transaction(self):
        invoice = issue_invoice(self.make_invoice())
        self.assertEqual(invoice.status, Invoice.STATUS_SENT)
        self.assertTrue(invoice.number)
        self.assertTrue(invoice.share_token)
        self.assertIsNotNone(invoice.transaction_id)
        self.assertIsNotNone(invoice.issued_at)

    def test_due_date_comes_from_the_payment_terms(self):
        invoice = issue_invoice(self.make_invoice(terms=15, issue_date=TODAY))
        self.assertEqual(invoice.due_date, TODAY + timedelta(days=15))

    def test_due_on_receipt_is_due_the_same_day(self):
        invoice = issue_invoice(self.make_invoice(terms=0, issue_date=TODAY))
        self.assertEqual(invoice.due_date, TODAY)

    def test_delivery_line_is_skipped_when_there_is_no_delivery_charge(self):
        invoice = issue_invoice(self.make_invoice(delivery='0.00'))
        accounts = {line.account.code for line in invoice.transaction.lines.all()}
        self.assertNotIn('4020', accounts)

    def test_an_invoice_cannot_be_issued_twice(self):
        invoice = issue_invoice(self.make_invoice())
        with self.assertRaises(LedgerError):
            issue_invoice(invoice)

    def test_an_empty_invoice_cannot_be_issued(self):
        invoice = Invoice.objects.create(party=self.client_party, issue_date=TODAY)
        with self.assertRaises(LedgerError):
            issue_invoice(invoice)

    def test_a_discount_bigger_than_the_goods_is_refused(self):
        invoice = self.make_invoice(lines=[('Widget', '100.00', 1)], discount='500.00')
        with self.assertRaises(LedgerError):
            issue_invoice(invoice)

    def test_issuing_keeps_the_ledger_balanced(self):
        issue_invoice(self.make_invoice(delivery='100.00'))
        self.assertTrue(trial_balance()['is_balanced'])

    def test_a_draft_owes_nothing_until_issued(self):
        self.make_invoice()
        self.assertEqual(self.client_party.outstanding, ZERO_D)
        self.assertEqual(type_balance([Account.TYPE_RECEIVABLE]), ZERO_D)


class InvoiceCancelTests(InvoiceTestCase):

    def test_cancelling_an_issued_invoice_reverses_the_posting(self):
        invoice = issue_invoice(self.make_invoice(delivery='100.00'))
        self.assertEqual(self.client_party.outstanding, Decimal('2100.00'))

        cancel_invoice(invoice, reason='Client cancelled')

        self.assertEqual(self.client_party.outstanding, ZERO_D)
        self.assertEqual(self.sales.balance(), ZERO_D)
        self.assertEqual(invoice.status, Invoice.STATUS_CANCELLED)

    def test_cancelling_a_draft_posts_nothing(self):
        invoice = self.make_invoice()
        cancel_invoice(invoice, reason='Not needed')
        self.assertEqual(invoice.status, Invoice.STATUS_CANCELLED)
        self.assertEqual(Transaction.objects.count(), 0)

    def test_cancelling_twice_is_refused(self):
        invoice = self.make_invoice()
        cancel_invoice(invoice, reason='Not needed')
        with self.assertRaises(LedgerError):
            cancel_invoice(invoice, reason='Again')

    def test_original_and_reversal_both_stay_visible(self):
        invoice = issue_invoice(self.make_invoice())
        cancel_invoice(invoice, reason='Client cancelled')

        self.assertEqual(Transaction.objects.count(), 2)
        invoice.transaction.refresh_from_db()
        self.assertTrue(invoice.transaction.is_reversed)

    def test_cancelling_leaves_the_ledger_balanced(self):
        invoice = issue_invoice(self.make_invoice(delivery='100.00'))
        cancel_invoice(invoice, reason='Client cancelled')
        self.assertTrue(trial_balance()['is_balanced'])

    def test_an_invoice_with_money_received_cannot_be_cancelled(self):
        invoice = issue_invoice(self.make_invoice())
        record_payment(
            party=self.client_party, date=TODAY, amount=Decimal('500.00'),
            account=self.cash, allocations=[(invoice, Decimal('500.00'))],
        )
        with self.assertRaises(LedgerError):
            cancel_invoice(invoice, reason='Too late')


class InvoicePaymentDerivationTests(InvoiceTestCase):
    """amount_paid is read from the ledger, so M4 needs no new field."""

    def setUp(self):
        super().setUp()
        self.invoice = issue_invoice(self.make_invoice(lines=[('Widget', '1000.00', 10)]))
        self.receivable = self.client_party.get_receivable_account()

    def _pay(self, amount, invoice=None):
        invoice = invoice or self.invoice
        return record_payment(
            party=self.client_party, date=TODAY, amount=Decimal(amount),
            account=self.cash, allocations=[(invoice, Decimal(amount))],
        )

    def test_a_payment_shows_up_as_received(self):
        self._pay('4000.00')
        self.assertEqual(self.invoice.amount_paid, Decimal('4000.00'))
        self.assertEqual(self.invoice.amount_due, Decimal('6000.00'))

    def test_payments_accumulate(self):
        self._pay('4000.00')
        self._pay('2500.00')
        self.assertEqual(self.invoice.amount_paid, Decimal('6500.00'))

    def test_a_reversed_payment_stops_counting(self):
        # Through reverse_payment(), not the raw ledger: reversing a payment's
        # entry by hand is refused precisely so the invoice cannot be left
        # disagreeing with the books.
        payment = self._pay('4000.00')
        reverse_payment(payment, reason='Cheque bounced')
        self.assertEqual(self.invoice.amount_paid, ZERO_D)

    def test_a_payment_against_another_invoice_is_not_counted(self):
        other = issue_invoice(self.make_invoice())
        self._pay('900.00', invoice=other)

        self.assertEqual(self.invoice.amount_paid, ZERO_D)
        self.assertEqual(other.amount_paid, Decimal('900.00'))

    def test_status_follows_what_has_been_received(self):
        refresh_invoice_status(self.invoice)
        self.assertEqual(self.invoice.status, Invoice.STATUS_SENT)

        self._pay('4000.00')
        refresh_invoice_status(self.invoice)
        self.assertEqual(self.invoice.status, Invoice.STATUS_PARTIAL)

        self._pay('6000.00')
        refresh_invoice_status(self.invoice)
        self.assertEqual(self.invoice.status, Invoice.STATUS_PAID)


class InvoiceOverdueTests(InvoiceTestCase):
    """
    Overdue is judged against the real clock, so these anchor to today rather
    than the fixed TODAY constant — otherwise they would start failing on their
    own the moment the wall clock passed the hardcoded due date.
    """

    def setUp(self):
        super().setUp()
        self.real_today = today()

    def test_an_invoice_past_its_due_date_is_overdue(self):
        issued = self.real_today - timedelta(days=40)
        invoice = issue_invoice(self.make_invoice(issue_date=issued, terms=7))

        self.assertTrue(invoice.is_overdue)
        self.assertEqual(invoice.days_overdue, 33)
        self.assertEqual(invoice.display_status, 'Overdue')

    def test_an_invoice_within_terms_is_not_overdue(self):
        invoice = issue_invoice(
            self.make_invoice(issue_date=self.real_today, terms=30),
        )
        self.assertFalse(invoice.is_overdue)
        self.assertEqual(invoice.days_overdue, 0)

    def test_due_today_is_not_yet_overdue(self):
        invoice = issue_invoice(
            self.make_invoice(issue_date=self.real_today, terms=0),
        )
        self.assertEqual(invoice.due_date, self.real_today)
        self.assertFalse(invoice.is_overdue)

    def test_a_draft_is_never_overdue(self):
        invoice = self.make_invoice(issue_date=self.real_today - timedelta(days=40))
        invoice.due_date = self.real_today - timedelta(days=30)
        self.assertFalse(invoice.is_overdue)

    def test_a_cancelled_invoice_is_never_overdue(self):
        invoice = issue_invoice(
            self.make_invoice(issue_date=self.real_today - timedelta(days=40), terms=0),
        )
        cancel_invoice(invoice, reason='Cancelled')
        self.assertFalse(invoice.is_overdue)


class PartyTests(InvoiceTestCase):

    def test_receivable_account_is_created_only_once(self):
        first = self.client_party.get_receivable_account()
        second = self.client_party.get_receivable_account()
        self.assertEqual(first.pk, second.pk)

    def test_a_party_with_no_invoices_owes_nothing(self):
        fresh = Party.objects.create(name='New Client')
        self.assertEqual(fresh.outstanding, ZERO_D)

    def test_credit_limit_warning_triggers_above_the_limit(self):
        issue_invoice(self.make_invoice(lines=[('Bulk order', '30000.00', 2)]))
        self.assertEqual(self.client_party.outstanding, Decimal('60000.00'))
        self.assertTrue(self.client_party.is_over_credit_limit)

    def test_no_credit_limit_means_no_warning(self):
        self.client_party.credit_limit = ZERO_D
        self.client_party.save()
        issue_invoice(self.make_invoice(lines=[('Bulk order', '99000.00', 1)]))
        self.assertFalse(self.client_party.is_over_credit_limit)

    def test_outstanding_spans_several_invoices(self):
        issue_invoice(self.make_invoice(lines=[('A', '1000.00', 1)]))
        issue_invoice(self.make_invoice(lines=[('B', '2500.00', 1)]))
        self.assertEqual(self.client_party.outstanding, Decimal('3500.00'))


class InvoiceFromOrderTests(InvoiceTestCase):

    def setUp(self):
        super().setUp()
        from store.models import Category, Order, OrderItem, Product, ProductVariant

        category = Category.objects.create(name='Cables')
        product = Product.objects.create(category=category, name='USB-C Cable 2m')
        self.variant = ProductVariant.objects.create(
            product=product, name='Black', price=Decimal('450.00'), stock=100,
        )
        self.order = Order.objects.create(
            customer_name='Karim Uddin', customer_phone='01822000000',
            customer_email='karim@example.com',
            address_line='House 12, Road 5', city='Dhaka',
            subtotal=Decimal('900.00'), delivery_fee=Decimal('60.00'),
            grand_total=Decimal('960.00'),
        )
        OrderItem.objects.create(
            order=self.order, variant=self.variant,
            product_name='USB-C Cable 2m', variant_name='Black',
            sku=self.variant.sku, unit_price=Decimal('450.00'), quantity=2,
        )

    def test_order_becomes_a_draft_invoice_with_its_lines(self):
        invoice = create_invoice_from_order(self.order)

        self.assertEqual(invoice.status, Invoice.STATUS_DRAFT)
        self.assertEqual(invoice.items.count(), 1)
        self.assertEqual(invoice.subtotal, Decimal('900.00'))
        self.assertEqual(invoice.delivery_charge, Decimal('60.00'))
        self.assertEqual(invoice.total, Decimal('960.00'))

    def test_the_order_customer_becomes_a_retail_party(self):
        invoice = create_invoice_from_order(self.order)
        self.assertEqual(invoice.party.name, 'Karim Uddin')
        self.assertEqual(invoice.party.party_type, Party.TYPE_RETAIL)
        self.assertEqual(invoice.party.phone, '01822000000')

    def test_a_returning_customer_is_matched_by_phone(self):
        existing = Party.objects.create(name='Karim U.', phone='01822000000')
        invoice = create_invoice_from_order(self.order)
        self.assertEqual(invoice.party.pk, existing.pk)

    def test_the_invoice_links_back_to_the_order(self):
        invoice = create_invoice_from_order(self.order)
        self.assertEqual(invoice.order_id, self.order.pk)

    def test_an_order_cannot_be_invoiced_twice(self):
        create_invoice_from_order(self.order)
        with self.assertRaises(LedgerError):
            create_invoice_from_order(self.order)

    def test_re_invoicing_is_allowed_after_cancellation(self):
        first = create_invoice_from_order(self.order)
        cancel_invoice(first, reason='Wrong details')
        second = create_invoice_from_order(self.order)
        self.assertNotEqual(first.pk, second.pk)

    def test_line_details_are_snapshotted_not_looked_up(self):
        invoice = create_invoice_from_order(self.order)
        item = invoice.items.first()

        self.variant.price = Decimal('999.00')
        self.variant.save()

        item.refresh_from_db()
        self.assertEqual(item.unit_price, Decimal('450.00'))


# ══════════════════════════════════════════════════════════════════════════════
#  12. M3 — screens
# ══════════════════════════════════════════════════════════════════════════════

@override_settings(STORAGES=TEST_STORAGES)
class M3ViewTests(InvoiceTestCase):

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user('owner', password='pw12345!', is_staff=True)
        self.client.login(username='owner', password='pw12345!')

    def test_all_m3_pages_load(self):
        draft = self.make_invoice()
        issued = issue_invoice(self.make_invoice())

        for url in [
            '/manage/finance/parties/',
            '/manage/finance/parties/new/',
            f'/manage/finance/parties/{self.client_party.pk}/',
            f'/manage/finance/parties/{self.client_party.pk}/edit/',
            '/manage/finance/invoices/',
            '/manage/finance/invoices/new/',
            '/manage/finance/invoices/walk-in/',
            f'/manage/finance/invoices/{draft.pk}/',
            f'/manage/finance/invoices/{draft.pk}/edit/',
            f'/manage/finance/invoices/{issued.pk}/',
            f'/manage/finance/invoices/{issued.pk}/print/',
            '/manage/finance/invoices/?status=overdue',
        ]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_creating_an_invoice_through_the_form(self):
        response = self.client.post('/manage/finance/invoices/new/', {
            'party': self.client_party.pk,
            'issue_date': '2026-08-05',
            'payment_terms_days': '15',
            'discount': '0',
            'delivery_charge': '100.00',
            'notes': 'Thanks for your business',
            'items-TOTAL_FORMS': '4',
            'items-INITIAL_FORMS': '0',
            'items-MIN_NUM_FORMS': '0',
            'items-MAX_NUM_FORMS': '1000',
            'items-0-description': 'USB-C Cable',
            'items-0-sku': 'AC-001',
            'items-0-unit_price': '450.00',
            'items-0-quantity': '4',
            'items-0-discount': '0',
            'items-1-description': '',
            'items-1-sku': '',
            'items-1-unit_price': '',
            'items-1-quantity': '',
            'items-1-discount': '',
            'items-2-description': '',
            'items-2-sku': '',
            'items-2-unit_price': '',
            'items-2-quantity': '',
            'items-2-discount': '',
            'items-3-description': '',
            'items-3-sku': '',
            'items-3-unit_price': '',
            'items-3-quantity': '',
            'items-3-discount': '',
        })
        self.assertEqual(response.status_code, 302)

        invoice = Invoice.objects.latest('id')
        self.assertEqual(invoice.items.count(), 1)
        self.assertEqual(invoice.subtotal, Decimal('1800.00'))
        self.assertEqual(invoice.total, Decimal('1900.00'))
        self.assertEqual(invoice.status, Invoice.STATUS_DRAFT)

    def test_issuing_through_the_view(self):
        invoice = self.make_invoice()
        response = self.client.post(f'/manage/finance/invoices/{invoice.pk}/issue/')
        self.assertEqual(response.status_code, 302)

        invoice.refresh_from_db()
        # The view built its own Party instance, so ours does not yet know it
        # has a receivable account.
        self.client_party.refresh_from_db()

        self.assertEqual(invoice.status, Invoice.STATUS_SENT)
        self.assertEqual(self.client_party.outstanding, Decimal('2000.00'))

    def test_editing_an_issued_invoice_is_blocked(self):
        invoice = issue_invoice(self.make_invoice())
        response = self.client.get(f'/manage/finance/invoices/{invoice.pk}/edit/')
        self.assertEqual(response.status_code, 302)

    def test_cancelling_through_the_view(self):
        invoice = issue_invoice(self.make_invoice())
        response = self.client.post(
            f'/manage/finance/invoices/{invoice.pk}/cancel/',
            {'reason': 'Client changed their mind'},
        )
        self.assertEqual(response.status_code, 302)

        invoice.refresh_from_db()
        self.client_party.refresh_from_db()

        self.assertEqual(invoice.status, Invoice.STATUS_CANCELLED)
        self.assertIsNotNone(self.client_party.receivable_account_id)
        self.assertEqual(self.client_party.outstanding, ZERO_D)

    def test_walk_in_creates_a_party_and_a_draft(self):
        response = self.client.post('/manage/finance/invoices/walk-in/', {
            'customer_name': 'Shop Visitor',
            'phone': '01999000000',
            'issue_date': '2026-08-05',
        })
        self.assertEqual(response.status_code, 302)

        party = Party.objects.get(name='Shop Visitor')
        self.assertEqual(party.party_type, Party.TYPE_WALKIN)
        self.assertEqual(party.invoices.count(), 1)

    def test_creating_a_party_then_invoicing_carries_them_through(self):
        response = self.client.post('/manage/finance/parties/new/', {
            'name': 'New Wholesale Co',
            'party_type': Party.TYPE_WHOLESALE,
            'phone': '01700000000',
            'email': '',
            'address': '',
            'credit_limit': '0',
            'notes': '',
            'is_active': 'on',
            'then_invoice': '1',
        })
        party = Party.objects.get(name='New Wholesale Co')
        self.assertRedirects(
            response, f'/manage/finance/invoices/new/?party={party.pk}',
        )


@override_settings(STORAGES=TEST_STORAGES)
class InvoiceShareLinkTests(InvoiceTestCase):

    def test_an_issued_invoice_opens_without_logging_in(self):
        invoice = issue_invoice(self.make_invoice())
        response = self.client.get(f'/invoice/{invoice.share_token}/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, invoice.number)

    def test_a_cancelled_invoice_is_not_reachable(self):
        invoice = issue_invoice(self.make_invoice())
        token = invoice.share_token
        cancel_invoice(invoice, reason='Cancelled')
        self.assertEqual(self.client.get(f'/invoice/{token}/').status_code, 404)

    def test_an_unknown_token_is_not_found(self):
        self.assertEqual(
            self.client.get('/invoice/definitely-not-a-real-token/').status_code, 404,
        )

    def test_drafts_have_no_token_at_all(self):
        self.assertIsNone(self.make_invoice().share_token)

    def test_tokens_differ_between_invoices(self):
        first = issue_invoice(self.make_invoice())
        second = issue_invoice(self.make_invoice())
        self.assertNotEqual(first.share_token, second.share_token)


# ══════════════════════════════════════════════════════════════════════════════
#  13. M4 — payments, dues and ageing
# ══════════════════════════════════════════════════════════════════════════════

class PaymentTests(InvoiceTestCase):

    def setUp(self):
        super().setUp()
        self.bkash = Account.objects.get(code='1030')

    def test_a_payment_settles_the_invoice_and_brings_in_cash(self):
        invoice = issue_invoice(self.make_invoice(lines=[('Widget', '1000.00', 5)]))

        record_payment(party=self.client_party, date=TODAY,
                       amount=Decimal('5000.00'), account=self.bkash)

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.STATUS_PAID)
        self.assertEqual(invoice.amount_due, ZERO_D)
        self.assertEqual(self.bkash.balance(), Decimal('5000.00'))
        self.assertEqual(self.client_party.outstanding, ZERO_D)

    def test_a_part_payment_leaves_the_invoice_partially_paid(self):
        invoice = issue_invoice(self.make_invoice(lines=[('Widget', '1000.00', 5)]))

        record_payment(party=self.client_party, date=TODAY,
                       amount=Decimal('2000.00'), account=self.cash)

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.STATUS_PARTIAL)
        self.assertEqual(invoice.amount_due, Decimal('3000.00'))
        self.assertEqual(self.client_party.outstanding, Decimal('3000.00'))

    def test_auto_allocation_settles_the_oldest_invoice_first(self):
        older = issue_invoice(self.make_invoice(
            lines=[('A', '1000.00', 1)], issue_date=TODAY - timedelta(days=20)))
        newer = issue_invoice(self.make_invoice(
            lines=[('B', '2000.00', 1)], issue_date=TODAY))

        record_payment(party=self.client_party, date=TODAY,
                       amount=Decimal('1500.00'), account=self.cash)

        older.refresh_from_db()
        newer.refresh_from_db()
        self.assertEqual(older.status, Invoice.STATUS_PAID)
        self.assertEqual(newer.amount_paid, Decimal('500.00'))
        self.assertEqual(newer.status, Invoice.STATUS_PARTIAL)

    def test_one_payment_can_clear_several_invoices(self):
        first = issue_invoice(self.make_invoice(lines=[('A', '1000.00', 1)]))
        second = issue_invoice(self.make_invoice(lines=[('B', '2000.00', 1)]))

        payment = record_payment(party=self.client_party, date=TODAY,
                                 amount=Decimal('3000.00'), account=self.cash)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, Invoice.STATUS_PAID)
        self.assertEqual(second.status, Invoice.STATUS_PAID)
        self.assertEqual(payment.allocations.count(), 2)

    def test_money_beyond_what_is_owed_is_kept_as_an_advance(self):
        invoice = issue_invoice(self.make_invoice(lines=[('A', '1000.00', 1)]))

        payment = record_payment(party=self.client_party, date=TODAY,
                                 amount=Decimal('1500.00'), account=self.cash)

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.STATUS_PAID)
        self.assertEqual(payment.allocated, Decimal('1000.00'))
        self.assertEqual(payment.unallocated, Decimal('500.00'))
        # The advance shows as a credit — a negative amount owed.
        self.assertEqual(self.client_party.outstanding, Decimal('-500.00'))

    def test_a_payment_with_no_invoices_at_all_is_pure_advance(self):
        payment = record_payment(party=self.client_party, date=TODAY,
                                 amount=Decimal('2000.00'), account=self.cash)
        self.assertEqual(payment.allocations.count(), 0)
        self.assertEqual(payment.unallocated, Decimal('2000.00'))
        self.assertEqual(self.client_party.outstanding, Decimal('-2000.00'))

    def test_manual_allocation_targets_a_specific_invoice(self):
        older = issue_invoice(self.make_invoice(
            lines=[('A', '1000.00', 1)], issue_date=TODAY - timedelta(days=20)))
        newer = issue_invoice(self.make_invoice(
            lines=[('B', '2000.00', 1)], issue_date=TODAY))

        record_payment(party=self.client_party, date=TODAY,
                       amount=Decimal('2000.00'), account=self.cash,
                       allocations=[(newer, Decimal('2000.00'))])

        older.refresh_from_db()
        newer.refresh_from_db()
        self.assertEqual(older.status, Invoice.STATUS_SENT)
        self.assertEqual(newer.status, Invoice.STATUS_PAID)

    def test_over_allocating_a_single_invoice_is_refused(self):
        invoice = issue_invoice(self.make_invoice(lines=[('A', '1000.00', 1)]))
        with self.assertRaises(LedgerError):
            record_payment(party=self.client_party, date=TODAY,
                           amount=Decimal('5000.00'), account=self.cash,
                           allocations=[(invoice, Decimal('5000.00'))])

    def test_allocating_more_than_was_received_is_refused(self):
        first = issue_invoice(self.make_invoice(lines=[('A', '1000.00', 1)]))
        second = issue_invoice(self.make_invoice(lines=[('B', '1000.00', 1)]))
        with self.assertRaises(LedgerError):
            record_payment(party=self.client_party, date=TODAY,
                           amount=Decimal('1500.00'), account=self.cash,
                           allocations=[(first, Decimal('1000.00')),
                                        (second, Decimal('1000.00'))])

    def test_a_payment_cannot_be_applied_to_a_draft(self):
        draft = self.make_invoice()
        with self.assertRaises(LedgerError):
            record_payment(party=self.client_party, date=TODAY,
                           amount=Decimal('100.00'), account=self.cash,
                           allocations=[(draft, Decimal('100.00'))])

    def test_a_payment_cannot_be_applied_to_another_clients_invoice(self):
        other_party = Party.objects.create(name='Someone Else')
        theirs = issue_invoice(self.make_invoice(party=other_party))
        with self.assertRaises(LedgerError):
            record_payment(party=self.client_party, date=TODAY,
                           amount=Decimal('100.00'), account=self.cash,
                           allocations=[(theirs, Decimal('100.00'))])

    def test_a_payment_must_land_in_a_money_account(self):
        with self.assertRaises(LedgerError):
            record_payment(party=self.client_party, date=TODAY,
                           amount=Decimal('100.00'),
                           account=Account.objects.get(code='5110'))

    def test_a_zero_payment_is_refused(self):
        with self.assertRaises(LedgerError):
            record_payment(party=self.client_party, date=TODAY,
                           amount=ZERO_D, account=self.cash)

    def test_payments_keep_the_ledger_balanced(self):
        issue_invoice(self.make_invoice(lines=[('A', '1000.00', 5)]))
        record_payment(party=self.client_party, date=TODAY,
                       amount=Decimal('3000.00'), account=self.cash)
        self.assertTrue(trial_balance()['is_balanced'])


class PaymentReversalTests(InvoiceTestCase):

    def setUp(self):
        super().setUp()
        self.invoice = issue_invoice(self.make_invoice(lines=[('A', '1000.00', 5)]))
        self.payment = record_payment(
            party=self.client_party, date=TODAY,
            amount=Decimal('5000.00'), account=self.cash,
        )

    def test_reversing_makes_the_invoice_owed_again(self):
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.STATUS_PAID)

        reverse_payment(self.payment, reason='Cheque bounced')

        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.STATUS_SENT)
        self.assertEqual(self.invoice.amount_due, Decimal('5000.00'))
        self.assertEqual(self.client_party.outstanding, Decimal('5000.00'))

    def test_reversing_takes_the_cash_back_out(self):
        self.assertEqual(self.cash.balance(), Decimal('5000.00'))
        reverse_payment(self.payment, reason='Cheque bounced')
        self.assertEqual(self.cash.balance(), ZERO_D)

    def test_reversing_twice_is_refused(self):
        reverse_payment(self.payment, reason='Cheque bounced')
        with self.assertRaises(LedgerError):
            reverse_payment(self.payment, reason='Again')

    def test_allocations_survive_as_history(self):
        reverse_payment(self.payment, reason='Cheque bounced')
        self.assertEqual(self.payment.allocations.count(), 1)
        self.assertTrue(self.payment.is_reversed)

    def test_the_ledger_stays_balanced_after_a_reversal(self):
        reverse_payment(self.payment, reason='Cheque bounced')
        self.assertTrue(trial_balance()['is_balanced'])


class SupplierPaymentTests(InvoiceTestCase):

    def setUp(self):
        super().setUp()
        self.supplier = Party.objects.create(
            name='Guangzhou Supplier', party_type=Party.TYPE_SUPPLIER,
        )
        self.payable = Account.objects.get(code='2010')
        post_opening_balance(account=self.cash, date=LAST_MONTH,
                             amount=Decimal('100000.00'))
        post_opening_balance(account=self.payable, date=LAST_MONTH,
                             amount=Decimal('40000.00'))

    def test_paying_a_supplier_reduces_what_is_owed_and_the_cash(self):
        record_supplier_payment(
            party=self.supplier, date=TODAY, amount=Decimal('15000.00'),
            paid_from=self.cash,
        )
        self.assertEqual(self.payable.balance(), Decimal('25000.00'))
        self.assertEqual(self.cash.balance(), Decimal('85000.00'))

    def test_supplier_payment_is_tagged_as_outgoing(self):
        payment = record_supplier_payment(
            party=self.supplier, date=TODAY, amount=Decimal('100.00'),
            paid_from=self.cash,
        )
        self.assertEqual(payment.direction, Payment.DIRECTION_OUT)

    def test_supplier_payments_keep_the_ledger_balanced(self):
        record_supplier_payment(
            party=self.supplier, date=TODAY, amount=Decimal('15000.00'),
            paid_from=self.cash,
        )
        self.assertTrue(trial_balance()['is_balanced'])


class AgeingTests(InvoiceTestCase):

    def setUp(self):
        super().setUp()
        self.real_today = today()

    def _issue_due_days_ago(self, days_late, amount='1000.00'):
        issue_date = self.real_today - timedelta(days=days_late)
        return issue_invoice(self.make_invoice(
            lines=[('Item', amount, 1)], issue_date=issue_date, terms=0,
        ))

    def test_invoices_land_in_the_right_buckets(self):
        self._issue_due_days_ago(10, '1000.00')    # 0–30
        self._issue_due_days_ago(45, '2000.00')    # 31–60
        self._issue_due_days_ago(75, '3000.00')    # 61–90
        self._issue_due_days_ago(200, '4000.00')   # 90+

        report = ageing_report()
        self.assertEqual(report['bucket_totals']['0–30 days'], Decimal('1000.00'))
        self.assertEqual(report['bucket_totals']['31–60 days'], Decimal('2000.00'))
        self.assertEqual(report['bucket_totals']['61–90 days'], Decimal('3000.00'))
        self.assertEqual(report['bucket_totals']['90+ days'], Decimal('4000.00'))
        self.assertEqual(report['grand_total'], Decimal('10000.00'))

    def test_an_invoice_not_yet_due_sits_in_its_own_bucket(self):
        issue_invoice(self.make_invoice(
            lines=[('Item', '5000.00', 1)], issue_date=self.real_today, terms=30,
        ))
        report = ageing_report()
        self.assertEqual(report['bucket_totals']['Not yet due'], Decimal('5000.00'))

    def test_only_the_unpaid_part_is_aged(self):
        invoice = self._issue_due_days_ago(45, '2000.00')
        record_payment(party=self.client_party, date=self.real_today,
                       amount=Decimal('500.00'), account=self.cash,
                       allocations=[(invoice, Decimal('500.00'))])

        report = ageing_report()
        self.assertEqual(report['bucket_totals']['31–60 days'], Decimal('1500.00'))

    def test_a_settled_invoice_drops_off_the_report(self):
        invoice = self._issue_due_days_ago(45, '2000.00')
        record_payment(party=self.client_party, date=self.real_today,
                       amount=Decimal('2000.00'), account=self.cash,
                       allocations=[(invoice, Decimal('2000.00'))])

        self.assertEqual(ageing_report()['grand_total'], ZERO_D)

    def test_totals_are_grouped_per_client(self):
        other = Party.objects.create(name='Second Client')
        self._issue_due_days_ago(45, '2000.00')
        issue_invoice(self.make_invoice(
            party=other, lines=[('X', '7000.00', 1)],
            issue_date=self.real_today - timedelta(days=100), terms=0,
        ))

        report = ageing_report()
        self.assertEqual(len(report['parties']), 2)
        # Sorted biggest first.
        self.assertEqual(report['parties'][0]['total'], Decimal('7000.00'))

    def test_receivables_summary_counts_the_overdue_ones(self):
        self._issue_due_days_ago(45, '2000.00')
        issue_invoice(self.make_invoice(
            lines=[('Fresh', '900.00', 1)], issue_date=self.real_today, terms=30,
        ))

        summary = receivables_summary()
        self.assertEqual(summary['open_total'], Decimal('2900.00'))
        self.assertEqual(summary['overdue_total'], Decimal('2000.00'))
        self.assertEqual(summary['overdue_count'], 1)


class StatementTests(InvoiceTestCase):

    def test_statement_runs_a_balance_through_invoices_and_payments(self):
        issue_invoice(self.make_invoice(
            lines=[('A', '1000.00', 1)], issue_date=TODAY - timedelta(days=20)))
        record_payment(party=self.client_party, date=TODAY - timedelta(days=10),
                       amount=Decimal('600.00'), account=self.cash)
        issue_invoice(self.make_invoice(
            lines=[('B', '2000.00', 1)], issue_date=TODAY))

        statement = party_statement(self.client_party)

        self.assertEqual(len(statement['rows']), 3)
        self.assertEqual(statement['total_charged'], Decimal('3000.00'))
        self.assertEqual(statement['total_paid'], Decimal('600.00'))
        self.assertEqual(statement['closing_balance'], Decimal('2400.00'))
        self.assertEqual(statement['closing_balance'], self.client_party.outstanding)

    def test_drafts_are_left_off_the_statement(self):
        self.make_invoice()
        self.assertEqual(len(party_statement(self.client_party)['rows']), 0)

    def test_a_reversed_payment_leaves_the_statement(self):
        issue_invoice(self.make_invoice(lines=[('A', '1000.00', 1)]))
        payment = record_payment(party=self.client_party, date=TODAY,
                                 amount=Decimal('600.00'), account=self.cash)
        reverse_payment(payment, reason='Bounced')

        statement = party_statement(self.client_party)
        self.assertEqual(statement['total_paid'], ZERO_D)
        self.assertEqual(statement['closing_balance'], Decimal('1000.00'))

    def test_a_date_window_carries_an_opening_balance(self):
        issue_invoice(self.make_invoice(
            lines=[('Old', '1000.00', 1)], issue_date=TODAY - timedelta(days=40)))
        issue_invoice(self.make_invoice(
            lines=[('New', '500.00', 1)], issue_date=TODAY))

        statement = party_statement(self.client_party, since=TODAY - timedelta(days=5))
        self.assertEqual(statement['opening_balance'], Decimal('1000.00'))
        self.assertEqual(len(statement['rows']), 1)
        self.assertEqual(statement['closing_balance'], Decimal('1500.00'))


@override_settings(STORAGES=TEST_STORAGES)
class M4ViewTests(InvoiceTestCase):

    def setUp(self):
        super().setUp()
        User.objects.create_user('owner', password='pw12345!', is_staff=True)
        self.client.login(username='owner', password='pw12345!')
        self.invoice = issue_invoice(self.make_invoice(lines=[('A', '1000.00', 5)]))
        self.supplier = Party.objects.create(
            name='A Supplier', party_type=Party.TYPE_SUPPLIER)

    def test_all_m4_pages_load(self):
        for url in [
            '/manage/finance/dues/',
            '/manage/finance/dues/ageing/',
            '/manage/finance/payments/',
            '/manage/finance/payments/new/',
            f'/manage/finance/payments/new/?party={self.client_party.pk}',
            '/manage/finance/payments/supplier/',
            f'/manage/finance/parties/{self.client_party.pk}/statement/',
        ]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_recording_a_payment_through_the_form(self):
        response = self.client.post('/manage/finance/payments/new/', {
            'party': self.client_party.pk,
            'date': TODAY.isoformat(),
            'amount': '5000.00',
            'account': self.cash.pk,
            'reference': 'TRX998877',
            'notes': '',
        })
        self.assertEqual(response.status_code, 302)

        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.STATUS_PAID)
        self.assertEqual(self.cash.balance(), Decimal('5000.00'))

    def test_manual_allocation_through_the_form(self):
        second = issue_invoice(self.make_invoice(lines=[('B', '2000.00', 1)]))

        response = self.client.post('/manage/finance/payments/new/', {
            'party': self.client_party.pk,
            'date': TODAY.isoformat(),
            'amount': '2000.00',
            'account': self.cash.pk,
            'reference': '',
            'notes': '',
            'allocation_mode': 'manual',
            f'alloc_{second.pk}': '2000.00',
        })
        self.assertEqual(response.status_code, 302)

        self.invoice.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.STATUS_SENT)
        self.assertEqual(second.status, Invoice.STATUS_PAID)

    def test_reversing_a_payment_through_the_form(self):
        payment = record_payment(party=self.client_party, date=TODAY,
                                 amount=Decimal('5000.00'), account=self.cash)

        response = self.client.post(
            f'/manage/finance/payments/{payment.pk}/reverse/',
            {'reason': 'Cheque bounced'},
        )
        self.assertEqual(response.status_code, 302)

        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.STATUS_SENT)

    def test_paying_a_supplier_through_the_form(self):
        post_opening_balance(account=self.cash, date=LAST_MONTH,
                             amount=Decimal('50000.00'))
        response = self.client.post('/manage/finance/payments/supplier/', {
            'party': self.supplier.pk,
            'date': TODAY.isoformat(),
            'amount': '12000.00',
            'paid_from': self.cash.pk,
            'reference': '',
            'notes': '',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.cash.balance(), Decimal('38000.00'))


# ══════════════════════════════════════════════════════════════════════════════
#  14. M5 — purchasing and landed cost
# ══════════════════════════════════════════════════════════════════════════════

class PurchaseTestCase(TestCase):
    """Seeded accounts, a supplier, and two products to buy."""

    def setUp(self):
        from django.core.management import call_command
        from io import StringIO
        from store.models import Category, Product, ProductVariant

        call_command('seed_chart_of_accounts', stdout=StringIO())

        self.inventory = Account.objects.get(code='1300')
        self.in_transit = Account.objects.get(code='1350')
        self.payable = Account.objects.get(code='2010')
        self.accrued = Account.objects.get(code='2150')
        self.cogs = Account.objects.get(code='5010')
        self.cash = Account.objects.get(code='1010')

        self.supplier = Party.objects.create(
            name='Guangzhou Trading', party_type=Party.TYPE_SUPPLIER,
        )

        category = Category.objects.create(name='Accessories')
        product_a = Product.objects.create(category=category, name='Product A')
        product_b = Product.objects.create(category=category, name='Product B')
        self.variant_a = ProductVariant.objects.create(
            product=product_a, name='Standard', price=Decimal('1500.00'), stock=0)
        self.variant_b = ProductVariant.objects.create(
            product=product_b, name='Standard', price=Decimal('500.00'), stock=0)

    def make_import(self, *, fx='17.50', per_kg='100.00', billed_weight='10.000',
                    extra='0.00', correction='0.00', lines=None):
        purchase = Purchase.objects.create(
            purchase_type=Purchase.TYPE_IMPORT,
            supplier=self.supplier,
            purchase_date=TODAY,
            fx_rate_rmb_to_bdt=Decimal(fx),
            default_per_kg_charge_bdt=Decimal(per_kg),
            billed_weight_kg=Decimal(billed_weight),
            extra_cost_bdt=Decimal(extra),
            correction_percent=Decimal(correction),
        )
        for index, spec in enumerate(lines or []):
            PurchaseItem.objects.create(
                purchase=purchase,
                variant=spec['variant'],
                quantity=spec['qty'],
                unit_price_rmb=Decimal(spec.get('rmb', '0.00')),
                domestic_shipping_rmb=Decimal(spec.get('shipping_rmb', '0.00')),
                entered_weight_kg=Decimal(spec.get('weight', '0.000')),
                per_kg_charge_bdt=(
                    Decimal(spec['per_kg']) if 'per_kg' in spec else None
                ),
                sort_order=index,
            )
        return purchase


class LandedCostTests(PurchaseTestCase):
    """The formula the owner specified, checked against their own numbers."""

    def test_the_owners_worked_example(self):
        """
        10 pcs, 10 kg total, ৳100/kg  →  ৳1,000 freight  →  ৳100 per piece.
        """
        purchase = self.make_import(
            fx='17.50', per_kg='100.00', billed_weight='10.000',
            lines=[{'variant': self.variant_a, 'qty': 10, 'rmb': '50.00',
                    'shipping_rmb': '30.00', 'weight': '10.000'}],
        )
        item = purchase.items.first()

        self.assertEqual(item.goods_rmb, Decimal('500.00'))
        self.assertEqual(item.line_rmb, Decimal('530.00'))
        self.assertEqual(item.goods_bdt, Decimal('9275.000'))
        self.assertEqual(item.freight_bdt, Decimal('1000.000'))
        # The ৳100-a-piece weight charge the owner described.
        self.assertEqual(item.freight_bdt / item.quantity, Decimal('100.00'))
        self.assertEqual(item.landed_total_bdt, Decimal('10275.00'))
        self.assertEqual(item.landed_unit_bdt, Decimal('1027.50'))

    def test_the_correction_percent_applies_to_the_whole_subtotal(self):
        purchase = self.make_import(
            fx='17.50', per_kg='100.00', billed_weight='10.000', correction='5.00',
            lines=[{'variant': self.variant_a, 'qty': 10, 'rmb': '50.00',
                    'shipping_rmb': '30.00', 'weight': '10.000'}],
        )
        item = purchase.items.first()

        # (9275 + 1000) x 1.05
        self.assertEqual(item.landed_total_bdt, Decimal('10788.75'))
        self.assertEqual(item.landed_unit_bdt, Decimal('1078.88'))

    def test_two_product_example_from_the_plan(self):
        """
        Entered 10kg, agent billed 11.5kg, ৳500 extra cost, 5% correction.
        """
        purchase = self.make_import(
            fx='17.50', per_kg='100.00', billed_weight='11.500',
            extra='500.00', correction='5.00',
            lines=[
                {'variant': self.variant_a, 'qty': 10, 'rmb': '50.00',
                 'shipping_rmb': '30.00', 'weight': '6.000'},
                {'variant': self.variant_b, 'qty': 20, 'rmb': '15.00',
                 'shipping_rmb': '20.00', 'weight': '4.000'},
            ],
        )
        first, second = list(purchase.items.all())

        self.assertEqual(purchase.weight_scale, Decimal('1.15'))
        self.assertEqual(first.scaled_weight_kg, Decimal('6.900'))
        self.assertEqual(second.scaled_weight_kg, Decimal('4.600'))

        self.assertEqual(first.goods_bdt, Decimal('9275.000'))
        self.assertEqual(second.goods_bdt, Decimal('5600.000'))
        self.assertEqual(first.freight_bdt, Decimal('690.000'))
        self.assertEqual(second.freight_bdt, Decimal('460.000'))
        self.assertEqual(first.extra_share_bdt, Decimal('300.00'))
        self.assertEqual(second.extra_share_bdt, Decimal('200.00'))

        self.assertEqual(first.landed_total_bdt, Decimal('10778.25'))
        self.assertEqual(second.landed_total_bdt, Decimal('6573.00'))
        self.assertEqual(first.landed_unit_bdt, Decimal('1077.83'))
        self.assertEqual(second.landed_unit_bdt, Decimal('328.65'))

    def test_freight_totals_exactly_what_the_agent_billed(self):
        purchase = self.make_import(
            per_kg='100.00', billed_weight='11.500',
            lines=[
                {'variant': self.variant_a, 'qty': 10, 'rmb': '50.00', 'weight': '6.000'},
                {'variant': self.variant_b, 'qty': 20, 'rmb': '15.00', 'weight': '4.000'},
            ],
        )
        self.assertEqual(purchase.freight_total_bdt, Decimal('1150.000'))

    def test_extra_cost_is_fully_allocated_by_weight(self):
        purchase = self.make_import(
            billed_weight='10.000', extra='500.00',
            lines=[
                {'variant': self.variant_a, 'qty': 10, 'rmb': '50.00', 'weight': '6.000'},
                {'variant': self.variant_b, 'qty': 20, 'rmb': '15.00', 'weight': '4.000'},
            ],
        )
        shares = sum((item.extra_share_bdt for item in purchase.items.all()), ZERO_D)
        self.assertEqual(shares, Decimal('500.00'))

    def test_a_line_can_override_the_per_kg_rate(self):
        purchase = self.make_import(
            per_kg='100.00', billed_weight='10.000',
            lines=[
                {'variant': self.variant_a, 'qty': 1, 'rmb': '10.00', 'weight': '6.000'},
                {'variant': self.variant_b, 'qty': 1, 'rmb': '10.00',
                 'weight': '4.000', 'per_kg': '140.00'},
            ],
        )
        first, second = list(purchase.items.all())
        self.assertEqual(first.rate_per_kg, Decimal('100.00'))
        self.assertEqual(second.rate_per_kg, Decimal('140.00'))
        self.assertEqual(second.freight_bdt, Decimal('560.000'))

    def test_line_weights_scale_up_to_the_billed_weight(self):
        purchase = self.make_import(
            billed_weight='11.500',
            lines=[{'variant': self.variant_a, 'qty': 1, 'rmb': '1.00', 'weight': '10.000'}],
        )
        self.assertEqual(purchase.items.first().scaled_weight_kg, Decimal('11.500'))

    def test_local_purchase_uses_taka_directly(self):
        purchase = Purchase.objects.create(
            purchase_type=Purchase.TYPE_LOCAL, supplier=self.supplier,
            purchase_date=TODAY, correction_percent=Decimal('5.00'),
        )
        PurchaseItem.objects.create(
            purchase=purchase, variant=self.variant_a, quantity=10,
            unit_cost_bdt=Decimal('900.00'), local_transport_bdt=Decimal('500.00'),
        )
        item = purchase.items.first()

        self.assertEqual(item.goods_bdt, Decimal('9500.00'))
        self.assertEqual(item.landed_total_bdt, Decimal('9975.00'))
        self.assertEqual(item.landed_unit_bdt, Decimal('997.50'))

    def test_purchase_numbers_run_in_sequence(self):
        first = self.make_import()
        second = self.make_import()
        self.assertEqual(first.purchase_no, 'PUR-0001')
        self.assertEqual(second.purchase_no, 'PUR-0002')


class PurchasePostingTests(PurchaseTestCase):

    def _full_purchase(self):
        return self.make_import(
            fx='17.50', per_kg='100.00', billed_weight='10.000',
            extra='500.00', correction='5.00',
            lines=[{'variant': self.variant_a, 'qty': 10, 'rmb': '50.00',
                    'shipping_rmb': '30.00', 'weight': '10.000'}],
        )

    def test_confirming_an_order_books_goods_in_transit(self):
        purchase = self._full_purchase()
        mark_purchase_ordered(purchase)

        self.assertEqual(purchase.status, Purchase.STATUS_ORDERED)
        self.assertEqual(self.in_transit.balance(), Decimal('9275.00'))
        self.assertEqual(self.payable.balance(), Decimal('9275.00'))

    def test_receiving_moves_the_value_into_stock(self):
        purchase = self._full_purchase()
        mark_purchase_ordered(purchase)
        receive_purchase(purchase)

        self.assertEqual(purchase.status, Purchase.STATUS_RECEIVED)
        self.assertEqual(self.in_transit.balance(), ZERO_D)

        # The ledger carries the batch value — unit cost x quantity — because
        # that is what the stock is actually worth once FIFO has a per-unit
        # figure to work with.
        batch = StockBatch.objects.get(variant=self.variant_a)
        self.assertEqual(self.inventory.balance(), batch.unit_cost * batch.qty_received)

    def test_rounding_the_unit_cost_never_leaves_the_ledger_adrift(self):
        """
        ৳11,313.75 over 10 units is ৳1,131.375 each. Rounding to ৳1,131.38 and
        multiplying back gives ৳11,313.80 — five paisa more than the line total.
        The inventory figure follows the batch, and the difference is absorbed
        by the supplier line so nothing is left dangling.
        """
        purchase = self._full_purchase()
        receive_purchase(purchase)

        batch = StockBatch.objects.get(variant=self.variant_a)
        batch_value = batch.unit_cost * batch.qty_received

        self.assertEqual(purchase.landed_total_bdt, Decimal('11313.75'))
        self.assertEqual(batch.unit_cost, Decimal('1131.38'))
        self.assertEqual(batch_value, Decimal('11313.80'))
        self.assertEqual(self.inventory.balance(), batch_value)
        self.assertTrue(trial_balance()['is_balanced'])

    def test_the_correction_goes_to_an_accrual_not_to_payables(self):
        purchase = self._full_purchase()
        receive_purchase(purchase)
        self.assertEqual(self.accrued.balance(), money(purchase.correction_amount_bdt))

    def test_receiving_creates_a_batch_and_a_movement(self):
        purchase = self._full_purchase()
        receive_purchase(purchase)

        batch = StockBatch.objects.get(variant=self.variant_a)
        self.assertEqual(batch.qty_received, 10)
        self.assertEqual(batch.qty_remaining, 10)
        self.assertEqual(batch.unit_cost, purchase.items.first().landed_unit_bdt)

        movement = StockMovement.objects.get(reason=StockMovement.REASON_PURCHASE)
        self.assertEqual(movement.quantity, 10)
        self.assertEqual(movement.reference, purchase.purchase_no)

    def test_receiving_updates_the_catalogue_stock_figure(self):
        purchase = self._full_purchase()
        receive_purchase(purchase)
        self.variant_a.refresh_from_db()
        self.assertEqual(self.variant_a.stock, 10)

    def test_receiving_without_the_weight_is_refused(self):
        purchase = self.make_import(
            billed_weight='0.000',
            lines=[{'variant': self.variant_a, 'qty': 10, 'rmb': '50.00', 'weight': '0.000'}],
        )
        with self.assertRaises(LedgerError):
            receive_purchase(purchase)

    def test_receiving_without_an_fx_rate_is_refused(self):
        purchase = self.make_import(
            fx='0.0000', billed_weight='10.000',
            lines=[{'variant': self.variant_a, 'qty': 10, 'rmb': '50.00', 'weight': '10.000'}],
        )
        with self.assertRaises(LedgerError):
            receive_purchase(purchase)

    def test_receiving_twice_is_refused(self):
        purchase = self._full_purchase()
        receive_purchase(purchase)
        with self.assertRaises(LedgerError):
            receive_purchase(purchase)

    def test_the_ledger_stays_balanced_through_the_whole_flow(self):
        purchase = self._full_purchase()
        mark_purchase_ordered(purchase)
        receive_purchase(purchase)
        self.assertTrue(trial_balance()['is_balanced'])

    def test_stock_value_matches_the_ledger_after_receiving(self):
        purchase = self._full_purchase()
        receive_purchase(purchase)

        valuation = stock_valuation()
        self.assertEqual(valuation['total_value'], valuation['ledger_value'])

    def test_cancelling_an_unsold_receipt_reverses_everything(self):
        purchase = self._full_purchase()
        mark_purchase_ordered(purchase)
        receive_purchase(purchase)

        cancel_purchase(purchase, reason='Wrong goods sent')

        self.assertEqual(purchase.status, Purchase.STATUS_CANCELLED)
        self.assertEqual(self.inventory.balance(), ZERO_D)
        self.variant_a.refresh_from_db()
        self.assertEqual(self.variant_a.stock, 0)
        self.assertTrue(trial_balance()['is_balanced'])

    def test_a_purchase_with_sold_stock_cannot_be_cancelled(self):
        purchase = self._full_purchase()
        receive_purchase(purchase)
        consume_stock(variant=self.variant_a, quantity=2,
                      reason=StockMovement.REASON_SALE, reference='AC-000001')

        with self.assertRaises(LedgerError):
            cancel_purchase(purchase, reason='Too late')


# ══════════════════════════════════════════════════════════════════════════════
#  15. M6 — stock and FIFO
# ══════════════════════════════════════════════════════════════════════════════

class FifoTests(PurchaseTestCase):
    """The behaviour the owner picked: oldest shipment consumed first."""

    def setUp(self):
        super().setUp()
        # Two shipments of the same product at different costs — PUR-110 @ ৳10
        # and PUR-112 @ ৳12, as in the owner's own example.
        self.batch_old = receive_opening_stock(
            variant=self.variant_a, quantity=5, unit_cost=Decimal('10.00'),
            date=TODAY - timedelta(days=30),
        )
        self.batch_new = receive_opening_stock(
            variant=self.variant_a, quantity=20, unit_cost=Decimal('12.00'),
            date=TODAY - timedelta(days=2),
        )

    def test_a_sale_takes_from_the_oldest_batch(self):
        movement = consume_stock(
            variant=self.variant_a, quantity=1,
            reason=StockMovement.REASON_SALE, reference='AC-000001',
        )
        self.assertEqual(movement.total_cost, Decimal('10.00'))

        self.batch_old.refresh_from_db()
        self.assertEqual(self.batch_old.qty_remaining, 4)

    def test_a_sale_spanning_two_batches_blends_the_real_costs(self):
        movement = consume_stock(
            variant=self.variant_a, quantity=6,
            reason=StockMovement.REASON_SALE, reference='AC-000002',
        )
        # 5 @ 10 + 1 @ 12
        self.assertEqual(movement.total_cost, Decimal('62.00'))

        self.batch_old.refresh_from_db()
        self.batch_new.refresh_from_db()
        self.assertEqual(self.batch_old.qty_remaining, 0)
        self.assertEqual(self.batch_new.qty_remaining, 19)

    def test_consumption_rows_record_which_batch_each_unit_came_from(self):
        movement = consume_stock(
            variant=self.variant_a, quantity=6,
            reason=StockMovement.REASON_SALE, reference='AC-000003',
        )
        rows = list(movement.consumptions.order_by('id'))

        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0].quantity, rows[0].unit_cost), (5, Decimal('10.00')))
        self.assertEqual((rows[1].quantity, rows[1].unit_cost), (1, Decimal('12.00')))

    def test_a_sale_posts_cost_of_goods_sold_at_the_real_batch_cost(self):
        consume_stock(variant=self.variant_a, quantity=6,
                      reason=StockMovement.REASON_SALE, reference='AC-000004')
        self.assertEqual(self.cogs.balance(), Decimal('62.00'))
        self.assertEqual(self.inventory.balance(), Decimal('228.00'))

    def test_selling_more_than_is_in_stock_is_refused(self):
        with self.assertRaises(LedgerError):
            consume_stock(variant=self.variant_a, quantity=100,
                          reason=StockMovement.REASON_SALE)

    def test_a_shortfall_can_be_forced_for_corrections(self):
        movement = consume_stock(
            variant=self.variant_a, quantity=100,
            reason=StockMovement.REASON_SALE, allow_short=True,
        )
        # Only what existed could be costed.
        self.assertEqual(movement.total_cost, Decimal('290.00'))
        self.assertEqual(variant_stock(self.variant_a), -75)

    def test_stock_on_hand_matches_the_movement_history(self):
        consume_stock(variant=self.variant_a, quantity=6,
                      reason=StockMovement.REASON_SALE)
        self.assertEqual(variant_stock(self.variant_a), 19)
        self.variant_a.refresh_from_db()
        self.assertEqual(self.variant_a.stock, 19)

    def test_damage_posts_to_write_off_not_cost_of_sales(self):
        consume_stock(variant=self.variant_a, quantity=2,
                      reason=StockMovement.REASON_DAMAGE, note='Broken in transit')
        self.assertEqual(Account.objects.get(code='5170').balance(), Decimal('20.00'))
        self.assertEqual(self.cogs.balance(), ZERO_D)

    def test_the_cost_history_shows_each_shipment_separately(self):
        history = stock_cost_history(self.variant_a)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]['unit_cost'], Decimal('10.00'))
        self.assertEqual(history[1]['unit_cost'], Decimal('12.00'))

    def test_current_cost_is_the_average_of_what_is_left(self):
        # 5 @ 10 + 20 @ 12 = 290 / 25
        self.assertEqual(current_unit_cost(self.variant_a), Decimal('11.60'))

    def test_current_cost_follows_fifo_consumption(self):
        consume_stock(variant=self.variant_a, quantity=5,
                      reason=StockMovement.REASON_SALE)
        # Only the ৳12 batch is left.
        self.assertEqual(current_unit_cost(self.variant_a), Decimal('12.00'))

    def test_the_ledger_stays_balanced_through_sales(self):
        consume_stock(variant=self.variant_a, quantity=6,
                      reason=StockMovement.REASON_SALE)
        self.assertTrue(trial_balance()['is_balanced'])


class StockReturnTests(PurchaseTestCase):

    def setUp(self):
        super().setUp()
        receive_opening_stock(variant=self.variant_a, quantity=10,
                              unit_cost=Decimal('100.00'), date=TODAY)

    def test_a_return_goes_back_to_its_original_batch(self):
        sale = consume_stock(variant=self.variant_a, quantity=3,
                             reason=StockMovement.REASON_SALE, reference='AC-000001')
        self.assertEqual(variant_stock(self.variant_a), 7)

        return_stock(variant=self.variant_a, quantity=1, reference='AC-000001',
                     original_movement=sale)

        self.assertEqual(variant_stock(self.variant_a), 8)
        batch = StockBatch.objects.get(variant=self.variant_a)
        self.assertEqual(batch.qty_remaining, 8)

    def test_a_return_reverses_the_cost_of_sale(self):
        sale = consume_stock(variant=self.variant_a, quantity=3,
                             reason=StockMovement.REASON_SALE)
        self.assertEqual(self.cogs.balance(), Decimal('300.00'))

        return_stock(variant=self.variant_a, quantity=1, original_movement=sale)
        self.assertEqual(self.cogs.balance(), Decimal('200.00'))

    def test_a_return_with_no_known_sale_uses_the_current_cost(self):
        return_stock(variant=self.variant_a, quantity=2)
        self.assertEqual(variant_stock(self.variant_a), 12)
        self.assertEqual(self.inventory.balance(), Decimal('1200.00'))

    def test_returns_keep_the_ledger_balanced(self):
        sale = consume_stock(variant=self.variant_a, quantity=3,
                             reason=StockMovement.REASON_SALE)
        return_stock(variant=self.variant_a, quantity=1, original_movement=sale)
        self.assertTrue(trial_balance()['is_balanced'])


class StockAdjustmentTests(PurchaseTestCase):

    def setUp(self):
        super().setUp()
        receive_opening_stock(variant=self.variant_a, quantity=10,
                              unit_cost=Decimal('100.00'), date=TODAY)

    def test_adding_stock_creates_a_batch(self):
        adjust_stock(variant=self.variant_a, quantity=5,
                     reason=StockMovement.REASON_ADJUST, note='Stocktake found extra')
        self.assertEqual(variant_stock(self.variant_a), 15)
        self.assertEqual(StockBatch.objects.filter(variant=self.variant_a).count(), 2)

    def test_removing_stock_consumes_fifo(self):
        adjust_stock(variant=self.variant_a, quantity=-3,
                     reason=StockMovement.REASON_DAMAGE, note='Water damage')
        self.assertEqual(variant_stock(self.variant_a), 7)

    def test_a_zero_adjustment_is_refused(self):
        with self.assertRaises(LedgerError):
            adjust_stock(variant=self.variant_a, quantity=0,
                         reason=StockMovement.REASON_ADJUST)

    def test_adjustments_keep_the_ledger_balanced(self):
        adjust_stock(variant=self.variant_a, quantity=5,
                     reason=StockMovement.REASON_ADJUST, note='Found')
        adjust_stock(variant=self.variant_a, quantity=-2,
                     reason=StockMovement.REASON_DAMAGE, note='Broken')
        self.assertTrue(trial_balance()['is_balanced'])


class CheckoutStockIntegrationTests(PurchaseTestCase):
    """
    The storefront no longer silently clamps stock at zero — every sale becomes
    a traceable movement with its cost posted.
    """

    def setUp(self):
        super().setUp()
        receive_opening_stock(variant=self.variant_a, quantity=10,
                              unit_cost=Decimal('100.00'), date=TODAY)

    def _place_order(self, quantity):
        from store.checkout_views import _consume_stock_for_order
        from store.models import Order

        order = Order.objects.create(
            customer_name='Test Buyer', customer_phone='01700000000',
            address_line='Somewhere', city='Dhaka',
            subtotal=Decimal('1500.00'), delivery_fee=Decimal('60.00'),
            grand_total=Decimal('1560.00'),
        )
        _consume_stock_for_order(order, self.variant_a, quantity)
        return order

    def test_a_sale_reduces_stock_and_posts_its_cost(self):
        order = self._place_order(3)

        self.variant_a.refresh_from_db()
        self.assertEqual(self.variant_a.stock, 7)
        self.assertEqual(self.cogs.balance(), Decimal('300.00'))

        movement = StockMovement.objects.get(reference=order.order_number)
        self.assertEqual(movement.quantity, -3)

    def test_an_oversell_is_recorded_instead_of_vanishing(self):
        """The old code clamped at zero and left no trace."""
        self._place_order(15)

        movement = StockMovement.objects.get(reason=StockMovement.REASON_SALE)
        self.assertEqual(movement.quantity, -15)
        self.assertIn('Oversold', movement.note)
        self.assertIn('only 10', movement.note)

    def test_margin_report_pairs_cost_against_price(self):
        rows = {row['variant'].pk: row for row in margin_report()}
        row = rows[self.variant_a.pk]

        self.assertEqual(row['cost'], Decimal('100.00'))
        self.assertEqual(row['price'], Decimal('1500.00'))
        self.assertEqual(row['margin'], Decimal('1400.00'))
        self.assertEqual(row['qty_on_hand'], 10)

    def test_reconcile_command_repairs_a_drifted_figure(self):
        from django.core.management import call_command
        from io import StringIO
        from store.models import ProductVariant

        ProductVariant.objects.filter(pk=self.variant_a.pk).update(stock=999)
        call_command('reconcile_stock', '--fix', stdout=StringIO())

        self.variant_a.refresh_from_db()
        self.assertEqual(self.variant_a.stock, 10)


@override_settings(STORAGES=TEST_STORAGES)
class M5M6ViewTests(PurchaseTestCase):

    def setUp(self):
        super().setUp()
        User.objects.create_user('owner', password='pw12345!', is_staff=True)
        self.client.login(username='owner', password='pw12345!')

        self.purchase = self.make_import(
            fx='17.50', per_kg='100.00', billed_weight='10.000',
            extra='500.00', correction='5.00',
            lines=[{'variant': self.variant_a, 'qty': 10, 'rmb': '50.00',
                    'shipping_rmb': '30.00', 'weight': '10.000'}],
        )
        receive_opening_stock(variant=self.variant_b, quantity=5,
                              unit_cost=Decimal('200.00'), date=TODAY)

    def test_all_m5_and_m6_pages_load(self):
        for url in [
            '/manage/finance/purchases/',
            '/manage/finance/purchases/new/',
            f'/manage/finance/purchases/{self.purchase.pk}/',
            f'/manage/finance/purchases/{self.purchase.pk}/edit/',
            '/manage/finance/margins/',
            '/manage/finance/stock/',
            f'/manage/finance/stock/{self.variant_b.pk}/',
            '/manage/finance/stock/adjust/',
            '/manage/finance/stock/opening/',
            '/manage/finance/stock/movements/',
        ]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_receiving_through_the_view(self):
        response = self.client.post(
            f'/manage/finance/purchases/{self.purchase.pk}/receive/',
            {'received_date': TODAY.isoformat()},
        )
        self.assertEqual(response.status_code, 302)

        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.status, Purchase.STATUS_RECEIVED)
        self.variant_a.refresh_from_db()
        self.assertEqual(self.variant_a.stock, 10)

    def test_confirming_an_order_through_the_view(self):
        response = self.client.post(
            f'/manage/finance/purchases/{self.purchase.pk}/order/')
        self.assertEqual(response.status_code, 302)

        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.status, Purchase.STATUS_ORDERED)

    def test_adjusting_stock_through_the_view(self):
        response = self.client.post('/manage/finance/stock/adjust/', {
            'variant': self.variant_b.pk,
            'direction': 'remove',
            'quantity': '2',
            'unit_cost': '',
            'date': TODAY.isoformat(),
            'note': 'Broken on the shelf',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(variant_stock(self.variant_b), 3)

    def test_adding_opening_stock_through_the_view(self):
        response = self.client.post('/manage/finance/stock/opening/', {
            'variant': self.variant_a.pk,
            'quantity': '25',
            'unit_cost': '90.00',
            'date': TODAY.isoformat(),
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(variant_stock(self.variant_a), 25)

    def test_creating_a_purchase_through_the_form(self):
        response = self.client.post('/manage/finance/purchases/new/', {
            'purchase_type': Purchase.TYPE_IMPORT,
            'supplier': self.supplier.pk,
            'purchase_date': TODAY.isoformat(),
            'fx_rate_rmb_to_bdt': '17.5000',
            'default_per_kg_charge_bdt': '100.00',
            'billed_weight_kg': '0.000',
            'extra_cost_bdt': '0.00',
            'correction_percent': '5.00',
            'notes': '',
            'items-TOTAL_FORMS': '4',
            'items-INITIAL_FORMS': '0',
            'items-MIN_NUM_FORMS': '0',
            'items-MAX_NUM_FORMS': '1000',
            'items-0-variant': self.variant_a.pk,
            'items-0-quantity': '10',
            'items-0-unit_price_rmb': '50.00',
            'items-0-domestic_shipping_rmb': '30.00',
            'items-0-unit_cost_bdt': '0.00',
            'items-0-local_transport_bdt': '0.00',
            'items-0-entered_weight_kg': '0.000',
            'items-0-per_kg_charge_bdt': '',
            **{
                f'items-{index}-{field}': ''
                for index in (1, 2, 3)
                for field in ('variant', 'quantity', 'unit_price_rmb',
                              'domestic_shipping_rmb', 'unit_cost_bdt',
                              'local_transport_bdt', 'entered_weight_kg',
                              'per_kg_charge_bdt')
            },
        })
        self.assertEqual(response.status_code, 302)

        created = Purchase.objects.latest('id')
        self.assertEqual(created.items.count(), 1)
        self.assertEqual(created.goods_total_bdt, Decimal('9275.000'))


# ══════════════════════════════════════════════════════════════════════════════
#  16. M7 — investors
# ══════════════════════════════════════════════════════════════════════════════

class InvestorTestCase(TestCase):

    def setUp(self):
        from django.core.management import call_command
        from io import StringIO
        call_command('seed_chart_of_accounts', stdout=StringIO())

        self.cash = Account.objects.get(code='1010')
        self.retained = Account.objects.get(code='3100')
        self.sales = Account.objects.get(code='4010')
        self.rent = Account.objects.get(code='5110')

        self.karim = Investor.objects.create(name='Karim')
        self.rahim = Investor.objects.create(name='Rahim')


class InvestorCapitalTests(InvestorTestCase):

    def test_capital_in_grows_the_stake_and_the_cash(self):
        record_capital(investor=self.karim, date=TODAY,
                       amount=Decimal('100000.00'), account=self.cash)

        self.assertEqual(self.karim.current_stake, Decimal('100000.00'))
        self.assertEqual(self.cash.balance(), Decimal('100000.00'))

    def test_a_drawing_shrinks_the_stake_and_the_cash(self):
        record_capital(investor=self.karim, date=TODAY,
                       amount=Decimal('100000.00'), account=self.cash)
        record_drawing(investor=self.karim, date=TODAY,
                       amount=Decimal('25000.00'), account=self.cash)

        self.assertEqual(self.karim.current_stake, Decimal('75000.00'))
        self.assertEqual(self.cash.balance(), Decimal('75000.00'))

    def test_capital_in_and_drawings_are_reported_separately(self):
        record_capital(investor=self.karim, date=TODAY,
                       amount=Decimal('100000.00'), account=self.cash)
        record_drawing(investor=self.karim, date=TODAY,
                       amount=Decimal('25000.00'), account=self.cash)

        self.assertEqual(self.karim.capital_in, Decimal('100000.00'))
        self.assertEqual(self.karim.drawings, Decimal('25000.00'))

    def test_each_investor_gets_their_own_account(self):
        record_capital(investor=self.karim, date=TODAY,
                       amount=Decimal('100.00'), account=self.cash)
        record_capital(investor=self.rahim, date=TODAY,
                       amount=Decimal('100.00'), account=self.cash)

        self.karim.refresh_from_db()
        self.rahim.refresh_from_db()
        self.assertNotEqual(self.karim.equity_account_id, self.rahim.equity_account_id)

    def test_a_zero_contribution_is_refused(self):
        with self.assertRaises(LedgerError):
            record_capital(investor=self.karim, date=TODAY,
                           amount=ZERO_D, account=self.cash)

    def test_capital_movements_keep_the_ledger_balanced(self):
        record_capital(investor=self.karim, date=TODAY,
                       amount=Decimal('100000.00'), account=self.cash)
        record_drawing(investor=self.karim, date=TODAY,
                       amount=Decimal('25000.00'), account=self.cash)
        self.assertTrue(trial_balance()['is_balanced'])


class OwnershipTests(InvestorTestCase):

    def test_ownership_follows_capital_contributed(self):
        record_capital(investor=self.karim, date=TODAY,
                       amount=Decimal('75000.00'), account=self.cash)
        record_capital(investor=self.rahim, date=TODAY,
                       amount=Decimal('25000.00'), account=self.cash)

        split = dict((inv.pk, pct) for inv, pct in ownership_split())
        self.assertEqual(split[self.karim.pk], Decimal('75.000'))
        self.assertEqual(split[self.rahim.pk], Decimal('25.000'))

    def test_a_manual_percentage_overrides_the_calculation(self):
        self.karim.ownership_percent = Decimal('60.000')
        self.karim.save()
        self.rahim.ownership_percent = Decimal('40.000')
        self.rahim.save()

        record_capital(investor=self.karim, date=TODAY,
                       amount=Decimal('90000.00'), account=self.cash)

        split = dict((inv.pk, pct) for inv, pct in ownership_split())
        self.assertEqual(split[self.karim.pk], Decimal('60.000'))
        self.assertEqual(split[self.rahim.pk], Decimal('40.000'))

    def test_nobody_owns_anything_before_any_capital(self):
        self.assertEqual(
            [pct for _inv, pct in ownership_split()], [ZERO_D, ZERO_D],
        )


class ProfitDistributionTests(InvestorTestCase):

    def setUp(self):
        super().setUp()
        record_capital(investor=self.karim, date=LAST_MONTH,
                       amount=Decimal('75000.00'), account=self.cash)
        record_capital(investor=self.rahim, date=LAST_MONTH,
                       amount=Decimal('25000.00'), account=self.cash)
        # ৳50,000 income less ৳10,000 rent = ৳40,000 profit.
        post_simple(date=TODAY, description='Sales',
                    debit_account=self.cash, credit_account=self.sales,
                    amount=Decimal('50000.00'))
        post_expense(date=TODAY, expense_account=self.rent, paid_from=self.cash,
                     amount=Decimal('10000.00'), description='Rent')

    def test_profit_splits_by_ownership(self):
        distribution = distribute_profit(
            period_start=LAST_MONTH, period_end=TODAY,
        )
        shares = {s.investor_id: s.amount for s in distribution.shares.all()}

        self.assertEqual(distribution.net_profit, Decimal('40000.00'))
        self.assertEqual(shares[self.karim.pk], Decimal('30000.00'))
        self.assertEqual(shares[self.rahim.pk], Decimal('10000.00'))

    def test_distribution_moves_value_into_the_investor_accounts(self):
        distribute_profit(period_start=LAST_MONTH, period_end=TODAY)

        self.assertEqual(self.karim.current_stake, Decimal('105000.00'))
        self.assertEqual(self.rahim.current_stake, Decimal('35000.00'))

    def test_distribution_comes_out_of_retained_earnings(self):
        distribute_profit(period_start=LAST_MONTH, period_end=TODAY)
        # Debiting an equity account makes its natural balance negative.
        self.assertEqual(self.retained.balance(), Decimal('-40000.00'))

    def test_a_partial_distribution_shares_only_what_was_asked_for(self):
        distribution = distribute_profit(
            period_start=LAST_MONTH, period_end=TODAY, amount=Decimal('20000.00'),
        )
        self.assertEqual(distribution.distributed_amount, Decimal('20000.00'))
        self.assertEqual(self.karim.current_stake, Decimal('90000.00'))

    def test_a_profit_share_does_not_count_as_contributed_capital(self):
        """
        Otherwise every distribution would quietly shift the ownership split
        towards whoever already had the biggest share.
        """
        distribute_profit(period_start=LAST_MONTH, period_end=TODAY)

        self.assertEqual(self.karim.capital_in, Decimal('75000.00'))
        self.assertEqual(self.karim.profit_share_total, Decimal('30000.00'))

        split = dict((inv.pk, pct) for inv, pct in ownership_split())
        self.assertEqual(split[self.karim.pk], Decimal('75.000'))

    def test_shares_add_up_to_exactly_the_distributed_amount(self):
        distribution = distribute_profit(
            period_start=LAST_MONTH, period_end=TODAY, amount=Decimal('100.01'),
        )
        total = sum((s.amount for s in distribution.shares.all()), ZERO_D)
        self.assertEqual(total, Decimal('100.01'))

    def test_distributing_when_there_is_no_profit_is_refused(self):
        Account.objects.filter(code='4010').update(is_active=False)
        with self.assertRaises(LedgerError):
            distribute_profit(
                period_start=TODAY + timedelta(days=10),
                period_end=TODAY + timedelta(days=20),
            )

    def test_distribution_keeps_the_ledger_balanced(self):
        distribute_profit(period_start=LAST_MONTH, period_end=TODAY)
        self.assertTrue(trial_balance()['is_balanced'])

    def test_the_statement_shows_capital_drawings_and_shares(self):
        distribute_profit(period_start=LAST_MONTH, period_end=TODAY)
        record_drawing(investor=self.karim, date=TODAY,
                       amount=Decimal('5000.00'), account=self.cash)

        statement = investor_statement(self.karim)
        self.assertEqual(statement['capital_in'], Decimal('75000.00'))
        self.assertEqual(statement['drawings'], Decimal('5000.00'))
        self.assertEqual(statement['profit_share'], Decimal('30000.00'))
        self.assertEqual(statement['closing_balance'], Decimal('100000.00'))


# ══════════════════════════════════════════════════════════════════════════════
#  17. M8 — loans
# ══════════════════════════════════════════════════════════════════════════════

class LoanScheduleTests(TestCase):

    def test_flat_interest_splits_evenly(self):
        rows = build_schedule(
            principal=Decimal('120000.00'), interest_rate=Decimal('10'),
            method=Loan.METHOD_FLAT, tenure_months=12, start_date=date(2026, 1, 1),
        )
        self.assertEqual(len(rows), 12)

        principal_total = sum((row[2] for row in rows), ZERO_D)
        interest_total = sum((row[3] for row in rows), ZERO_D)

        self.assertEqual(principal_total, Decimal('120000.00'))
        self.assertEqual(interest_total, Decimal('12000.00'))
        self.assertEqual(rows[0][2], Decimal('10000.00'))
        self.assertEqual(rows[0][3], Decimal('1000.00'))

    def test_reducing_balance_shifts_from_interest_to_principal(self):
        rows = build_schedule(
            principal=Decimal('100000.00'), interest_rate=Decimal('12'),
            method=Loan.METHOD_REDUCING, tenure_months=12,
            start_date=date(2026, 1, 1),
        )
        first_interest = rows[0][3]
        last_interest = rows[-1][3]

        self.assertGreater(first_interest, last_interest)
        self.assertEqual(sum((row[2] for row in rows), ZERO_D), Decimal('100000.00'))

    def test_reducing_balance_first_month_interest_is_the_monthly_rate(self):
        rows = build_schedule(
            principal=Decimal('100000.00'), interest_rate=Decimal('12'),
            method=Loan.METHOD_REDUCING, tenure_months=12,
            start_date=date(2026, 1, 1),
        )
        # 12% a year on ৳100,000 is ৳1,000 in the first month.
        self.assertEqual(rows[0][3], Decimal('1000.00'))

    def test_an_interest_free_loan_has_no_interest(self):
        rows = build_schedule(
            principal=Decimal('60000.00'), interest_rate=Decimal('0'),
            method=Loan.METHOD_REDUCING, tenure_months=6,
            start_date=date(2026, 1, 1),
        )
        self.assertEqual(sum((row[3] for row in rows), ZERO_D), ZERO_D)
        self.assertEqual(sum((row[2] for row in rows), ZERO_D), Decimal('60000.00'))

    def test_due_dates_are_monthly(self):
        rows = build_schedule(
            principal=Decimal('1200.00'), interest_rate=Decimal('0'),
            method=Loan.METHOD_FLAT, tenure_months=3, start_date=date(2026, 1, 15),
        )
        self.assertEqual([row[1] for row in rows], [
            date(2026, 2, 15), date(2026, 3, 15), date(2026, 4, 15),
        ])

    def test_month_end_dates_clamp_to_the_shorter_month(self):
        rows = build_schedule(
            principal=Decimal('300.00'), interest_rate=Decimal('0'),
            method=Loan.METHOD_FLAT, tenure_months=2, start_date=date(2026, 1, 31),
        )
        self.assertEqual(rows[0][1], date(2026, 2, 28))

    def test_a_zero_tenure_is_refused(self):
        with self.assertRaises(LedgerError):
            build_schedule(
                principal=Decimal('1000.00'), interest_rate=Decimal('0'),
                method=Loan.METHOD_FLAT, tenure_months=0, start_date=TODAY,
            )


class LoanTests(TestCase):

    def setUp(self):
        from django.core.management import call_command
        from io import StringIO
        call_command('seed_chart_of_accounts', stdout=StringIO())

        self.cash = Account.objects.get(code='1010')
        self.interest_expense = Account.objects.get(code='5180')
        self.interest_income = Account.objects.get(code='4910')

    def make_loan(self, **kwargs):
        defaults = dict(
            direction=Loan.DIRECTION_TAKEN,
            counterparty_name='City Bank',
            principal=Decimal('120000.00'),
            interest_rate=Decimal('10'),
            method=Loan.METHOD_FLAT,
            tenure_months=12,
            start_date=TODAY,
            account=self.cash,
        )
        defaults.update(kwargs)
        return create_loan(**defaults)

    def test_taking_a_loan_brings_in_cash_and_records_the_debt(self):
        loan = self.make_loan()

        self.assertEqual(self.cash.balance(), Decimal('120000.00'))
        self.assertEqual(loan.outstanding, Decimal('120000.00'))
        self.assertEqual(loan.account.type, Account.TYPE_LOAN_PAYABLE)

    def test_giving_a_loan_takes_cash_out_and_records_the_claim(self):
        post_opening_balance(account=self.cash, date=LAST_MONTH,
                             amount=Decimal('200000.00'))
        loan = self.make_loan(direction=Loan.DIRECTION_GIVEN,
                              counterparty_name='A friend')

        self.assertEqual(self.cash.balance(), Decimal('80000.00'))
        self.assertEqual(loan.outstanding, Decimal('120000.00'))
        self.assertEqual(loan.account.type, Account.TYPE_LOAN_RECEIVABLE)

    def test_the_schedule_is_generated(self):
        loan = self.make_loan()
        self.assertEqual(loan.installments.count(), 12)
        self.assertEqual(loan.total_interest, Decimal('12000.00'))
        self.assertEqual(loan.total_repayable, Decimal('132000.00'))

    def test_a_repayment_splits_principal_from_interest(self):
        loan = self.make_loan()
        installment = loan.installments.first()

        record_loan_payment(
            loan=loan, date=TODAY,
            principal_amount=installment.principal_due,
            interest_amount=installment.interest_due,
            account=self.cash, installment=installment,
        )

        loan.refresh_from_db()
        installment.refresh_from_db()

        self.assertEqual(loan.outstanding, Decimal('110000.00'))
        self.assertEqual(self.interest_expense.balance(), Decimal('1000.00'))
        self.assertEqual(installment.status, LoanInstallment.STATUS_PAID)

    def test_a_repayment_takes_the_money_out_of_cash(self):
        loan = self.make_loan()
        installment = loan.installments.first()

        record_loan_payment(
            loan=loan, date=TODAY,
            principal_amount=installment.principal_due,
            interest_amount=installment.interest_due,
            account=self.cash, installment=installment,
        )
        # 120,000 borrowed, 11,000 repaid.
        self.assertEqual(self.cash.balance(), Decimal('109000.00'))

    def test_a_part_payment_marks_the_installment_partial(self):
        loan = self.make_loan()
        installment = loan.installments.first()

        record_loan_payment(
            loan=loan, date=TODAY, principal_amount=Decimal('5000.00'),
            interest_amount=ZERO_D, account=self.cash, installment=installment,
        )
        installment.refresh_from_db()
        self.assertEqual(installment.status, LoanInstallment.STATUS_PARTIAL)

    def test_a_loan_closes_when_every_installment_is_paid(self):
        loan = self.make_loan(tenure_months=2, principal=Decimal('2000.00'),
                              interest_rate=Decimal('0'))
        for installment in loan.installments.all():
            record_loan_payment(
                loan=loan, date=TODAY,
                principal_amount=installment.principal_due,
                interest_amount=installment.interest_due,
                account=self.cash, installment=installment,
            )
        loan.refresh_from_db()
        self.assertEqual(loan.status, Loan.STATUS_CLOSED)
        self.assertEqual(loan.outstanding, ZERO_D)

    def test_interest_on_a_loan_given_is_income_not_expense(self):
        post_opening_balance(account=self.cash, date=LAST_MONTH,
                             amount=Decimal('200000.00'))
        loan = self.make_loan(direction=Loan.DIRECTION_GIVEN,
                              counterparty_name='A friend')
        installment = loan.installments.first()

        record_loan_payment(
            loan=loan, date=TODAY,
            principal_amount=installment.principal_due,
            interest_amount=installment.interest_due,
            account=self.cash, installment=installment,
        )
        self.assertEqual(self.interest_income.balance(), Decimal('1000.00'))
        self.assertEqual(self.interest_expense.balance(), ZERO_D)

    def test_an_overdue_installment_is_flagged(self):
        loan = self.make_loan(start_date=today() - timedelta(days=120))
        self.assertTrue(loan.overdue_installments)
        self.assertIn(loan.overdue_installments[0], loan.installments.all())

    def test_loan_summary_totals_what_is_owed(self):
        self.make_loan()
        summary = loan_summary()
        self.assertEqual(summary['owed'], Decimal('120000.00'))
        self.assertEqual(summary['taken_count'], 1)

    def test_loan_numbers_run_in_sequence(self):
        first = self.make_loan()
        second = self.make_loan()
        self.assertEqual(first.loan_no, 'LOAN-0001')
        self.assertEqual(second.loan_no, 'LOAN-0002')

    def test_loans_keep_the_ledger_balanced(self):
        loan = self.make_loan()
        installment = loan.installments.first()
        record_loan_payment(
            loan=loan, date=TODAY,
            principal_amount=installment.principal_due,
            interest_amount=installment.interest_due,
            account=self.cash, installment=installment,
        )
        self.assertTrue(trial_balance()['is_balanced'])

    def test_a_zero_repayment_is_refused(self):
        loan = self.make_loan()
        with self.assertRaises(LedgerError):
            record_loan_payment(
                loan=loan, date=TODAY, principal_amount=ZERO_D,
                interest_amount=ZERO_D, account=self.cash,
            )


# ══════════════════════════════════════════════════════════════════════════════
#  18. M7 / M8 / M9 — screens and reports
# ══════════════════════════════════════════════════════════════════════════════

@override_settings(STORAGES=TEST_STORAGES)
class M7M8M9ViewTests(TestCase):

    def setUp(self):
        from django.core.management import call_command
        from io import StringIO
        call_command('seed_chart_of_accounts', stdout=StringIO())

        User.objects.create_user('owner', password='pw12345!', is_staff=True)
        self.client.login(username='owner', password='pw12345!')

        self.cash = Account.objects.get(code='1010')
        self.sales = Account.objects.get(code='4010')
        self.rent = Account.objects.get(code='5110')

        self.investor = Investor.objects.create(name='Karim')
        record_capital(investor=self.investor, date=LAST_MONTH,
                       amount=Decimal('100000.00'), account=self.cash)

        self.loan = create_loan(
            direction=Loan.DIRECTION_TAKEN, counterparty_name='City Bank',
            principal=Decimal('120000.00'), interest_rate=Decimal('10'),
            method=Loan.METHOD_FLAT, tenure_months=12, start_date=TODAY,
            account=self.cash,
        )

        post_simple(date=TODAY, description='Sales',
                    debit_account=self.cash, credit_account=self.sales,
                    amount=Decimal('50000.00'))
        post_expense(date=TODAY, expense_account=self.rent, paid_from=self.cash,
                     amount=Decimal('10000.00'), description='Rent')

    def test_all_remaining_pages_load(self):
        for url in [
            '/manage/finance/',
            '/manage/finance/investors/',
            '/manage/finance/investors/new/',
            f'/manage/finance/investors/{self.investor.pk}/',
            f'/manage/finance/investors/{self.investor.pk}/edit/',
            f'/manage/finance/investors/{self.investor.pk}/capital/',
            '/manage/finance/investors/distribute/',
            '/manage/finance/loans/',
            '/manage/finance/loans/new/',
            f'/manage/finance/loans/{self.loan.pk}/',
            '/manage/finance/reports/profit-loss/',
            '/manage/finance/reports/cash-flow/',
        ]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_every_csv_export_downloads(self):
        for report in ['profit-loss', 'daybook', 'ageing', 'stock', 'margins']:
            with self.subTest(report=report):
                response = self.client.get(f'/manage/finance/reports/export/{report}/')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response['Content-Type'], 'text/csv')
                self.assertIn('attachment', response['Content-Disposition'])

    def test_an_unknown_export_is_not_found(self):
        self.assertEqual(
            self.client.get('/manage/finance/reports/export/nonsense/').status_code,
            404,
        )

    def test_recording_capital_through_the_view(self):
        response = self.client.post(
            f'/manage/finance/investors/{self.investor.pk}/capital/',
            {'movement': 'in', 'date': TODAY.isoformat(),
             'amount': '50000.00', 'account': self.cash.pk, 'notes': ''},
        )
        self.assertEqual(response.status_code, 302)
        self.investor.refresh_from_db()
        self.assertEqual(self.investor.current_stake, Decimal('150000.00'))

    def test_creating_a_loan_through_the_view(self):
        response = self.client.post('/manage/finance/loans/new/', {
            'direction': Loan.DIRECTION_TAKEN,
            'counterparty_name': 'Uncle',
            'principal': '50000.00',
            'interest_rate': '0.000',
            'method': Loan.METHOD_FLAT,
            'tenure_months': '10',
            'start_date': TODAY.isoformat(),
            'account': self.cash.pk,
            'notes': '',
        })
        self.assertEqual(response.status_code, 302)

        created = Loan.objects.get(counterparty_name='Uncle')
        self.assertEqual(created.installments.count(), 10)

    def test_recording_a_loan_repayment_through_the_view(self):
        installment = self.loan.installments.first()
        response = self.client.post(f'/manage/finance/loans/{self.loan.pk}/pay/', {
            'date': TODAY.isoformat(),
            'principal_amount': str(installment.principal_due),
            'interest_amount': str(installment.interest_due),
            'account': self.cash.pk,
            'installment': installment.pk,
            'reference': '',
        })
        self.assertEqual(response.status_code, 302)

        installment.refresh_from_db()
        self.assertEqual(installment.status, LoanInstallment.STATUS_PAID)

    def test_distributing_profit_through_the_view(self):
        response = self.client.post('/manage/finance/investors/distribute/', {
            'period_start': LAST_MONTH.isoformat(),
            'period_end': TODAY.isoformat(),
            'distribution_date': TODAY.isoformat(),
            'amount': '',
            'notes': '',
        })
        self.assertEqual(response.status_code, 302)

        distribution = ProfitDistribution.objects.latest('id')
        self.assertEqual(distribution.distributed_amount, Decimal('40000.00'))

    def test_the_profit_and_loss_report_adds_up(self):
        response = self.client.get('/manage/finance/reports/profit-loss/', {
            'since': LAST_MONTH.isoformat(), 'as_of': TODAY.isoformat(),
        })
        result = response.context['result']
        self.assertEqual(result['income'], Decimal('50000.00'))
        self.assertEqual(result['expense'], Decimal('10000.00'))
        self.assertEqual(result['profit'], Decimal('40000.00'))

    def test_the_dashboard_reports_the_whole_position(self):
        response = self.client.get('/manage/finance/')
        self.assertEqual(response.context['cash_total'], Decimal('260000.00'))
        self.assertEqual(response.context['loan_total'], Decimal('120000.00'))
        self.assertTrue(response.context['trial']['is_balanced'])


# ══════════════════════════════════════════════════════════════════════════════
#  19. M10 — integration and hardening
# ══════════════════════════════════════════════════════════════════════════════

class AffiliateIntegrationTestCase(TestCase):
    """A real affiliate with a commission and a withdrawal to post."""

    def setUp(self):
        from django.core.management import call_command
        from io import StringIO
        from affiliate.models import AffiliateProfile, Commission, WithdrawalRequest

        call_command('seed_chart_of_accounts', stdout=StringIO())

        self.commission_expense = Account.objects.get(code='5150')
        self.commission_payable = Account.objects.get(code='2100')
        self.bkash = Account.objects.get(code='1030')

        self.user = User.objects.create_user('affiliate_user', password='pw12345!')
        self.affiliate = AffiliateProfile.objects.create(
            user=self.user, referral_code='ALPHA01',
            full_name='Test Affiliate', phone_number='01799887766',
            how_will_promote='Facebook', status='approved',
        )
        self.commission = Commission.objects.create(
            affiliate=self.affiliate, order_id=1,
            order_total=Decimal('5000.00'),
            commission_amount=Decimal('500.00'),
            status=Commission.STATUS_APPROVED,
        )
        self.withdrawal = WithdrawalRequest.objects.create(
            affiliate=self.affiliate, amount=Decimal('500.00'),
            payment_method='bkash', payment_account='01799887766',
            status=WithdrawalRequest.STATUS_PAID,
            transaction_id='TRX77001',
        )


class AffiliateHookTests(AffiliateIntegrationTestCase):

    def test_approving_a_commission_books_the_cost_and_the_debt(self):
        result = on_commission_approved(self.commission)

        self.assertTrue(result.posted)
        self.assertEqual(self.commission_expense.balance(), Decimal('500.00'))
        self.assertEqual(self.commission_payable.balance(), Decimal('500.00'))

    def test_posting_the_same_commission_twice_is_skipped(self):
        on_commission_approved(self.commission)
        again = on_commission_approved(self.commission)

        self.assertFalse(again.posted)
        self.assertIn('Already posted', again.skipped_reason)
        self.assertEqual(self.commission_expense.balance(), Decimal('500.00'))

    def test_paying_a_withdrawal_clears_the_debt_and_takes_the_money(self):
        on_commission_approved(self.commission)
        result = on_withdrawal_paid(self.withdrawal)

        self.assertTrue(result.posted)
        self.assertEqual(self.commission_payable.balance(), ZERO_D)
        self.assertEqual(self.bkash.balance(), Decimal('-500.00'))

    def test_the_payout_comes_from_the_wallet_that_was_actually_used(self):
        on_withdrawal_paid(self.withdrawal)
        self.assertEqual(self.bkash.balance(), Decimal('-500.00'))
        self.assertEqual(Account.objects.get(code='1040').balance(), ZERO_D)

    def test_a_nagad_payout_comes_out_of_nagad(self):
        self.withdrawal.payment_method = 'nagad'
        self.withdrawal.save()
        on_withdrawal_paid(self.withdrawal)

        self.assertEqual(Account.objects.get(code='1040').balance(), Decimal('-500.00'))
        self.assertEqual(self.bkash.balance(), ZERO_D)

    def test_commissions_and_withdrawals_do_not_collide(self):
        """Both use the 'affiliate' source type, so their ids must not clash."""
        self.commission.pk = self.withdrawal.pk
        on_commission_approved(self.commission)
        result = on_withdrawal_paid(self.withdrawal)

        self.assertTrue(result.posted, 'Withdrawal was wrongly treated as already posted')

    def test_a_missing_account_is_reported_not_raised(self):
        Account.objects.filter(code='5150').delete()
        result = on_commission_approved(self.commission)

        self.assertFalse(result.posted)
        self.assertTrue(result.failed)
        self.assertIn('5150', result.error)

    def test_the_switch_turns_posting_off(self):
        with override_settings(FINANCE_POST_AFFILIATE=False):
            result = on_commission_approved(self.commission)
        self.assertFalse(result.posted)
        self.assertFalse(result.failed)

    def test_affiliate_postings_keep_the_ledger_balanced(self):
        on_commission_approved(self.commission)
        on_withdrawal_paid(self.withdrawal)
        self.assertTrue(trial_balance()['is_balanced'])

    def test_commission_owed_reads_from_the_ledger(self):
        on_commission_approved(self.commission)
        self.assertEqual(affiliate_commission_owed(), Decimal('500.00'))
        on_withdrawal_paid(self.withdrawal)
        self.assertEqual(affiliate_commission_owed(), ZERO_D)


class BackfillTests(AffiliateIntegrationTestCase):

    def test_a_dry_run_writes_nothing(self):
        summary = backfill_affiliate_history(dry_run=True)

        self.assertEqual(summary['commissions_found'], 1)
        self.assertEqual(summary['withdrawals_found'], 1)
        self.assertEqual(Transaction.objects.count(), 0)

    def test_applying_posts_the_history(self):
        summary = backfill_affiliate_history(dry_run=False)

        self.assertEqual(summary['commissions_posted'], 1)
        self.assertEqual(summary['withdrawals_posted'], 1)
        self.assertEqual(self.commission_expense.balance(), Decimal('500.00'))
        self.assertTrue(trial_balance()['is_balanced'])

    def test_running_it_twice_changes_nothing(self):
        backfill_affiliate_history(dry_run=False)
        count = Transaction.objects.count()

        second = backfill_affiliate_history(dry_run=False)
        self.assertEqual(second['commissions_posted'], 0)
        self.assertEqual(Transaction.objects.count(), count)


class OrderDeliveryHookTests(InvoiceTestCase):

    def setUp(self):
        super().setUp()
        from store.models import Category, Order, OrderItem, Product, ProductVariant

        category = Category.objects.create(name='Cables')
        product = Product.objects.create(category=category, name='Cable')
        variant = ProductVariant.objects.create(
            product=product, name='Black', price=Decimal('450.00'), stock=50,
        )
        self.order = Order.objects.create(
            customer_name='Delivered Customer', customer_phone='01855000000',
            address_line='Road 1', city='Dhaka',
            subtotal=Decimal('900.00'), delivery_fee=Decimal('60.00'),
            grand_total=Decimal('960.00'),
        )
        OrderItem.objects.create(
            order=self.order, variant=variant, product_name='Cable',
            variant_name='Black', sku=variant.sku,
            unit_price=Decimal('450.00'), quantity=2,
        )

    def test_auto_invoicing_is_off_by_default(self):
        result = on_order_delivered(self.order)
        self.assertFalse(result.posted)
        self.assertEqual(self.order.invoices.count(), 0)

    @override_settings(FINANCE_AUTO_INVOICE_ON_DELIVERY=True)
    def test_turning_it_on_raises_and_issues_the_invoice(self):
        result = on_order_delivered(self.order)

        self.assertTrue(result.posted)
        invoice = self.order.invoices.first()
        self.assertEqual(invoice.status, Invoice.STATUS_SENT)
        self.assertEqual(invoice.total, Decimal('960.00'))
        self.assertTrue(invoice.number)

    @override_settings(FINANCE_AUTO_INVOICE_ON_DELIVERY=True)
    def test_it_will_not_invoice_the_same_order_twice(self):
        on_order_delivered(self.order)
        again = on_order_delivered(self.order)

        self.assertFalse(again.posted)
        self.assertEqual(self.order.invoices.count(), 1)

    @override_settings(FINANCE_AUTO_INVOICE_ON_DELIVERY=True)
    def test_auto_invoicing_keeps_the_ledger_balanced(self):
        on_order_delivered(self.order)
        self.assertTrue(trial_balance()['is_balanced'])


class FinanceAccessControlTests(TestCase):
    """The group restriction that keeps non-finance staff out."""

    def setUp(self):
        from django.contrib.auth.models import Group

        self.finance_group = Group.objects.create(name='Finance')
        self.owner = User.objects.create_user('owner', password='pw12345!', is_staff=True)
        self.packer = User.objects.create_user('packer', password='pw12345!', is_staff=True)
        self.superuser = User.objects.create_superuser(
            'boss', 'boss@example.com', 'pw12345!')
        self.owner.groups.add(self.finance_group)

    def test_any_staff_user_gets_in_when_no_group_is_set(self):
        with override_settings(FINANCE_REQUIRED_GROUP=''):
            self.assertTrue(user_can_access_finance(self.packer))

    def test_only_group_members_get_in_when_a_group_is_set(self):
        with override_settings(FINANCE_REQUIRED_GROUP='Finance'):
            self.assertTrue(user_can_access_finance(self.owner))
            self.assertFalse(user_can_access_finance(self.packer))

    def test_a_superuser_always_gets_in(self):
        with override_settings(FINANCE_REQUIRED_GROUP='Finance'):
            self.assertTrue(user_can_access_finance(self.superuser))

    def test_a_non_staff_user_never_gets_in(self):
        shopper = User.objects.create_user('shopper', password='pw12345!')
        with override_settings(FINANCE_REQUIRED_GROUP=''):
            self.assertFalse(user_can_access_finance(shopper))

    def test_an_inactive_staff_user_is_locked_out(self):
        self.packer.is_active = False
        with override_settings(FINANCE_REQUIRED_GROUP=''):
            self.assertFalse(user_can_access_finance(self.packer))

    @override_settings(FINANCE_REQUIRED_GROUP='Finance', STORAGES=TEST_STORAGES)
    def test_a_blocked_staff_user_is_refused_by_the_view(self):
        self.client.login(username='packer', password='pw12345!')
        self.assertEqual(self.client.get('/manage/finance/').status_code, 403)

    @override_settings(FINANCE_REQUIRED_GROUP='Finance', STORAGES=TEST_STORAGES)
    def test_a_group_member_reaches_the_panel(self):
        self.client.login(username='owner', password='pw12345!')
        self.assertEqual(self.client.get('/manage/finance/').status_code, 200)


@override_settings(STORAGES=TEST_STORAGES)
class M10ViewTests(AffiliateIntegrationTestCase):

    def setUp(self):
        super().setUp()
        User.objects.create_user('owner', password='pw12345!', is_staff=True)
        self.client.login(username='owner', password='pw12345!')
        on_commission_approved(self.commission)

    def test_the_m10_pages_load(self):
        for url in [
            '/manage/finance/audit/',
            '/manage/finance/audit/?only=reversals',
            '/manage/finance/audit/?source=affiliate',
            '/manage/finance/integrations/',
        ]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_the_audit_log_shows_who_posted_what(self):
        post_expense(
            date=TODAY, expense_account=Account.objects.get(code='5110'),
            paid_from=Account.objects.get(code='1010'),
            amount=Decimal('100.00'), description='Rent',
            created_by=User.objects.get(username='owner'),
        )
        response = self.client.get('/manage/finance/audit/')
        self.assertContains(response, 'owner')

    def test_the_audit_log_filters_by_user(self):
        owner = User.objects.get(username='owner')
        post_expense(
            date=TODAY, expense_account=Account.objects.get(code='5110'),
            paid_from=Account.objects.get(code='1010'),
            amount=Decimal('100.00'), description='Rent', created_by=owner,
        )
        response = self.client.get(f'/manage/finance/audit/?user={owner.pk}')
        self.assertEqual(response.context['page_obj'].paginator.count, 1)

    def test_the_backfill_preview_writes_nothing(self):
        before = Transaction.objects.count()
        response = self.client.post('/manage/finance/integrations/', {'action': 'preview'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Transaction.objects.count(), before)
        self.assertTrue(response.context['summary']['dry_run'])

    def test_the_backfill_apply_posts_the_missing_entries(self):
        response = self.client.post('/manage/finance/integrations/', {'action': 'apply'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['summary']['withdrawals_posted'], 1)
        self.assertTrue(trial_balance()['is_balanced'])


#: A key that passes the strength check, so these tests do not depend on
#: whatever happens to be in the developer's own .env.
STRONG_KEY = 'x' * 64


class DeploymentCheckTests(TestCase):

    def _run(self):
        from io import StringIO
        from django.core.management import call_command

        out = StringIO()
        try:
            call_command('check_deployment', stdout=out, stderr=out)
            return out.getvalue(), 0
        except SystemExit as exc:
            return out.getvalue(), exc.code

    def test_it_fails_when_the_chart_of_accounts_is_missing(self):
        output, code = self._run()
        self.assertEqual(code, 1)
        self.assertIn('Chart of accounts is empty', output)

    def test_it_passes_on_a_properly_set_up_install(self):
        """
        DEBUG is forced on here because the test runner turns it off, which
        makes the SQLite test database look like a misconfigured production
        server — which is precisely what the check is built to catch.
        """
        from io import StringIO
        from django.core.management import call_command

        call_command('seed_chart_of_accounts', stdout=StringIO())
        with override_settings(DEBUG=True, SECRET_KEY=STRONG_KEY):
            output, code = self._run()

        self.assertEqual(code, 0)
        self.assertIn('Ledger balances', output)

    def test_it_rejects_a_weak_secret_key(self):
        from io import StringIO
        from django.core.management import call_command

        call_command('seed_chart_of_accounts', stdout=StringIO())
        with override_settings(DEBUG=True, SECRET_KEY='django-insecure-short'):
            output, code = self._run()

        self.assertEqual(code, 1)
        self.assertIn('SECRET_KEY', output)

    def test_it_refuses_sqlite_when_debug_is_off(self):
        """The M0 bug's safety net — SQLite in production must never pass."""
        from io import StringIO
        from django.core.management import call_command

        call_command('seed_chart_of_accounts', stdout=StringIO())
        with override_settings(DEBUG=False, SECRET_KEY=STRONG_KEY):
            output, code = self._run()

        self.assertEqual(code, 1)
        self.assertIn('SQLite', output)
        self.assertIn('DB_ENGINE=mysql', output)

    def test_it_reports_stock_drift(self):
        from io import StringIO
        from django.core.management import call_command
        from store.models import Category, Product, ProductVariant

        call_command('seed_chart_of_accounts', stdout=StringIO())
        category = Category.objects.create(name='Cables')
        product = Product.objects.create(category=category, name='Cable')
        ProductVariant.objects.create(
            product=product, name='Black', price=Decimal('100.00'), stock=42,
        )
        output, _code = self._run()
        self.assertIn('disagree with their movement history', output)


# ══════════════════════════════════════════════════════════════════════════════
#  20. Catalogue — product picker and product management
# ══════════════════════════════════════════════════════════════════════════════

@override_settings(STORAGES=TEST_STORAGES)
class ProductSearchTests(TestCase):

    def setUp(self):
        from django.core.management import call_command
        from io import StringIO
        from store.models import Category, Product, ProductVariant

        call_command('seed_chart_of_accounts', stdout=StringIO())
        User.objects.create_user('owner', password='pw12345!', is_staff=True)
        self.client.login(username='owner', password='pw12345!')

        self.category = Category.objects.create(name='Cables')
        cable = Product.objects.create(category=self.category, name='USB-C Cable 2m')
        charger = Product.objects.create(category=self.category, name='Wall Charger 30W')

        self.cable_black = ProductVariant.objects.create(
            product=cable, name='Black', price=Decimal('450.00'),
            stock=25, sku='AC-CBL-BLK')
        self.cable_white = ProductVariant.objects.create(
            product=cable, name='White', price=Decimal('450.00'),
            stock=0, sku='AC-CBL-WHT')
        self.charger = ProductVariant.objects.create(
            product=charger, name='Standard', price=Decimal('1200.00'),
            stock=8, sku='AC-CHG-STD')

    def test_it_finds_products_by_name(self):
        response = self.client.get('/manage/finance/api/products/', {'q': 'cable'})
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(data['results']), 2)
        self.assertTrue(all('Cable' in r['label'] for r in data['results']))

    def test_it_finds_products_by_sku(self):
        response = self.client.get('/manage/finance/api/products/', {'q': 'AC-CHG'})
        data = response.json()

        self.assertEqual(len(data['results']), 1)
        self.assertEqual(data['results'][0]['id'], self.charger.pk)

    def test_it_finds_products_by_option_name(self):
        response = self.client.get('/manage/finance/api/products/', {'q': 'white'})
        self.assertEqual(len(response.json()['results']), 1)

    def test_each_result_carries_what_the_row_needs(self):
        response = self.client.get('/manage/finance/api/products/', {'q': 'AC-CBL-BLK'})
        result = response.json()['results'][0]

        self.assertEqual(result['label'], 'USB-C Cable 2m — Black')
        self.assertEqual(result['price'], '450.00')
        self.assertEqual(result['sku'], 'AC-CBL-BLK')
        self.assertEqual(result['stock'], 25)
        self.assertTrue(result['tracked'])

    def test_out_of_stock_products_are_still_returned(self):
        """You can invoice something you are about to restock."""
        response = self.client.get('/manage/finance/api/products/', {'q': 'white'})
        self.assertEqual(response.json()['results'][0]['stock'], 0)

    def test_an_empty_query_returns_the_catalogue(self):
        response = self.client.get('/manage/finance/api/products/')
        self.assertEqual(len(response.json()['results']), 3)

    def test_inactive_products_are_hidden(self):
        self.cable_white.is_active = False
        self.cable_white.save()
        response = self.client.get('/manage/finance/api/products/', {'q': 'cable'})
        self.assertEqual(len(response.json()['results']), 1)

    def test_a_nonsense_query_returns_nothing(self):
        response = self.client.get('/manage/finance/api/products/', {'q': 'zzzzz'})
        self.assertEqual(response.json()['results'], [])

    def test_it_is_staff_only(self):
        self.client.logout()
        User.objects.create_user('shopper', password='pw12345!')
        self.client.login(username='shopper', password='pw12345!')
        self.assertEqual(
            self.client.get('/manage/finance/api/products/').status_code, 302,
        )


@override_settings(STORAGES=TEST_STORAGES)
class InvoiceProductLinkTests(InvoiceTestCase):

    def setUp(self):
        super().setUp()
        from store.models import Category, Product, ProductVariant

        User.objects.create_user('owner', password='pw12345!', is_staff=True)
        self.client.login(username='owner', password='pw12345!')

        category = Category.objects.create(name='Cables')
        product = Product.objects.create(category=category, name='USB-C Cable 2m')
        self.variant = ProductVariant.objects.create(
            product=product, name='Black', price=Decimal('450.00'),
            stock=25, sku='AC-CBL-BLK')

    def _payload(self, **overrides):
        data = {
            'party': self.client_party.pk,
            'issue_date': TODAY.isoformat(),
            'payment_terms_days': '0',
            'discount': '0',
            'delivery_charge': '0',
            'notes': '',
            'items-TOTAL_FORMS': '4',
            'items-INITIAL_FORMS': '0',
            'items-MIN_NUM_FORMS': '0',
            'items-MAX_NUM_FORMS': '1000',
            'items-0-variant': self.variant.pk,
            'items-0-description': 'USB-C Cable 2m — Black',
            'items-0-sku': 'AC-CBL-BLK',
            'items-0-unit_price': '450.00',
            'items-0-quantity': '4',
            'items-0-discount': '0',
        }
        for index in (1, 2, 3):
            for field in ('variant', 'description', 'sku', 'unit_price',
                          'quantity', 'discount'):
                data[f'items-{index}-{field}'] = ''
        data.update(overrides)
        return data

    def test_a_picked_product_is_linked_to_the_line(self):
        response = self.client.post('/manage/finance/invoices/new/', self._payload())
        self.assertEqual(response.status_code, 302)

        item = Invoice.objects.latest('id').items.first()
        self.assertEqual(item.variant_id, self.variant.pk)
        self.assertEqual(item.unit_price, Decimal('450.00'))
        self.assertEqual(item.line_total, Decimal('1800.00'))

    def test_a_freehand_line_without_a_product_still_works(self):
        """Delivery charges and repairs were never catalogue items."""
        response = self.client.post('/manage/finance/invoices/new/', self._payload(**{
            'items-0-variant': '',
            'items-0-description': 'Cable repair labour',
            'items-0-sku': '',
            'items-0-unit_price': '500.00',
            'items-0-quantity': '1',
        }))
        self.assertEqual(response.status_code, 302)

        item = Invoice.objects.latest('id').items.first()
        self.assertIsNone(item.variant_id)
        self.assertEqual(item.description, 'Cable repair labour')

    def test_the_line_keeps_its_own_price_after_the_product_changes(self):
        self.client.post('/manage/finance/invoices/new/', self._payload())
        item = Invoice.objects.latest('id').items.first()

        self.variant.price = Decimal('999.00')
        self.variant.save()

        item.refresh_from_db()
        self.assertEqual(item.unit_price, Decimal('450.00'))

    def test_the_form_offers_the_picker(self):
        response = self.client.get('/manage/finance/invoices/new/')
        self.assertContains(response, 'product-search')
        self.assertContains(response, '/manage/finance/api/products/')

    def test_editing_a_draft_shows_the_product_already_chosen(self):
        self.client.post('/manage/finance/invoices/new/', self._payload())
        invoice = Invoice.objects.latest('id')

        response = self.client.get(f'/manage/finance/invoices/{invoice.pk}/edit/')
        self.assertContains(response, 'USB-C Cable 2m — Black')


@override_settings(STORAGES=TEST_STORAGES)
class PurchaseProductPickerTests(PurchaseTestCase):
    """The same picker, wired to purchase lines."""

    def setUp(self):
        super().setUp()
        User.objects.create_user('owner', password='pw12345!', is_staff=True)
        self.client.login(username='owner', password='pw12345!')

    def _payload(self, **overrides):
        data = {
            'purchase_type': Purchase.TYPE_IMPORT,
            'supplier': self.supplier.pk,
            'purchase_date': TODAY.isoformat(),
            'fx_rate_rmb_to_bdt': '17.5000',
            'default_per_kg_charge_bdt': '100.00',
            'billed_weight_kg': '0.000',
            'extra_cost_bdt': '0.00',
            'correction_percent': '5.00',
            'notes': '',
            'items-TOTAL_FORMS': '4',
            'items-INITIAL_FORMS': '0',
            'items-MIN_NUM_FORMS': '0',
            'items-MAX_NUM_FORMS': '1000',
            'items-0-variant': self.variant_a.pk,
            'items-0-quantity': '10',
            'items-0-unit_price_rmb': '50.00',
            'items-0-domestic_shipping_rmb': '30.00',
            'items-0-unit_cost_bdt': '0.00',
            'items-0-local_transport_bdt': '0.00',
            'items-0-entered_weight_kg': '0.000',
            'items-0-per_kg_charge_bdt': '',
        }
        for index in (1, 2, 3):
            for field in ('variant', 'quantity', 'unit_price_rmb',
                          'domestic_shipping_rmb', 'unit_cost_bdt',
                          'local_transport_bdt', 'entered_weight_kg',
                          'per_kg_charge_bdt'):
                data[f'items-{index}-{field}'] = ''
        data.update(overrides)
        return data

    def test_the_purchase_form_offers_the_picker(self):
        response = self.client.get('/manage/finance/purchases/new/')

        self.assertContains(response, 'product-search')
        self.assertContains(response, 'data-product-picker')
        self.assertContains(response, '/manage/finance/api/products/')

    def test_the_purchase_form_no_longer_lists_every_product(self):
        """
        The old widget was a <select> holding one <option> per variant, which
        does not survive a catalogue of any size.
        """
        response = self.client.get('/manage/finance/purchases/new/')
        body = response.content.decode()

        self.assertNotIn('<select name="items-0-variant"', body)
        self.assertIn('<input type="hidden" name="items-0-variant"', body)

    def test_a_picked_product_is_saved_on_the_line(self):
        response = self.client.post('/manage/finance/purchases/new/', self._payload())
        self.assertEqual(response.status_code, 302)

        item = Purchase.objects.latest('id').items.first()
        self.assertEqual(item.variant_id, self.variant_a.pk)
        self.assertEqual(item.quantity, 10)

    def test_a_row_with_no_product_is_still_refused(self):
        """Unlike an invoice, a purchase line must name a real product."""
        response = self.client.post('/manage/finance/purchases/new/', self._payload(**{
            'items-0-variant': '',
            'items-0-quantity': '10',
            'items-0-unit_price_rmb': '50.00',
        }))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Purchase.objects.count(), 0)

    def test_editing_shows_the_product_already_chosen(self):
        self.client.post('/manage/finance/purchases/new/', self._payload())
        purchase = Purchase.objects.latest('id')

        response = self.client.get(f'/manage/finance/purchases/{purchase.pk}/edit/')
        self.assertContains(response, str(self.variant_a))

    def test_the_picker_does_not_warn_about_stock_on_a_purchase(self):
        """You are buying stock in — a low-stock warning would be noise."""
        response = self.client.get('/manage/finance/purchases/new/')
        body = response.content.decode()

        table_start = body.index('data-product-picker')
        table_chunk = body[table_start:table_start + 200]
        self.assertNotIn('data-stock-warning', table_chunk)

    def test_the_invoice_picker_does_warn_about_stock(self):
        response = self.client.get('/manage/finance/invoices/new/')
        self.assertContains(response, 'data-stock-warning="1"')

    def test_a_retired_product_on_an_existing_line_still_validates(self):
        self.client.post('/manage/finance/purchases/new/', self._payload())
        purchase = Purchase.objects.latest('id')

        self.variant_a.is_active = False
        self.variant_a.save()

        response = self.client.get(f'/manage/finance/purchases/{purchase.pk}/edit/')
        self.assertEqual(response.status_code, 200)


@override_settings(STORAGES=TEST_STORAGES)
class ProductManagementTests(TestCase):

    def setUp(self):
        from django.core.management import call_command
        from io import StringIO
        from store.models import Category

        call_command('seed_chart_of_accounts', stdout=StringIO())
        User.objects.create_user('owner', password='pw12345!', is_staff=True)
        self.client.login(username='owner', password='pw12345!')
        self.category = Category.objects.create(name='Cables')

    def _payload(self, **overrides):
        data = {
            'name': 'Braided Cable 3m',
            'category': self.category.pk,
            'sku': '',
            'short_description': 'Tough braided cable',
            'is_active': 'on',
            'variants-TOTAL_FORMS': '3',
            'variants-INITIAL_FORMS': '0',
            'variants-MIN_NUM_FORMS': '0',
            'variants-MAX_NUM_FORMS': '1000',
            'variants-0-name': 'Black',
            'variants-0-sku': '',
            'variants-0-price': '650.00',
            'variants-0-compare_price': '',
            'variants-0-track_stock': 'on',
            'variants-0-is_active': 'on',
            'variants-1-name': 'Red',
            'variants-1-sku': '',
            'variants-1-price': '650.00',
            'variants-1-compare_price': '',
            'variants-1-track_stock': 'on',
            'variants-1-is_active': 'on',
            'variants-2-name': '',
            'variants-2-sku': '',
            'variants-2-price': '',
            'variants-2-compare_price': '',
        }
        data.update(overrides)
        return data

    def test_creating_a_product_with_two_options(self):
        from store.models import Product

        response = self.client.post('/manage/finance/products/new/', self._payload())
        self.assertEqual(response.status_code, 302)

        product = Product.objects.get(name='Braided Cable 3m')
        self.assertEqual(product.variants.count(), 2)
        self.assertEqual(product.category, self.category)

    def test_skus_are_generated_when_left_blank(self):
        from store.models import Product

        self.client.post('/manage/finance/products/new/', self._payload())
        product = Product.objects.get(name='Braided Cable 3m')

        self.assertTrue(product.sku)
        self.assertTrue(all(v.sku for v in product.variants.all()))

    def test_a_product_with_no_options_is_refused(self):
        from store.models import Product

        payload = self._payload(**{
            'variants-0-name': '', 'variants-0-price': '',
            'variants-1-name': '', 'variants-1-price': '',
        })
        response = self.client.post('/manage/finance/products/new/', payload)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Product.objects.filter(name='Braided Cable 3m').exists())

    def test_an_option_without_a_price_is_refused(self):
        from store.models import Product

        response = self.client.post('/manage/finance/products/new/', self._payload(**{
            'variants-0-price': '',
        }))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Product.objects.filter(name='Braided Cable 3m').exists())

    def test_a_was_price_below_the_selling_price_is_refused(self):
        response = self.client.post('/manage/finance/products/new/', self._payload(**{
            'variants-0-compare_price': '100.00',
        }))
        self.assertEqual(response.status_code, 200)

    def test_a_new_product_is_immediately_searchable(self):
        self.client.post('/manage/finance/products/new/', self._payload())
        response = self.client.get('/manage/finance/api/products/', {'q': 'Braided'})
        self.assertEqual(len(response.json()['results']), 2)

    def test_editing_a_product_adds_an_option(self):
        from store.models import Product

        self.client.post('/manage/finance/products/new/', self._payload())
        product = Product.objects.get(name='Braided Cable 3m')
        existing = list(product.variants.order_by('id'))

        payload = {
            'name': product.name,
            'category': self.category.pk,
            'sku': product.sku,
            'short_description': '',
            'is_active': 'on',
            'variants-TOTAL_FORMS': '4',
            'variants-INITIAL_FORMS': '2',
            'variants-MIN_NUM_FORMS': '0',
            'variants-MAX_NUM_FORMS': '1000',
            'variants-3-name': 'Blue',
            'variants-3-sku': '',
            'variants-3-price': '700.00',
            'variants-3-compare_price': '',
            'variants-3-track_stock': 'on',
            'variants-3-is_active': 'on',
        }
        for index, variant in enumerate(existing):
            payload.update({
                f'variants-{index}-id': variant.pk,
                f'variants-{index}-product': product.pk,
                f'variants-{index}-name': variant.name,
                f'variants-{index}-sku': variant.sku,
                f'variants-{index}-price': str(variant.price),
                f'variants-{index}-compare_price': '',
                f'variants-{index}-track_stock': 'on',
                f'variants-{index}-is_active': 'on',
            })
        payload['variants-2-name'] = ''
        payload['variants-2-sku'] = ''
        payload['variants-2-price'] = ''
        payload['variants-2-compare_price'] = ''

        response = self.client.post(
            f'/manage/finance/products/{product.pk}/edit/', payload)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(product.variants.count(), 3)

    def test_creating_a_category(self):
        from store.models import Category

        response = self.client.post('/manage/finance/products/categories/new/', {
            'name': 'Chargers', 'description': '', 'sort_order': '1', 'is_active': 'on',
        })
        self.assertRedirects(response, '/manage/finance/products/new/')
        self.assertTrue(Category.objects.filter(name='Chargers').exists())

    def test_product_creation_redirects_to_categories_when_there_are_none(self):
        from store.models import Category

        Category.objects.all().delete()
        response = self.client.get('/manage/finance/products/new/')
        self.assertRedirects(response, '/manage/finance/products/categories/new/')

    def test_the_catalogue_pages_load(self):
        self.client.post('/manage/finance/products/new/', self._payload())
        from store.models import Product
        product = Product.objects.get(name='Braided Cable 3m')

        for url in [
            '/manage/finance/products/',
            '/manage/finance/products/?q=braided',
            f'/manage/finance/products/?category={self.category.pk}',
            '/manage/finance/products/new/',
            f'/manage/finance/products/{product.pk}/edit/',
            '/manage/finance/products/categories/new/',
        ]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_a_new_product_shows_as_never_bought(self):
        self.client.post('/manage/finance/products/new/', self._payload())
        response = self.client.get('/manage/finance/products/')
        self.assertContains(response, 'never bought')


# ══════════════════════════════════════════════════════════════════════════════
#  21. Bug-report fixes — each test fails against the code before the fix
# ══════════════════════════════════════════════════════════════════════════════

class CheckoutResilienceTests(PurchaseTestCase):
    """
    #1 — a missing chart of accounts used to take the whole storefront down.

    `_place_order` runs inside one atomic block, so a LedgerError raised while
    posting cost of sales rolled the customer's order back and returned a 500.
    """

    def setUp(self):
        super().setUp()
        receive_opening_stock(variant=self.variant_a, quantity=10,
                              unit_cost=Decimal('100.00'), date=TODAY)

    def _order(self):
        from store.models import Order
        return Order.objects.create(
            customer_name='Buyer', customer_phone='01700000000',
            address_line='x', city='Dhaka', subtotal=Decimal('1'),
            delivery_fee=Decimal('0'), grand_total=Decimal('1'),
        )

    def test_checkout_survives_a_missing_cogs_account(self):
        from store.checkout_views import _consume_stock_for_order

        Account.objects.filter(code='5010').delete()
        order = self._order()

        _consume_stock_for_order(order, self.variant_a, 3)   # must not raise

        self.variant_a.refresh_from_db()
        self.assertEqual(self.variant_a.stock, 7)

    def test_the_unposted_cost_is_flagged_on_the_movement(self):
        from store.checkout_views import _consume_stock_for_order

        Account.objects.filter(code='5010').delete()
        _consume_stock_for_order(self._order(), self.variant_a, 3)

        movement = StockMovement.objects.get(reason=StockMovement.REASON_SALE)
        self.assertIsNone(movement.transaction_id)
        self.assertIn('not posted', movement.note)
        self.assertIn('300.00', movement.note)

    def test_the_ledger_still_balances_when_a_cost_could_not_post(self):
        from store.checkout_views import _consume_stock_for_order

        Account.objects.filter(code='5010').delete()
        _consume_stock_for_order(self._order(), self.variant_a, 3)
        self.assertTrue(trial_balance()['is_balanced'])

    def test_a_normal_checkout_still_posts_its_cost(self):
        from store.checkout_views import _consume_stock_for_order

        _consume_stock_for_order(self._order(), self.variant_a, 3)
        self.assertEqual(self.cogs.balance(), Decimal('300.00'))

    def test_direct_calls_still_fail_loudly(self):
        """Only the storefront tolerates a missing account."""
        Account.objects.filter(code='5010').delete()
        with self.assertRaises(LedgerError):
            consume_stock(variant=self.variant_a, quantity=1,
                          reason=StockMovement.REASON_SALE)


class ManagedReversalTests(InvoiceTestCase):
    """#2 — reversing a document's entry from the ledger desynchronised it."""

    def test_an_invoice_posting_cannot_be_reversed_from_the_ledger(self):
        invoice = issue_invoice(self.make_invoice())

        with self.assertRaises(LedgerError) as caught:
            reverse_transaction(invoice.transaction, reason='oops')

        self.assertIn('Cancel the invoice', str(caught.exception))
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, Invoice.STATUS_SENT)
        self.assertEqual(self.client_party.outstanding, invoice.total)

    def test_cancelling_the_invoice_still_works(self):
        invoice = issue_invoice(self.make_invoice())
        cancel_invoice(invoice, reason='Client changed their mind')

        self.assertEqual(invoice.status, Invoice.STATUS_CANCELLED)
        self.assertEqual(self.client_party.outstanding, ZERO_D)

    def test_a_payment_posting_cannot_be_reversed_from_the_ledger(self):
        issue_invoice(self.make_invoice())
        payment = record_payment(party=self.client_party, date=TODAY,
                                 amount=Decimal('500.00'), account=self.cash)

        with self.assertRaises(LedgerError):
            reverse_transaction(payment.transaction, reason='oops')

    def test_a_manual_expense_can_still_be_reversed(self):
        txn = post_expense(
            date=TODAY, expense_account=Account.objects.get(code='5110'),
            paid_from=self.cash, amount=Decimal('100.00'), description='Rent',
        )
        reverse_transaction(txn, reason='Entered twice')
        txn.refresh_from_db()
        self.assertTrue(txn.is_reversed)

    @override_settings(STORAGES=TEST_STORAGES)
    def test_the_view_refuses_and_explains(self):
        User.objects.create_user('owner', password='pw12345!', is_staff=True)
        self.client.login(username='owner', password='pw12345!')
        invoice = issue_invoice(self.make_invoice())

        response = self.client.post(
            f'/manage/finance/transactions/{invoice.transaction.pk}/reverse/',
            {'reason': 'oops'}, follow=True,
        )
        self.assertEqual(response.status_code, 200)
        invoice.transaction.refresh_from_db()
        self.assertFalse(invoice.transaction.is_reversed)

    @override_settings(STORAGES=TEST_STORAGES)
    def test_the_page_hides_the_form_and_points_at_the_document(self):
        User.objects.create_user('owner', password='pw12345!', is_staff=True)
        self.client.login(username='owner', password='pw12345!')
        invoice = issue_invoice(self.make_invoice())

        response = self.client.get(
            f'/manage/finance/transactions/{invoice.transaction.pk}/')
        self.assertContains(response, 'Cancel the invoice')
        self.assertNotContains(response, 'Reverse this entry')


class NumberAllocationTests(InvoiceTestCase):
    """#3 — concurrent issuing collided on the unique number."""

    def test_a_taken_number_is_skipped_rather_than_crashing(self):
        from finance.services import allocate_invoice_number

        first = issue_invoice(self.make_invoice())
        self.assertEqual(first.number, 'INV-2026-0001')

        # Simulate the racer that already grabbed the next number.
        other = self.make_invoice()
        Invoice.objects.filter(pk=other.pk).update(number='INV-2026-0002')

        third = self.make_invoice()
        number = allocate_invoice_number(third, year=2026)
        self.assertEqual(number, 'INV-2026-0003')

    def test_issuing_after_a_manual_number_still_works(self):
        taken = self.make_invoice()
        Invoice.objects.filter(pk=taken.pk).update(number='INV-2026-0001')

        issued = issue_invoice(self.make_invoice())
        self.assertEqual(issued.number, 'INV-2026-0002')

    def test_purchase_numbers_survive_a_collision(self):
        first = self.make_purchase_stub()
        Purchase.objects.filter(pk=first.pk).update(purchase_no='PUR-0002')

        second = self.make_purchase_stub()
        self.assertTrue(second.purchase_no.startswith('PUR-'))
        self.assertEqual(Purchase.objects.filter(purchase_no=second.purchase_no).count(), 1)

    def make_purchase_stub(self):
        supplier = Party.objects.filter(party_type=Party.TYPE_SUPPLIER).first()
        if supplier is None:
            supplier = Party.objects.create(
                name='A Supplier', party_type=Party.TYPE_SUPPLIER)
        return Purchase.objects.create(
            supplier=supplier, purchase_date=TODAY,
            fx_rate_rmb_to_bdt=Decimal('17.50'),
        )


class StockLockOrderingTests(PurchaseTestCase):
    """#4 — availability was read before the batches were locked."""

    def setUp(self):
        super().setUp()
        receive_opening_stock(variant=self.variant_a, quantity=5,
                              unit_cost=Decimal('100.00'), date=TODAY)

    def test_availability_comes_from_the_locked_batches(self):
        with self.assertRaises(LedgerError) as caught:
            consume_stock(variant=self.variant_a, quantity=6,
                          reason=StockMovement.REASON_SALE)
        self.assertIn('Only 5', str(caught.exception))

    def test_stock_with_no_batches_cannot_be_consumed_unforced(self):
        """
        The catalogue figure can be non-zero with no batches behind it — stock
        set by hand before the ledger existed. There is nothing to cost, so a
        plain sale is refused rather than inventing a cost.
        """
        from store.models import ProductVariant
        ProductVariant.objects.filter(pk=self.variant_b.pk).update(stock=99)
        self.variant_b.refresh_from_db()

        with self.assertRaises(LedgerError):
            consume_stock(variant=self.variant_b, quantity=1,
                          reason=StockMovement.REASON_SALE)

    def test_forcing_still_works_for_corrections(self):
        movement = consume_stock(variant=self.variant_a, quantity=8,
                                 reason=StockMovement.REASON_SALE, allow_short=True)
        self.assertEqual(movement.total_cost, Decimal('500.00'))


class ReturnCapTests(PurchaseTestCase):
    """#6 — the same sale could be returned over and over."""

    def setUp(self):
        super().setUp()
        receive_opening_stock(variant=self.variant_a, quantity=10,
                              unit_cost=Decimal('100.00'), date=TODAY)
        self.sale = consume_stock(variant=self.variant_a, quantity=3,
                                  reason=StockMovement.REASON_SALE, reference='AC-1')

    def test_returning_more_than_was_sold_is_refused(self):
        with self.assertRaises(LedgerError):
            return_stock(variant=self.variant_a, quantity=4,
                         original_movement=self.sale)

    def test_the_same_sale_cannot_be_returned_twice(self):
        return_stock(variant=self.variant_a, quantity=3, original_movement=self.sale)
        self.assertEqual(variant_stock(self.variant_a), 10)

        with self.assertRaises(LedgerError):
            return_stock(variant=self.variant_a, quantity=1,
                         original_movement=self.sale)
        self.assertEqual(variant_stock(self.variant_a), 10)

    def test_partial_returns_add_up_to_the_original(self):
        return_stock(variant=self.variant_a, quantity=1, original_movement=self.sale)
        return_stock(variant=self.variant_a, quantity=2, original_movement=self.sale)
        self.assertEqual(variant_stock(self.variant_a), 10)

        with self.assertRaises(LedgerError):
            return_stock(variant=self.variant_a, quantity=1,
                         original_movement=self.sale)

    def test_returns_keep_the_ledger_balanced(self):
        return_stock(variant=self.variant_a, quantity=2, original_movement=self.sale)
        self.assertTrue(trial_balance()['is_balanced'])


class LoanGuardTests(TestCase):
    """#7 — overpayment was allowed, and some loans never closed."""

    def setUp(self):
        from django.core.management import call_command
        from io import StringIO
        call_command('seed_chart_of_accounts', stdout=StringIO())
        self.cash = Account.objects.get(code='1010')
        self.loan = create_loan(
            direction=Loan.DIRECTION_TAKEN, counterparty_name='Uncle',
            principal=Decimal('10000.00'), interest_rate=Decimal('0'),
            method=Loan.METHOD_FLAT, tenure_months=10, start_date=TODAY,
            account=self.cash,
        )

    def test_paying_more_principal_than_is_owed_is_refused(self):
        with self.assertRaises(LedgerError) as caught:
            record_loan_payment(
                loan=self.loan, date=TODAY,
                principal_amount=Decimal('15000.00'), interest_amount=ZERO_D,
                account=self.cash,
            )
        self.assertIn('more principal', str(caught.exception))

    def test_the_loan_closes_once_the_principal_is_cleared(self):
        record_loan_payment(
            loan=self.loan, date=TODAY,
            principal_amount=Decimal('10000.00'), interest_amount=ZERO_D,
            account=self.cash,
        )
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, Loan.STATUS_CLOSED)
        self.assertEqual(self.loan.outstanding, ZERO_D)

    def test_a_partly_repaid_loan_stays_active(self):
        record_loan_payment(
            loan=self.loan, date=TODAY,
            principal_amount=Decimal('1000.00'), interest_amount=ZERO_D,
            account=self.cash,
        )
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, Loan.STATUS_ACTIVE)


class QueryCountTests(InvoiceTestCase):
    """#5 — the list views ran one query per row."""

    def setUp(self):
        super().setUp()
        for index in range(6):
            issue_invoice(self.make_invoice(lines=[('Item', '100.00', 1)]))
        for index in range(4):
            Party.objects.create(name=f'Client {index}')

    def _count_invoice_queries(self):
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        from finance.services import with_payment_totals

        with CaptureQueriesContext(connection) as captured:
            for invoice in with_payment_totals(
                Invoice.objects.all()
            ).prefetch_related('items'):
                invoice.amount_due          # the column that used to cost a query
        return len(captured)

    def test_invoice_totals_do_not_scale_with_row_count(self):
        """
        The count must be the same for 6 invoices as for 26 — that is what
        distinguishes a fixed number of queries from one per row.
        """
        before = self._count_invoice_queries()

        for _ in range(20):
            issue_invoice(self.make_invoice(lines=[('Item', '100.00', 1)]))

        after = self._count_invoice_queries()
        self.assertEqual(before, after)
        self.assertLessEqual(after, 3)

    def test_party_balances_do_not_scale_with_row_count(self):
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        from finance.services import with_outstanding

        def count():
            with CaptureQueriesContext(connection) as captured:
                for party in with_outstanding(Party.objects.all()):
                    party.outstanding
            return len(captured)

        before = count()
        for index in range(20):
            Party.objects.create(name=f'Extra Client {index}')
        after = count()

        self.assertEqual(before, after)
        self.assertLessEqual(after, 2)

    def test_the_annotated_figure_matches_the_slow_path(self):
        from finance.services import with_outstanding, with_payment_totals

        annotated = {p.pk: p.outstanding for p in with_outstanding(Party.objects.all())}
        for party in Party.objects.all():
            self.assertEqual(annotated[party.pk], party.outstanding)

        paid = {i.pk: i.amount_paid for i in with_payment_totals(Invoice.objects.all())}
        for invoice in Invoice.objects.all():
            self.assertEqual(paid[invoice.pk], invoice.amount_paid)


class TypeBalanceGuardTests(TestCase):
    """Mixed-direction totals used to return a plausible but wrong number."""

    def setUp(self):
        from django.core.management import call_command
        from io import StringIO
        call_command('seed_chart_of_accounts', stdout=StringIO())

    def test_mixing_income_and_expense_is_refused(self):
        with self.assertRaises(LedgerError):
            type_balance([Account.TYPE_INCOME, Account.TYPE_EXPENSE])

    def test_types_sharing_a_direction_still_work(self):
        self.assertEqual(type_balance(Account.MONEY_TYPES), ZERO_D)


@override_settings(STORAGES=TEST_STORAGES)
class ViewHardeningTests(InvoiceTestCase):
    """#8 and #9, plus share-link revocation."""

    def setUp(self):
        super().setUp()
        User.objects.create_user('owner', password='pw12345!', is_staff=True)
        self.client.login(username='owner', password='pw12345!')

    def test_a_malformed_date_does_not_crash_the_trial_balance(self):
        response = self.client.get('/manage/finance/trial-balance/?as_of=garbage')
        self.assertEqual(response.status_code, 200)

    def test_a_malformed_date_does_not_crash_the_ageing_report(self):
        response = self.client.get('/manage/finance/dues/ageing/?as_of=13-45-9999')
        self.assertEqual(response.status_code, 200)

    def test_a_malformed_date_does_not_crash_the_reports(self):
        for url in ['/manage/finance/reports/profit-loss/?since=nope',
                    '/manage/finance/reports/cash-flow/?as_of=nope']:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_creating_an_invoice_from_an_order_needs_a_post(self):
        from store.models import Category, Order, OrderItem, Product, ProductVariant

        category = Category.objects.create(name='Cables')
        product = Product.objects.create(category=category, name='Cable')
        variant = ProductVariant.objects.create(
            product=product, name='Black', price=Decimal('100.00'), stock=5)
        order = Order.objects.create(
            customer_name='Someone', customer_phone='0170', address_line='x',
            city='Dhaka', subtotal=Decimal('100'), delivery_fee=Decimal('0'),
            grand_total=Decimal('100'))
        OrderItem.objects.create(
            order=order, variant=variant, product_name='Cable',
            variant_name='Black', sku=variant.sku,
            unit_price=Decimal('100.00'), quantity=1)

        # A GET — what a crawler or link prefetch would do — must create nothing.
        self.client.get(f'/manage/finance/invoices/from-order/{order.order_number}/')
        self.assertEqual(order.invoices.count(), 0)

        self.client.post(f'/manage/finance/invoices/from-order/{order.order_number}/')
        self.assertEqual(order.invoices.count(), 1)

    def test_a_share_link_can_be_replaced(self):
        invoice = issue_invoice(self.make_invoice())
        original = invoice.share_token

        self.client.post(f'/manage/finance/invoices/{invoice.pk}/share/',
                         {'action': 'regenerate'})
        invoice.refresh_from_db()

        self.assertNotEqual(invoice.share_token, original)
        self.assertEqual(self.client.get(f'/invoice/{original}/').status_code, 404)
        self.assertEqual(
            self.client.get(f'/invoice/{invoice.share_token}/').status_code, 200)

    def test_sharing_can_be_switched_off(self):
        invoice = issue_invoice(self.make_invoice())
        original = invoice.share_token

        self.client.post(f'/manage/finance/invoices/{invoice.pk}/share/',
                         {'action': 'revoke'})
        invoice.refresh_from_db()

        self.assertIsNone(invoice.share_token)
        self.assertEqual(self.client.get(f'/invoice/{original}/').status_code, 404)

    def test_a_client_list_page_loads_with_pagination(self):
        for index in range(3):
            Party.objects.create(name=f'Bulk Client {index}')
        response = self.client.get('/manage/finance/parties/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('page_obj', response.context)


class SeedCommandTests(TestCase):

    def test_seed_creates_accounts_and_is_repeatable(self):
        from django.core.management import call_command
        from io import StringIO

        call_command('seed_chart_of_accounts', stdout=StringIO())
        first_count = Account.objects.count()
        self.assertGreater(first_count, 20)

        call_command('seed_chart_of_accounts', stdout=StringIO())
        self.assertEqual(Account.objects.count(), first_count)

    def test_seeded_ledger_starts_balanced(self):
        from django.core.management import call_command
        from io import StringIO

        call_command('seed_chart_of_accounts', stdout=StringIO())
        self.assertTrue(trial_balance()['is_balanced'])
