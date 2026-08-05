"""
Pre-flight check before trusting this system with real money.

    python manage.py check_deployment

Verifies the things that are easy to get wrong on a shared host and expensive
to discover later — running on the wrong database, DEBUG left on, a missing
chart of accounts, a ledger that does not balance, stock figures that have
drifted from their movement history.

Exits non-zero if anything is wrong, so it can gate a deploy script.
"""

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection

from finance.models import Account, Transaction
from finance.services import trial_balance, variant_stock
from store.models import ProductVariant

OK = 'ok'
WARN = 'warn'
FAIL = 'fail'


class Command(BaseCommand):
    help = 'Check this installation is fit to record real money.'

    def handle(self, *args, **options):
        checks = [
            self._check_database(),
            self._check_debug(),
            self._check_secret_key(),
            self._check_chart_of_accounts(),
            self._check_trial_balance(),
            self._check_stock_sync(),
            self._check_allowed_hosts(),
            self._check_https(),
        ]

        self.stdout.write('')
        for status, label, detail in checks:
            if status == OK:
                marker = self.style.SUCCESS('  OK  ')
            elif status == WARN:
                marker = self.style.WARNING(' WARN ')
            else:
                marker = self.style.ERROR(' FAIL ')
            self.stdout.write(f'{marker} {label}')
            if detail:
                self.stdout.write(f'        {detail}')

        failures = [c for c in checks if c[0] == FAIL]
        warnings = [c for c in checks if c[0] == WARN]

        self.stdout.write('')
        if failures:
            self.stdout.write(self.style.ERROR(
                f'{len(failures)} problem(s) must be fixed before going live.'
            ))
            raise SystemExit(1)
        if warnings:
            self.stdout.write(self.style.WARNING(
                f'Ready, with {len(warnings)} thing(s) worth reviewing.'
            ))
        else:
            self.stdout.write(self.style.SUCCESS('All checks passed.'))

    # ── Individual checks ─────────────────────────────────────────────────

    def _check_database(self):
        engine = connection.settings_dict['ENGINE']
        name = connection.settings_dict['NAME']

        if 'sqlite' in engine:
            if settings.DEBUG:
                return (OK, 'Database: SQLite (development)', str(name))
            return (
                FAIL,
                'Database: running on SQLite with DEBUG off',
                'Production must use MySQL. Set DB_ENGINE=mysql in .env — '
                'anything recorded in db.sqlite3 on the server will not be in MySQL.',
            )
        return (OK, f'Database: MySQL ({name})', '')

    def _check_debug(self):
        if settings.DEBUG:
            return (
                WARN, 'DEBUG is on',
                'Fine locally. On the live site this leaks stack traces and settings.',
            )
        return (OK, 'DEBUG is off', '')

    def _check_secret_key(self):
        key = settings.SECRET_KEY or ''
        if len(key) < 40 or key.startswith('django-insecure'):
            return (
                FAIL, 'SECRET_KEY is weak or still the development default',
                'Generate a fresh one and put it in .env.',
            )
        return (OK, 'SECRET_KEY looks fine', '')

    def _check_chart_of_accounts(self):
        count = Account.objects.count()
        if count == 0:
            return (
                FAIL, 'Chart of accounts is empty',
                'Run: python manage.py seed_chart_of_accounts',
            )

        required = ['1010', '1300', '2010', '2150', '3900', '4010', '5010', '5150']
        missing = [
            code for code in required
            if not Account.objects.filter(code=code).exists()
        ]
        if missing:
            return (
                FAIL, f'Chart of accounts is missing {len(missing)} standard account(s)',
                f'Missing: {", ".join(missing)}. Run seed_chart_of_accounts again.',
            )
        return (OK, f'Chart of accounts complete ({count} accounts)', '')

    def _check_trial_balance(self):
        result = trial_balance()
        if not result['is_balanced']:
            return (
                FAIL,
                f'Ledger is out of balance by {result["total_signed"]}',
                'Something wrote to TransactionLine without going through '
                'post_transaction(). Investigate before trusting any figure.',
            )
        return (
            OK,
            f'Ledger balances ({Transaction.objects.count()} entries)',
            '',
        )

    def _check_stock_sync(self):
        drifted = []
        for variant in ProductVariant.objects.all():
            if max(0, variant_stock(variant)) != variant.stock:
                drifted.append(str(variant))

        if drifted:
            shown = ', '.join(drifted[:5])
            more = f' and {len(drifted) - 5} more' if len(drifted) > 5 else ''
            return (
                WARN,
                f'{len(drifted)} product(s) disagree with their movement history',
                f'{shown}{more}. Run: python manage.py reconcile_stock --fix',
            )
        return (OK, 'Stock figures match their movement history', '')

    def _check_allowed_hosts(self):
        hosts = [h for h in settings.ALLOWED_HOSTS if h not in ('localhost', '127.0.0.1')]
        if not settings.DEBUG and not hosts:
            return (
                FAIL, 'ALLOWED_HOSTS has no real domain',
                'Set ALLOWED_HOSTS in .env to your live domain.',
            )
        return (OK, f'ALLOWED_HOSTS: {", ".join(settings.ALLOWED_HOSTS) or "—"}', '')

    def _check_https(self):
        if settings.DEBUG:
            return (OK, 'HTTPS settings skipped (development)', '')
        if not getattr(settings, 'SESSION_COOKIE_SECURE', False):
            return (
                WARN, 'Session cookies are not marked secure',
                'They are set automatically when DEBUG is off — check settings.py.',
            )
        return (OK, 'Secure cookies enabled', '')
