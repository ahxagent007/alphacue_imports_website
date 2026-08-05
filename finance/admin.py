"""
finance/admin.py
────────────────
Django admin registration for inspection only.

The ledger is append-only, so the admin is deliberately read-only for
transactions and their lines — the immutability guards in models.py would
reject an edit anyway, and a form that always errors is worse than no form.
Accounts remain editable because renaming or deactivating one is a legitimate
day-to-day action that changes no history.
"""

from django.contrib import admin
from django.utils.html import format_html

from .models import (
    Account, Invoice, InvoiceItem, Party, Transaction, TransactionLine,
)


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'type', 'balance_display', 'is_active', 'is_system')
    list_filter = ('type', 'is_active', 'is_system', 'party_type')
    search_fields = ('code', 'name', 'description')
    ordering = ('code',)
    readonly_fields = ('created_at', 'balance_display')
    fieldsets = (
        (None, {
            'fields': ('code', 'name', 'type', 'description'),
        }),
        ('Party link', {
            'classes': ('collapse',),
            'fields': ('party_type', 'party_id'),
            'description': 'Set only for per-client, per-supplier, per-investor '
                           'or per-loan accounts.',
        }),
        ('Status', {
            'fields': ('is_active', 'is_system', 'balance_display', 'created_at'),
        }),
    )

    @admin.display(description='Balance')
    def balance_display(self, obj):
        if not obj.pk:
            return '—'
        return format_html('<strong>৳{}</strong>', f'{obj.balance():,.2f}')


class TransactionLineInline(admin.TabularInline):
    model = TransactionLine
    extra = 0
    can_delete = False
    readonly_fields = ('account', 'amount', 'memo')

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ('reference_display', 'date', 'description', 'total_display',
                    'source_type', 'is_reversed')
    list_filter = ('date', 'source_type', 'is_reversed')
    search_fields = ('description',)
    date_hierarchy = 'date'
    ordering = ('-date', '-id')
    inlines = [TransactionLineInline]
    readonly_fields = ('date', 'description', 'source_type', 'source_id',
                       'created_by', 'created_at', 'is_reversed', 'reversal_of')

    @admin.display(description='Reference', ordering='id')
    def reference_display(self, obj):
        return obj.reference_no

    @admin.display(description='Amount')
    def total_display(self, obj):
        return f'৳{obj.total:,.2f}'

    def has_add_permission(self, request):
        # Posting must go through finance.services.post_transaction().
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Party)
class PartyAdmin(admin.ModelAdmin):
    list_display = ('name', 'party_type', 'phone', 'outstanding_display',
                    'credit_limit', 'is_active')
    list_filter = ('party_type', 'is_active')
    search_fields = ('name', 'phone', 'email')
    ordering = ('name',)
    readonly_fields = ('receivable_account', 'created_at', 'updated_at',
                       'outstanding_display')

    @admin.display(description='Outstanding')
    def outstanding_display(self, obj):
        return f'৳{obj.outstanding:,.2f}' if obj.pk else '—'


class InvoiceItemInline(admin.TabularInline):
    model = InvoiceItem
    extra = 0


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    """
    Issued invoices are read-only here — they sit on the ledger, and editing one
    behind the service's back would leave the posting disagreeing with the bill.
    Drafts stay editable.
    """
    list_display = ('display_number', 'party', 'issue_date', 'due_date',
                    'status', 'total_display')
    list_filter = ('status', 'issue_date')
    search_fields = ('number', 'party__name')
    date_hierarchy = 'issue_date'
    inlines = [InvoiceItemInline]
    readonly_fields = ('number', 'transaction', 'share_token', 'issued_at',
                       'cancelled_at', 'created_at', 'updated_at')

    @admin.display(description='Number', ordering='number')
    def display_number(self, obj):
        return obj.display_number

    @admin.display(description='Total')
    def total_display(self, obj):
        return f'৳{obj.total:,.2f}'

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj and not obj.is_editable:
            fields += ['party', 'issue_date', 'due_date', 'payment_terms_days',
                       'discount', 'delivery_charge', 'notes', 'status', 'order']
        return fields

    def has_delete_permission(self, request, obj=None):
        return obj is None or obj.is_editable
