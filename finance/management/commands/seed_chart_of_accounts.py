"""
Create the default chart of accounts for AlphaCue Imports.

    python manage.py seed_chart_of_accounts

Safe to run more than once — existing accounts are left untouched and only
missing ones are created. Codes are grouped by kind:

    1000s  things the business owns      (cash, stock, money owed to us)
    2000s  things the business owes      (suppliers, loans, commissions)
    3000s  owner and investor capital
    4000s  income
    5000s  expenses
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from finance.models import Account

A = Account

DEFAULT_ACCOUNTS = [
    # code,  name,                            type,                  description
    ('1010', 'Cash in Hand',                  A.TYPE_CASH,             'Physical cash kept at the shop or office'),
    ('1020', 'Bank Account',                  A.TYPE_BANK,             'Main business bank account'),
    ('1030', 'bKash',                         A.TYPE_MOBILE_MONEY,     'bKash merchant / personal wallet'),
    ('1040', 'Nagad',                         A.TYPE_MOBILE_MONEY,     'Nagad wallet'),
    ('1200', 'Money Owed By Customers',       A.TYPE_RECEIVABLE,       'Total unpaid customer invoices (per-client accounts sit under this)'),
    ('1300', 'Stock on Hand',                 A.TYPE_INVENTORY,        'Value of goods currently in stock, at landed cost'),
    ('1350', 'Goods in Transit',              A.TYPE_GOODS_IN_TRANSIT, 'Paid for, shipped, not yet received'),
    ('1400', 'Loans Given Out',               A.TYPE_LOAN_RECEIVABLE,  'Money lent to others, still to come back'),

    ('2010', 'Money Owed To Suppliers',       A.TYPE_PAYABLE,          'Unpaid supplier bills'),
    ('2100', 'Affiliate Commission Payable',  A.TYPE_PAYABLE,          'Commission earned by affiliates, not yet paid out'),
    ('2150', 'Accrued Landed Costs',          A.TYPE_PAYABLE,          'The correction buffer on purchases — expected but unbilled costs'),
    ('2200', 'Loans Taken',                   A.TYPE_LOAN_PAYABLE,     'Money borrowed, still to be repaid'),

    ('3010', "Owner's Capital",               A.TYPE_EQUITY,           'Money the owner has put into the business'),
    ('3020', "Owner's Drawings",              A.TYPE_EQUITY,           'Money the owner has taken out for personal use'),
    ('3100', 'Retained Earnings',             A.TYPE_EQUITY,           'Profit kept in the business from earlier periods'),
    ('3900', 'Opening Balances',              A.TYPE_EQUITY,           'Counterpart used when entering starting balances'),

    ('4010', 'Product Sales',                 A.TYPE_INCOME,           'Revenue from goods sold'),
    ('4020', 'Delivery Charges Collected',    A.TYPE_INCOME,           'Delivery fees charged to customers'),
    ('4900', 'Other Income',                  A.TYPE_INCOME,           'Anything earned outside normal sales'),
    ('4910', 'Interest Income',               A.TYPE_INCOME,           'Interest earned on money you lent out'),

    ('5010', 'Cost of Goods Sold',            A.TYPE_EXPENSE,          'Landed cost of the stock actually sold'),
    ('5020', 'Shipping & Freight',            A.TYPE_EXPENSE,          'Courier and freight paid out'),
    ('5030', 'Customs & Clearing',            A.TYPE_EXPENSE,          'Duty, clearing and agent charges'),
    ('5100', 'Salaries & Wages',              A.TYPE_EXPENSE,          'Staff pay'),
    ('5110', 'Rent',                          A.TYPE_EXPENSE,          'Shop, office or warehouse rent'),
    ('5120', 'Utilities',                     A.TYPE_EXPENSE,          'Electricity, internet, water, gas'),
    ('5130', 'Packaging',                     A.TYPE_EXPENSE,          'Boxes, tape, polybags, labels'),
    ('5140', 'Marketing & Advertising',       A.TYPE_EXPENSE,          'Facebook ads, boosting, printing'),
    ('5150', 'Affiliate Commission',          A.TYPE_EXPENSE,          'Commission cost earned by affiliates'),
    ('5160', 'Bank & Payment Charges',        A.TYPE_EXPENSE,          'bKash/Nagad cash-out fees, bank charges'),
    ('5170', 'Stock Write-off & Damage',      A.TYPE_EXPENSE,          'Goods lost, broken or written off'),
    ('5180', 'Interest Expense',              A.TYPE_EXPENSE,          'Interest paid on money you borrowed'),
    ('5900', 'Miscellaneous Expense',         A.TYPE_EXPENSE,          'Anything that fits nowhere else'),
]


class Command(BaseCommand):
    help = 'Create the default chart of accounts. Safe to run repeatedly.'

    @transaction.atomic
    def handle(self, *args, **options):
        created_count = 0
        existing_count = 0

        for code, name, acc_type, description in DEFAULT_ACCOUNTS:
            _account, created = Account.objects.get_or_create(
                code=code,
                defaults={
                    'name': name,
                    'type': acc_type,
                    'description': description,
                    'is_system': True,
                },
            )
            if created:
                created_count += 1
                self.stdout.write(self.style.SUCCESS(f'  + {code}  {name}'))
            else:
                existing_count += 1

        self.stdout.write('')
        if created_count:
            self.stdout.write(self.style.SUCCESS(
                f'Created {created_count} account(s).'
            ))
        if existing_count:
            self.stdout.write(
                f'Left {existing_count} existing account(s) untouched.'
            )
        if not created_count:
            self.stdout.write(self.style.SUCCESS('Chart of accounts already complete.'))
