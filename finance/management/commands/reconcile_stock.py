"""
Rebuild the cached ProductVariant.stock figures from the movement history.

    python manage.py reconcile_stock            # report differences only
    python manage.py reconcile_stock --fix      # write the corrected figures

The movement ledger is the truth; `ProductVariant.stock` is a denormalised copy
so the storefront does not have to sum it on every page view. This command
proves the two agree, and repairs them when they do not.
"""

from django.core.management.base import BaseCommand

from finance.services import sync_variant_stock, variant_stock
from store.models import ProductVariant


class Command(BaseCommand):
    help = 'Check ProductVariant.stock against the stock movement history.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--fix', action='store_true',
            help='Write the corrected figures instead of only reporting them.',
        )

    def handle(self, *args, **options):
        fix = options['fix']
        drifted = []

        for variant in ProductVariant.objects.select_related('product'):
            derived = max(0, variant_stock(variant))
            if derived != variant.stock:
                drifted.append((variant, variant.stock, derived))

        if not drifted:
            self.stdout.write(self.style.SUCCESS(
                'Every product matches its movement history.'
            ))
            return

        self.stdout.write(self.style.WARNING(
            f'{len(drifted)} product(s) disagree with the movement history:'
        ))
        for variant, cached, derived in drifted:
            self.stdout.write(
                f'  {variant}  cached={cached}  from movements={derived}'
            )
            if fix:
                sync_variant_stock(variant)

        self.stdout.write('')
        if fix:
            self.stdout.write(self.style.SUCCESS(f'Corrected {len(drifted)} product(s).'))
        else:
            self.stdout.write('Run again with --fix to correct them.')
