"""
finance/views.py
────────────────
M1 — inspection screens (accounts, transactions, trial balance).
M2 — data entry: opening balances, expenses, transfers, and the day book.

No view writes to the ledger directly. Each one validates with a form, then
hands clean values to finance.services, which is the only thing that posts.
"""

import csv
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.models import User
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from .decorators import finance_staff_required
from .exceptions import LedgerError
from .integrations import affiliate_commission_owed, backfill_affiliate_history
from .forms import (
    AccountForm, CapitalForm, CategoryForm, DaybookFilterForm, ExpenseForm,
    InvestorForm, InvoiceCancelForm, InvoiceForm, InvoiceItemFormSet,
    LoanForm, LoanPaymentForm, OpeningBalanceForm, OpeningStockForm,
    PartyForm, PaymentForm, PaymentReverseForm, ProductForm,
    ProfitDistributionForm, PurchaseCancelForm, PurchaseForm,
    PurchaseItemFormSet, PurchaseReceiveForm, ReverseTransactionForm,
    StatementFilterForm, StockAdjustmentForm, SupplierPaymentForm,
    TransferForm, WalkInInvoiceForm, build_variant_formset,
)
from .models import (
    Account, Investor, Invoice, InvoiceItem, Loan, Party, Payment,
    ProfitDistribution, Purchase, Transaction, today,
)
from .services import (
    cancel_loan, delete_draft_invoice, delete_draft_purchase, delete_investor,
    delete_party, reverse_distribution, reverse_investor_movement,
    reverse_loan_payment, reverse_stock_movement,
    why_investor_cannot_be_deleted, why_party_cannot_be_deleted,
    with_outstanding, with_payment_totals,
    account_ledger, accounts_with_balances, adjust_stock, ageing_report,
    cancel_invoice, cancel_purchase, cash_on_hand, create_invoice_from_order,
    create_loan, current_unit_cost, daybook, distribute_profit,
    has_opening_balance, investor_statement, issue_invoice, loan_summary,
    low_stock, margin_report, mark_purchase_ordered, open_invoices_for,
    ownership_split, party_statement, period_profit, post_expense,
    post_opening_balance, post_transfer, purchase_batches, receivables_summary,
    oversold_variants, receive_opening_stock, receive_purchase,
    record_capital, record_drawing,
    record_loan_payment, record_payment, record_supplier_payment,
    reverse_payment, reverse_transaction, stock_cost_history, stock_valuation,
    trial_balance, type_balance, variant_stock,
)


def _group_by_type(accounts):
    """Group an annotated account queryset into display sections."""
    buckets = {}
    for account in accounts:
        buckets.setdefault(account.get_type_display(), []).append(account)
    return buckets


# ══════════════════════════════════════════════════════════════════════════════
#  DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

@finance_staff_required
def dashboard(request):
    """
    GET /manage/finance/
    Headline balances, and a health check that the ledger is internally sound.
    """
    today_date = today()
    month_start = today_date.replace(day=1)

    ctx = {
        'money_accounts':   accounts_with_balances(types=Account.MONEY_TYPES),
        'cash_total':       cash_on_hand(),
        'receivable_total': type_balance([Account.TYPE_RECEIVABLE]),
        'payable_total':    type_balance([Account.TYPE_PAYABLE]),
        'loan_total':       type_balance([Account.TYPE_LOAN_PAYABLE]),
        'stock_total':      type_balance([Account.TYPE_INVENTORY]),
        'profit':           period_profit(),
        'month_profit':     period_profit(since=month_start, as_of=today_date),
        'month_start':      month_start,
        'trial':            trial_balance(),
        'account_count':    Account.objects.filter(is_active=True).count(),
        'txn_count':        Transaction.objects.count(),
        'recent':           Transaction.objects.prefetch_related('lines__account')[:10],
        'receivables':      receivables_summary(),
        'loans':            loan_summary(),
        'low_stock':        low_stock(),
        'oversold':         oversold_variants(),
        'draft_invoices':   Invoice.objects.filter(status=Invoice.STATUS_DRAFT).count(),
        'affiliate_owed':   affiliate_commission_owed(),
    }
    return render(request, 'finance/dashboard.html', ctx)


# ══════════════════════════════════════════════════════════════════════════════
#  ACCOUNTS
# ══════════════════════════════════════════════════════════════════════════════

@finance_staff_required
def account_list(request):
    """GET /manage/finance/accounts/ — every account with its current balance."""
    show_inactive = request.GET.get('inactive') == '1'
    accounts = accounts_with_balances(include_inactive=show_inactive)

    ctx = {
        'grouped':       _group_by_type(accounts),
        'show_inactive': show_inactive,
        'total_count':   len(accounts),
    }
    return render(request, 'finance/account_list.html', ctx)


@finance_staff_required
def account_create(request):
    """GET/POST /manage/finance/accounts/new/"""
    form = AccountForm(request.POST or None)

    if request.method == 'POST' and form.is_valid():
        account = form.save()
        messages.success(request, f'Account "{account.name}" created.')
        return redirect('finance:account_detail', pk=account.pk)

    return render(request, 'finance/account_form.html', {
        'form': form,
        'is_new': True,
    })


@finance_staff_required
def account_edit(request, pk):
    """GET/POST /manage/finance/accounts/<pk>/edit/"""
    account = get_object_or_404(Account, pk=pk)
    form = AccountForm(request.POST or None, instance=account)

    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, f'Account "{account.name}" updated.')
        return redirect('finance:account_detail', pk=account.pk)

    return render(request, 'finance/account_form.html', {
        'form': form,
        'account': account,
        'is_new': False,
    })


@finance_staff_required
def account_detail(request, pk):
    """
    GET /manage/finance/accounts/<pk>/
    One account's entries in date order with a running balance.
    """
    account = get_object_or_404(Account, pk=pk)
    rows = account_ledger(account)

    ctx = {
        'account':      account,
        'rows':         list(reversed(rows)),   # newest first for reading
        'balance':      account.balance(),
        'has_opening':  has_opening_balance(account),
    }
    return render(request, 'finance/account_detail.html', ctx)


@finance_staff_required
def opening_balance(request, pk):
    """
    GET/POST /manage/finance/accounts/<pk>/opening-balance/
    Record what this account already held when the books started.
    """
    account = get_object_or_404(Account, pk=pk)
    form = OpeningBalanceForm(request.POST or None)

    if request.method == 'POST' and form.is_valid():
        try:
            post_opening_balance(
                account=account,
                date=form.cleaned_data['date'],
                amount=form.cleaned_data['amount'],
                created_by=request.user,
            )
        except LedgerError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                f'Opening balance recorded for {account.name}.',
            )
            return redirect('finance:account_detail', pk=account.pk)

    return render(request, 'finance/opening_balance_form.html', {
        'form': form,
        'account': account,
        'already_set': has_opening_balance(account),
    })


# ══════════════════════════════════════════════════════════════════════════════
#  EXPENSES
# ══════════════════════════════════════════════════════════════════════════════

@finance_staff_required
def expense_list(request):
    """GET /manage/finance/expenses/ — everything recorded as a cost."""
    qs = Transaction.objects.filter(
        source_type=Transaction.SOURCE_EXPENSE,
    ).prefetch_related('lines__account')

    category_id = request.GET.get('category', '')
    if category_id.isdigit():
        qs = qs.filter(lines__account_id=int(category_id)).distinct()

    paginator = Paginator(qs, 40)
    page_obj = paginator.get_page(request.GET.get('page', 1))

    ctx = {
        'page_obj':      page_obj,
        'categories':    accounts_with_balances(types=[Account.TYPE_EXPENSE]),
        'category_id':   category_id,
        'expense_total': type_balance([Account.TYPE_EXPENSE]),
    }
    return render(request, 'finance/expense_list.html', ctx)


@finance_staff_required
def expense_create(request):
    """GET/POST /manage/finance/expenses/new/ — record money spent."""
    form = ExpenseForm(request.POST or None)

    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        try:
            txn = post_expense(
                date=data['date'],
                expense_account=data['expense_account'],
                paid_from=data['paid_from'],
                amount=data['amount'],
                description=data['description'],
                memo=data['memo'],
                created_by=request.user,
            )
        except LedgerError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                f'Recorded ৳{data["amount"]:,.2f} — {data["description"]} '
                f'({txn.reference_no}).',
            )
            if 'save_and_add' in request.POST:
                return redirect('finance:expense_create')
            return redirect('finance:expense_list')

    return render(request, 'finance/expense_form.html', {
        'form': form,
        'money_accounts': accounts_with_balances(types=Account.MONEY_TYPES),
    })


# ══════════════════════════════════════════════════════════════════════════════
#  TRANSFERS
# ══════════════════════════════════════════════════════════════════════════════

@finance_staff_required
def transfer_create(request):
    """GET/POST /manage/finance/transfers/new/ — move money between own accounts."""
    form = TransferForm(request.POST or None)

    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        try:
            txn = post_transfer(
                date=data['date'],
                from_account=data['from_account'],
                to_account=data['to_account'],
                amount=data['amount'],
                fee=data['fee'],
                description=data['description'],
                created_by=request.user,
            )
        except LedgerError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                f'Moved ৳{data["amount"]:,.2f} from {data["from_account"].name} '
                f'to {data["to_account"].name} ({txn.reference_no}).',
            )
            return redirect('finance:daybook')

    return render(request, 'finance/transfer_form.html', {
        'form': form,
        'money_accounts': accounts_with_balances(types=Account.MONEY_TYPES),
    })


# ══════════════════════════════════════════════════════════════════════════════
#  DAY BOOK
# ══════════════════════════════════════════════════════════════════════════════

@finance_staff_required
def daybook_view(request):
    """
    GET /manage/finance/daybook/
    Every movement of spendable money in a date range, with a running balance.
    """
    form = DaybookFilterForm(request.GET or None)
    since = as_of = account = None

    if form.is_valid():
        since = form.cleaned_data.get('since')
        as_of = form.cleaned_data.get('as_of')
        account = form.cleaned_data.get('account')

    book = daybook(since=since, as_of=as_of, account=account)

    ctx = {
        'form':  form,
        'book':  book,
        'rows':  list(reversed(book['rows'])),   # newest first for reading
    }
    return render(request, 'finance/daybook.html', ctx)


# ══════════════════════════════════════════════════════════════════════════════
#  PARTIES
# ══════════════════════════════════════════════════════════════════════════════

@finance_staff_required
def party_list(request):
    """GET /manage/finance/parties/ — clients, walk-ins and suppliers."""
    qs = with_outstanding(Party.objects.select_related('receivable_account'))

    party_type = request.GET.get('type', '').strip()
    if party_type:
        qs = qs.filter(party_type=party_type)

    search = request.GET.get('q', '').strip()
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(phone__icontains=search))

    paginator = Paginator(qs.order_by('name'), 50)
    page_obj = paginator.get_page(request.GET.get('page', 1))

    ctx = {
        'page_obj':    page_obj,
        'parties':     page_obj,
        'party_type':  party_type,
        'search':      search,
        'type_choices': Party.TYPE_CHOICES,
        'total_owed':  type_balance([Account.TYPE_RECEIVABLE]),
        'total_count': paginator.count,
    }
    return render(request, 'finance/party_list.html', ctx)


@finance_staff_required
def party_create(request):
    """GET/POST /manage/finance/parties/new/"""
    form = PartyForm(request.POST or None)

    if request.method == 'POST' and form.is_valid():
        party = form.save()
        messages.success(request, f'"{party.name}" added.')
        if 'then_invoice' in request.POST:
            return redirect(f"{reverse('finance:invoice_create')}?party={party.pk}")
        return redirect('finance:party_detail', pk=party.pk)

    return render(request, 'finance/party_form.html', {'form': form, 'is_new': True})


@finance_staff_required
def party_edit(request, pk):
    """GET/POST /manage/finance/parties/<pk>/edit/"""
    party = get_object_or_404(Party, pk=pk)
    form = PartyForm(request.POST or None, instance=party)

    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, f'"{party.name}" updated.')
        return redirect('finance:party_detail', pk=party.pk)

    return render(request, 'finance/party_form.html', {
        'form': form, 'party': party, 'is_new': False,
    })


@finance_staff_required
def party_detail(request, pk):
    """GET /manage/finance/parties/<pk>/ — their invoices and what they owe."""
    party = get_object_or_404(
        Party.objects.select_related('receivable_account'), pk=pk,
    )
    invoices = with_payment_totals(
        party.invoices.prefetch_related('items')
    ).order_by('-issue_date', '-id')

    ctx = {
        'party':    party,
        'invoices': invoices,
        'open_count': sum(1 for inv in invoices if inv.is_open),
        'cannot_delete': why_party_cannot_be_deleted(party),
        'remove_url': reverse('finance:party_delete', kwargs={'pk': party.pk}),
    }
    return render(request, 'finance/party_detail.html', ctx)


# ══════════════════════════════════════════════════════════════════════════════
#  INVOICES
# ══════════════════════════════════════════════════════════════════════════════

@finance_staff_required
def invoice_list(request):
    """GET /manage/finance/invoices/"""
    qs = with_payment_totals(
        Invoice.objects.select_related('party').prefetch_related('items')
    )

    status = request.GET.get('status', '').strip()
    if status == 'overdue':
        qs = qs.filter(
            status__in=Invoice.OPEN_STATUSES,
            due_date__lt=today(),
        )
    elif status:
        qs = qs.filter(status=status)

    party_id = request.GET.get('party', '')
    if party_id.isdigit():
        qs = qs.filter(party_id=int(party_id))

    search = request.GET.get('q', '').strip()
    if search:
        qs = qs.filter(Q(number__icontains=search) | Q(party__name__icontains=search))

    paginator = Paginator(qs, 30)
    page_obj = paginator.get_page(request.GET.get('page', 1))

    all_invoices = Invoice.objects.all()
    open_invoices = all_invoices.filter(status__in=Invoice.OPEN_STATUSES)

    ctx = {
        'page_obj': page_obj,
        'status':   status,
        'search':   search,
        'party_id': party_id,
        'status_choices': Invoice.STATUS_CHOICES,
        'stats': {
            'total':     all_invoices.count(),
            'draft':     all_invoices.filter(status=Invoice.STATUS_DRAFT).count(),
            'open':      open_invoices.count(),
            'overdue':   open_invoices.filter(due_date__lt=today()).count(),
            'owed':      type_balance([Account.TYPE_RECEIVABLE]),
        },
    }
    return render(request, 'finance/invoice_list.html', ctx)


@finance_staff_required
def product_search(request):
    """
    GET /manage/finance/api/products/?q=cable
    Feeds the product picker on the invoice and purchase line rows.

    Returns the price and what is actually on hand, so the owner can see they
    are about to invoice something they do not have before they do it.
    """
    from store.models import ProductVariant

    query = request.GET.get('q', '').strip()

    qs = ProductVariant.objects.select_related('product').filter(is_active=True)
    if query:
        qs = qs.filter(
            Q(product__name__icontains=query)
            | Q(name__icontains=query)
            | Q(sku__icontains=query)
        )
    qs = qs.order_by('product__name', 'sort_order', 'id')[:20]

    results = [
        {
            'id':       variant.pk,
            'product':  variant.product.name,
            'variant':  variant.name,
            'label':    f'{variant.product.name} — {variant.name}',
            'sku':      variant.sku,
            'price':    f'{variant.price:.2f}',
            'stock':    variant.stock,
            'tracked':  variant.track_stock,
        }
        for variant in qs
    ]
    return JsonResponse({'results': results, 'query': query})


def _save_invoice_items(formset, invoice):
    """
    Persist the line formset, dropping rows the user left completely blank and
    keeping sort order matching what they see on screen.
    """
    items = formset.save(commit=False)
    for obj in formset.deleted_objects:
        obj.delete()
    for index, item in enumerate(items):
        item.invoice = invoice
        item.sort_order = index
        item.save()
    formset.save_m2m()


@finance_staff_required
def invoice_create(request):
    """GET/POST /manage/finance/invoices/new/ — a fresh draft."""
    invoice = Invoice()

    initial = {}
    party_id = request.GET.get('party', '')
    if party_id.isdigit():
        initial['party'] = party_id

    form = InvoiceForm(request.POST or None, instance=invoice, initial=initial)
    formset = InvoiceItemFormSet(request.POST or None, instance=invoice)

    if request.method == 'POST' and form.is_valid() and formset.is_valid():
        invoice = form.save(commit=False)
        invoice.created_by = request.user
        invoice.save()
        formset.instance = invoice
        _save_invoice_items(formset, invoice)

        messages.success(request, 'Draft invoice created. Check it, then issue it.')
        return redirect('finance:invoice_detail', pk=invoice.pk)

    return render(request, 'finance/invoice_form.html', {
        'form': form, 'formset': formset, 'is_new': True,
    })


@finance_staff_required
def invoice_edit(request, pk):
    """GET/POST /manage/finance/invoices/<pk>/edit/ — drafts only."""
    invoice = get_object_or_404(Invoice, pk=pk)

    if not invoice.is_editable:
        messages.error(
            request,
            f'{invoice.display_number} has been issued and is on the ledger. '
            f'Cancel it to make changes.',
        )
        return redirect('finance:invoice_detail', pk=invoice.pk)

    form = InvoiceForm(request.POST or None, instance=invoice)
    formset = InvoiceItemFormSet(request.POST or None, instance=invoice)

    if request.method == 'POST' and form.is_valid() and formset.is_valid():
        invoice = form.save()
        _save_invoice_items(formset, invoice)
        messages.success(request, 'Draft updated.')
        return redirect('finance:invoice_detail', pk=invoice.pk)

    return render(request, 'finance/invoice_form.html', {
        'form': form, 'formset': formset, 'invoice': invoice, 'is_new': False,
    })


@finance_staff_required
def invoice_walkin(request):
    """
    GET/POST /manage/finance/invoices/walk-in/
    Quick invoice for someone with no record — creates the party behind the scenes.
    """
    form = WalkInInvoiceForm(request.POST or None)

    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        party = Party.objects.create(
            name=data['customer_name'],
            party_type=Party.TYPE_WALKIN,
            phone=data['phone'],
        )
        invoice = Invoice.objects.create(
            party=party,
            issue_date=data['issue_date'],
            created_by=request.user,
        )
        messages.success(request, f'Draft started for {party.name}. Add the items.')
        return redirect('finance:invoice_edit', pk=invoice.pk)

    return render(request, 'finance/invoice_walkin_form.html', {'form': form})


@finance_staff_required
def invoice_from_order(request, order_number):
    """
    POST /manage/finance/invoices/from-order/<order_number>/
    One click from a website order to a draft invoice.

    POST only: as a plain link, a browser prefetch or a crawler following it
    would silently create draft invoices.
    """
    from store.models import Order

    order = get_object_or_404(Order, order_number=order_number)

    if request.method != 'POST':
        return redirect('store:manage_order_detail', order_number=order.order_number)

    try:
        invoice = create_invoice_from_order(order, created_by=request.user)
    except LedgerError as exc:
        messages.error(request, str(exc))
        return redirect('store:manage_order_detail', order_number=order.order_number)

    messages.success(
        request,
        f'Draft invoice created from order {order.order_number}. '
        f'Check it, then issue it.',
    )
    return redirect('finance:invoice_detail', pk=invoice.pk)


@finance_staff_required
def invoice_detail(request, pk):
    """GET /manage/finance/invoices/<pk>/"""
    invoice = get_object_or_404(
        Invoice.objects.select_related('party', 'transaction', 'order'), pk=pk,
    )
    share_url = None
    if invoice.share_token:
        share_url = request.build_absolute_uri(
            reverse('finance:invoice_public', kwargs={'token': invoice.share_token})
        )

    ctx = {
        'invoice':     invoice,
        'items':       invoice.items.all(),
        'share_url':   share_url,
        'cancel_form': InvoiceCancelForm(),
        'delete_url':  reverse('finance:invoice_delete', kwargs={'pk': invoice.pk}),
    }
    return render(request, 'finance/invoice_detail.html', ctx)


@finance_staff_required
def invoice_issue(request, pk):
    """POST /manage/finance/invoices/<pk>/issue/ — put it on the ledger."""
    invoice = get_object_or_404(Invoice, pk=pk)

    if request.method != 'POST':
        return redirect('finance:invoice_detail', pk=invoice.pk)

    try:
        issue_invoice(invoice, created_by=request.user)
    except LedgerError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            f'{invoice.number} issued for ৳{invoice.total:,.2f}. '
            f'{invoice.party.name} now owes it.',
        )

    return redirect('finance:invoice_detail', pk=invoice.pk)


@finance_staff_required
def invoice_cancel(request, pk):
    """POST /manage/finance/invoices/<pk>/cancel/"""
    invoice = get_object_or_404(Invoice, pk=pk)

    if request.method != 'POST':
        return redirect('finance:invoice_detail', pk=invoice.pk)

    form = InvoiceCancelForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Please give a reason for cancelling.')
        return redirect('finance:invoice_detail', pk=invoice.pk)

    try:
        cancel_invoice(
            invoice, reason=form.cleaned_data['reason'], created_by=request.user,
        )
    except LedgerError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f'{invoice.display_number} cancelled.')

    return redirect('finance:invoice_detail', pk=invoice.pk)


@finance_staff_required
def invoice_share(request, pk):
    """
    POST /manage/finance/invoices/<pk>/share/
    Mint a new share link, or switch sharing off.

    A link sent to the wrong person could not previously be taken back — the
    token lived forever with no way to kill it.
    """
    import secrets

    invoice = get_object_or_404(Invoice, pk=pk)

    if request.method != 'POST':
        return redirect('finance:invoice_detail', pk=invoice.pk)

    action = request.POST.get('action', 'regenerate')

    if action == 'revoke':
        invoice.share_token = None
        invoice.save(update_fields=['share_token', 'updated_at'])
        messages.success(
            request,
            'Sharing switched off. The old link no longer opens this invoice.',
        )
    else:
        invoice.share_token = secrets.token_urlsafe(32)
        invoice.save(update_fields=['share_token', 'updated_at'])
        messages.success(
            request,
            'New link created. Any link you sent before has stopped working.',
        )

    return redirect('finance:invoice_detail', pk=invoice.pk)


@finance_staff_required
def invoice_print(request, pk):
    """
    GET /manage/finance/invoices/<pk>/print/
    Print-optimised layout. Print → Save as PDF gives the client their copy.
    """
    invoice = get_object_or_404(Invoice.objects.select_related('party'), pk=pk)
    return render(request, 'finance/invoice_print.html', _print_context(invoice))


def invoice_public(request, token):
    """
    GET /invoice/<token>/
    The client's own copy — no login, reachable only with the token.

    Drafts and cancelled invoices are deliberately unreachable: a draft is not
    a real bill yet, and a cancelled one should stop being presentable.
    """
    invoice = get_object_or_404(
        Invoice.objects.select_related('party'), share_token=token,
    )
    if invoice.status in (Invoice.STATUS_DRAFT, Invoice.STATUS_CANCELLED):
        raise Http404('This invoice is not available.')

    ctx = _print_context(invoice)
    ctx['is_public'] = True
    return render(request, 'finance/invoice_print.html', ctx)


def _print_context(invoice):
    from store.models import SiteSettings
    return {
        'invoice':  invoice,
        'items':    invoice.items.all(),
        'settings': SiteSettings.get(),
        'is_public': False,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  PAYMENTS & DUES
# ══════════════════════════════════════════════════════════════════════════════

@finance_staff_required
def dues_dashboard(request):
    """GET /manage/finance/dues/ — who owes what, and how badly overdue."""
    summary = receivables_summary()
    ageing = ageing_report()

    overdue = [
        invoice for invoice in Invoice.objects.filter(
            status__in=Invoice.OPEN_STATUSES, due_date__lt=today(),
        ).select_related('party').order_by('due_date')[:15]
        if invoice.amount_due > 0
    ]

    ctx = {
        'summary':  summary,
        'ageing':   ageing,
        'overdue':  overdue,
        'payable_total': type_balance([Account.TYPE_PAYABLE]),
        'recent_payments': Payment.objects.select_related(
            'party', 'account', 'transaction',
        )[:10],
    }
    return render(request, 'finance/dues_dashboard.html', ctx)


@finance_staff_required
def ageing_view(request):
    """GET /manage/finance/dues/ageing/ — receivables bucketed by lateness."""
    as_of = _parse_date(request.GET.get('as_of'))

    return render(request, 'finance/ageing.html', {
        'ageing': ageing_report(as_of=as_of),
        'as_of':  as_of,
    })


@finance_staff_required
def payment_list(request):
    """GET /manage/finance/payments/"""
    qs = Payment.objects.select_related('party', 'account', 'transaction')

    direction = request.GET.get('direction', '').strip()
    if direction in (Payment.DIRECTION_IN, Payment.DIRECTION_OUT):
        qs = qs.filter(direction=direction)

    paginator = Paginator(qs, 40)
    page_obj = paginator.get_page(request.GET.get('page', 1))

    ctx = {
        'page_obj':     page_obj,
        'direction':    direction,
        'reverse_form': PaymentReverseForm(),
    }
    return render(request, 'finance/payment_list.html', ctx)


@finance_staff_required
def payment_create(request):
    """
    GET/POST /manage/finance/payments/new/
    Record money in. Allocation defaults to oldest invoice first; tick the
    boxes to decide by hand instead.
    """
    form = PaymentForm(request.POST or None, initial=_payment_initial(request))
    preview_party = _preview_party(request)

    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        allocations = _read_manual_allocations(request, data['party'])

        try:
            payment = record_payment(
                party=data['party'],
                date=data['date'],
                amount=data['amount'],
                account=data['account'],
                allocations=allocations,
                reference=data['reference'],
                notes=data['notes'],
                created_by=request.user,
            )
        except LedgerError as exc:
            messages.error(request, str(exc))
        else:
            leftover = payment.unallocated
            note = (
                f' ৳{leftover:,.2f} kept as an advance.' if leftover > 0 else ''
            )
            messages.success(
                request,
                f'Received ৳{payment.amount:,.2f} from {payment.party.name}.{note}',
            )
            return redirect('finance:party_statement', pk=payment.party.pk)

    ctx = {
        'form':          form,
        'preview_party': preview_party,
        'open_invoices': open_invoices_for(preview_party) if preview_party else [],
        'money_accounts': accounts_with_balances(types=Account.MONEY_TYPES),
    }
    return render(request, 'finance/payment_form.html', ctx)


def _payment_initial(request):
    party_id = request.GET.get('party', '')
    return {'party': party_id} if party_id.isdigit() else {}


def _preview_party(request):
    """The client whose open invoices we show while filling in the form."""
    party_id = request.POST.get('party') or request.GET.get('party') or ''
    if str(party_id).isdigit():
        return Party.objects.filter(pk=int(party_id)).first()
    return None


def _read_manual_allocations(request, party):
    """
    Read hand-entered allocations from the form, or return None to let the
    service settle the oldest invoices first.
    """
    if request.POST.get('allocation_mode') != 'manual':
        return None

    allocations = []
    for invoice in open_invoices_for(party):
        raw = (request.POST.get(f'alloc_{invoice.pk}') or '').strip()
        if not raw:
            continue
        try:
            value = Decimal(raw)
        except (InvalidOperation, ValueError):
            raise LedgerError(f'"{raw}" is not a valid amount.')
        if value > 0:
            allocations.append((invoice, value))
    return allocations


@finance_staff_required
def payment_reverse(request, pk):
    """POST /manage/finance/payments/<pk>/reverse/"""
    payment = get_object_or_404(Payment, pk=pk)

    if request.method != 'POST':
        return redirect('finance:payment_list')

    form = PaymentReverseForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Please give a reason for reversing the payment.')
        return redirect('finance:payment_list')

    try:
        reverse_payment(
            payment, reason=form.cleaned_data['reason'], created_by=request.user,
        )
    except LedgerError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            f'{payment.reference_no} reversed. The invoices it covered are '
            f'owed again.',
        )
    return redirect('finance:payment_list')


@finance_staff_required
def supplier_payment_create(request):
    """GET/POST /manage/finance/payments/supplier/"""
    form = SupplierPaymentForm(request.POST or None)

    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        try:
            record_supplier_payment(
                party=data['party'], date=data['date'], amount=data['amount'],
                paid_from=data['paid_from'], reference=data['reference'],
                notes=data['notes'], created_by=request.user,
            )
        except LedgerError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                f'Paid ৳{data["amount"]:,.2f} to {data["party"].name}.',
            )
            return redirect('finance:payment_list')

    return render(request, 'finance/supplier_payment_form.html', {
        'form': form,
        'money_accounts': accounts_with_balances(types=Account.MONEY_TYPES),
        'payable_total': type_balance([Account.TYPE_PAYABLE]),
    })


@finance_staff_required
def party_statement_view(request, pk):
    """GET /manage/finance/parties/<pk>/statement/ — printable client statement."""
    party = get_object_or_404(Party, pk=pk)
    form = StatementFilterForm(request.GET or None)

    since = as_of = None
    if form.is_valid():
        since = form.cleaned_data.get('since')
        as_of = form.cleaned_data.get('as_of')

    from store.models import SiteSettings
    ctx = {
        'party':     party,
        'form':      form,
        'statement': party_statement(party, since=since, as_of=as_of),
        'settings':  SiteSettings.get(),
    }
    return render(request, 'finance/party_statement.html', ctx)


# ══════════════════════════════════════════════════════════════════════════════
#  PURCHASES
# ══════════════════════════════════════════════════════════════════════════════

@finance_staff_required
def purchase_list(request):
    """GET /manage/finance/purchases/"""
    qs = Purchase.objects.select_related('supplier').prefetch_related('items__variant')

    status = request.GET.get('status', '').strip()
    if status:
        qs = qs.filter(status=status)

    paginator = Paginator(qs, 30)
    page_obj = paginator.get_page(request.GET.get('page', 1))

    ctx = {
        'page_obj': page_obj,
        'status':   status,
        'status_choices': Purchase.STATUS_CHOICES,
        'in_transit_value': type_balance([Account.TYPE_GOODS_IN_TRANSIT]),
        'stock_value': type_balance([Account.TYPE_INVENTORY]),
        'stats': {
            'draft':      Purchase.objects.filter(status=Purchase.STATUS_DRAFT).count(),
            'in_flight':  Purchase.objects.filter(
                              status__in=Purchase.IN_FLIGHT_STATUSES).count(),
            'received':   Purchase.objects.filter(status=Purchase.STATUS_RECEIVED).count(),
        },
    }
    return render(request, 'finance/purchase_list.html', ctx)


def _save_purchase_items(formset, purchase):
    items = formset.save(commit=False)
    for obj in formset.deleted_objects:
        obj.delete()
    for index, item in enumerate(items):
        item.purchase = purchase
        item.sort_order = index
        item.save()
    formset.save_m2m()


def _formset_has_row(formset, key):
    """True if the user filled in at least one row that is not being deleted."""
    for row_form in formset.forms:
        cleaned = getattr(row_form, 'cleaned_data', None) or {}
        if cleaned.get('DELETE'):
            continue
        value = cleaned.get(key)
        if isinstance(value, str):
            value = value.strip()
        if value:
            return True
    return False


@finance_staff_required
def purchase_create(request):
    """GET/POST /manage/finance/purchases/new/ — stage 1."""
    purchase = Purchase()
    form = PurchaseForm(request.POST or None, instance=purchase)
    formset = PurchaseItemFormSet(request.POST or None, instance=purchase)

    if request.method == 'POST' and form.is_valid() and formset.is_valid():
        if not _formset_has_row(formset, 'variant'):
            # An empty purchase can never be ordered or received, so there is
            # nothing to be gained by letting one be created.
            messages.error(
                request,
                'Search for at least one product before creating this purchase.',
            )
        else:
            purchase = form.save(commit=False)
            purchase.created_by = request.user
            purchase.save()
            formset.instance = purchase
            _save_purchase_items(formset, purchase)

            messages.success(
                request,
                f'{purchase.purchase_no} created. Confirm it once the order is placed, '
                f'then enter the weights when it lands.',
            )
            return redirect('finance:purchase_detail', pk=purchase.pk)

    return render(request, 'finance/purchase_form.html', {
        'form': form, 'formset': formset, 'is_new': True,
        'money_accounts': accounts_with_balances(types=Account.MONEY_TYPES),
    })


@finance_staff_required
def purchase_edit(request, pk):
    """GET/POST /manage/finance/purchases/<pk>/edit/ — stage 2 lives here too."""
    purchase = get_object_or_404(Purchase, pk=pk)

    if not purchase.is_editable:
        messages.error(
            request,
            f'{purchase.purchase_no} has been received and its costs are frozen '
            f'into the stock batches.',
        )
        return redirect('finance:purchase_detail', pk=purchase.pk)

    form = PurchaseForm(request.POST or None, instance=purchase)
    formset = PurchaseItemFormSet(request.POST or None, instance=purchase)

    if request.method == 'POST' and form.is_valid() and formset.is_valid():
        purchase = form.save()
        _save_purchase_items(formset, purchase)
        messages.success(request, f'{purchase.purchase_no} updated.')
        return redirect('finance:purchase_detail', pk=purchase.pk)

    return render(request, 'finance/purchase_form.html', {
        'form': form, 'formset': formset, 'purchase': purchase, 'is_new': False,
        'money_accounts': accounts_with_balances(types=Account.MONEY_TYPES),
    })


@finance_staff_required
def purchase_detail(request, pk):
    """GET /manage/finance/purchases/<pk>/ — the cost breakdown."""
    purchase = get_object_or_404(
        Purchase.objects.select_related('supplier'), pk=pk,
    )
    ctx = {
        'purchase':     purchase,
        'items':        purchase.items.select_related('variant__product'),
        'batches':      purchase_batches(purchase),
        'receive_form': PurchaseReceiveForm(),
        'cancel_form':  PurchaseCancelForm(),
        'delete_url':   reverse('finance:purchase_delete', kwargs={'pk': purchase.pk}),
    }
    return render(request, 'finance/purchase_detail.html', ctx)


@finance_staff_required
def purchase_order(request, pk):
    """POST /manage/finance/purchases/<pk>/order/ — confirm and book in transit."""
    purchase = get_object_or_404(Purchase, pk=pk)
    if request.method != 'POST':
        return redirect('finance:purchase_detail', pk=purchase.pk)

    try:
        mark_purchase_ordered(purchase, created_by=request.user)
    except LedgerError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            f'{purchase.purchase_no} confirmed. The goods are booked as in transit '
            f'until you receive them.',
        )
    return redirect('finance:purchase_detail', pk=purchase.pk)


@finance_staff_required
def purchase_receive(request, pk):
    """POST /manage/finance/purchases/<pk>/receive/ — stage 2, stock goes in."""
    purchase = get_object_or_404(Purchase, pk=pk)
    if request.method != 'POST':
        return redirect('finance:purchase_detail', pk=purchase.pk)

    form = PurchaseReceiveForm(request.POST)
    received_date = form.cleaned_data['received_date'] if form.is_valid() else None

    try:
        receive_purchase(
            purchase, received_date=received_date, created_by=request.user,
        )
    except LedgerError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            f'{purchase.purchase_no} received — {purchase.total_quantity} unit(s) '
            f'added to stock at a landed cost of ৳{purchase.landed_total_bdt:,.2f}.',
        )
    return redirect('finance:purchase_detail', pk=purchase.pk)


@finance_staff_required
def purchase_cancel(request, pk):
    """POST /manage/finance/purchases/<pk>/cancel/"""
    purchase = get_object_or_404(Purchase, pk=pk)
    if request.method != 'POST':
        return redirect('finance:purchase_detail', pk=purchase.pk)

    form = PurchaseCancelForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Please give a reason for cancelling.')
        return redirect('finance:purchase_detail', pk=purchase.pk)

    try:
        cancel_purchase(
            purchase, reason=form.cleaned_data['reason'], created_by=request.user,
        )
    except LedgerError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f'{purchase.purchase_no} cancelled.')
        if purchase.payment_id:
            # Cancelling the order does not un-send the money. Leaving the
            # payment standing turns the supplier balance into an advance,
            # which is what actually happened.
            messages.warning(
                request,
                f'৳{purchase.payment.amount:,.2f} was already paid to '
                f'{purchase.supplier.name} and has been left in place — they now '
                f'hold that as an advance. Reverse it from Payments if it was '
                f'refunded.',
            )
    return redirect('finance:purchase_detail', pk=purchase.pk)


@finance_staff_required
def margin_view(request):
    """GET /manage/finance/margins/ — what each product actually earns."""
    rows = margin_report()
    losing = [row for row in rows if row['has_cost'] and row['margin'] <= 0]

    return render(request, 'finance/margins.html', {
        'rows':   rows,
        'losing': losing,
        'valuation': stock_valuation(),
    })


# ══════════════════════════════════════════════════════════════════════════════
#  CATALOGUE — PRODUCTS
# ══════════════════════════════════════════════════════════════════════════════

@finance_staff_required
def product_list(request):
    """GET /manage/finance/products/ — the catalogue, with cost and stock."""
    from store.models import Category, Product

    qs = Product.objects.select_related('category').prefetch_related('variants')

    search = request.GET.get('q', '').strip()
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(sku__icontains=search))

    category_id = request.GET.get('category', '')
    if category_id.isdigit():
        qs = qs.filter(category_id=int(category_id))

    paginator = Paginator(qs.order_by('name'), 30)
    page_obj = paginator.get_page(request.GET.get('page', 1))

    # Landed cost per variant, in one pass rather than a query per row.
    costs = {row['variant'].pk: row for row in margin_report(only_active=False)}

    ctx = {
        'page_obj':    page_obj,
        'search':      search,
        'category_id': category_id,
        'categories':  Category.objects.order_by('sort_order', 'name'),
        'costs':       costs,
        'total':       Product.objects.count(),
        'variant_total': sum(p.variants.count() for p in page_obj),
    }
    return render(request, 'finance/product_list.html', ctx)


def _save_variants(formset, product):
    """Persist the variant rows, skipping the spares the user left blank."""
    variants = formset.save(commit=False)
    for obj in formset.deleted_objects:
        obj.delete()
    for index, variant in enumerate(variants):
        variant.product = product
        if not variant.sort_order:
            variant.sort_order = index
        variant.save()
    formset.save_m2m()


@finance_staff_required
def product_create(request):
    """GET/POST /manage/finance/products/new/"""
    from store.models import Category, Product

    VariantFormSet = build_variant_formset(extra=3)
    product = Product()
    form = ProductForm(request.POST or None, instance=product)
    formset = VariantFormSet(request.POST or None, instance=product)

    if not Category.objects.exists():
        messages.warning(
            request,
            'There are no categories yet, and every product needs one. '
            'Create a category first.',
        )
        return redirect('finance:category_create')

    if request.method == 'POST' and form.is_valid() and formset.is_valid():
        if not _has_any_variant(formset):
            messages.error(
                request,
                'Add at least one option — price and stock live on the option, '
                'not the product. Name it "Standard" if there is only one.',
            )
        else:
            product = form.save()
            formset.instance = product
            _save_variants(formset, product)
            messages.success(
                request,
                f'"{product.name}" added with {product.variants.count()} option(s). '
                f'You can now buy it in and sell it.',
            )
            if 'then_purchase' in request.POST:
                return redirect('finance:purchase_create')
            return redirect('finance:product_list')

    return render(request, 'finance/product_form.html', {
        'form': form, 'formset': formset, 'is_new': True,
    })


@finance_staff_required
def product_edit(request, pk):
    """GET/POST /manage/finance/products/<pk>/edit/"""
    from store.models import Product

    product = get_object_or_404(Product, pk=pk)
    VariantFormSet = build_variant_formset(extra=2)
    form = ProductForm(request.POST or None, instance=product)
    formset = VariantFormSet(request.POST or None, instance=product)

    if request.method == 'POST' and form.is_valid() and formset.is_valid():
        product = form.save()
        _save_variants(formset, product)
        messages.success(request, f'"{product.name}" updated.')
        return redirect('finance:product_list')

    return render(request, 'finance/product_form.html', {
        'form': form, 'formset': formset, 'product': product, 'is_new': False,
    })


def _has_any_variant(formset):
    """True if the user filled in at least one option row."""
    return _formset_has_row(formset, 'name')


@finance_staff_required
def category_create(request):
    """GET/POST /manage/finance/products/categories/new/"""
    from store.models import Category

    form = CategoryForm(request.POST or None)

    if request.method == 'POST' and form.is_valid():
        category = form.save()
        messages.success(request, f'Category "{category.name}" created.')
        return redirect('finance:product_create')

    return render(request, 'finance/category_form.html', {
        'form': form,
        'categories': Category.objects.order_by('sort_order', 'name'),
    })


# ══════════════════════════════════════════════════════════════════════════════
#  STOCK
# ══════════════════════════════════════════════════════════════════════════════

@finance_staff_required
def stock_list(request):
    """GET /manage/finance/stock/ — what is on hand and what it is worth."""
    valuation = stock_valuation()

    search = request.GET.get('q', '').strip()
    rows = valuation['rows']
    if search:
        rows = [
            row for row in rows
            if search.lower() in str(row['variant']).lower()
        ]

    return render(request, 'finance/stock_list.html', {
        'valuation': valuation,
        'rows':      rows,
        'search':    search,
        'low_stock': low_stock(),
        'oversold':  oversold_variants(),
    })


@finance_staff_required
def stock_detail(request, variant_id):
    """
    GET /manage/finance/stock/<variant_id>/
    Cost history per shipment, plus every movement that touched this product.
    """
    from store.models import ProductVariant

    variant = get_object_or_404(
        ProductVariant.objects.select_related('product'), pk=variant_id,
    )
    movements = variant.stock_movements.select_related(
        'transaction', 'batch',
    ).prefetch_related('consumptions__batch')[:100]

    from .services import UNDOABLE_STOCK_REASONS

    return render(request, 'finance/stock_detail.html', {
        'variant':      variant,
        'history':      stock_cost_history(variant),
        'movements':    movements,
        'on_hand':      variant_stock(variant),
        'unit_cost':    current_unit_cost(variant),
        'undoable':     UNDOABLE_STOCK_REASONS,
    })


@finance_staff_required
def stock_adjust(request):
    """GET/POST /manage/finance/stock/adjust/"""
    from .models import StockMovement

    form = StockAdjustmentForm(request.POST or None)

    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        adding = data['direction'] == StockAdjustmentForm.ADJUST_ADD
        quantity = data['quantity'] if adding else -data['quantity']
        reason = (
            StockMovement.REASON_ADJUST if adding else StockMovement.REASON_DAMAGE
        )

        try:
            adjust_stock(
                variant=data['variant'], quantity=quantity, reason=reason,
                note=data['note'], date=data['date'],
                unit_cost=data['unit_cost'], created_by=request.user,
            )
        except LedgerError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                f'{data["variant"]} adjusted by {quantity:+d}.',
            )
            return redirect('finance:stock_detail', variant_id=data['variant'].pk)

    return render(request, 'finance/stock_adjust_form.html', {'form': form})


@finance_staff_required
def stock_opening(request):
    """GET/POST /manage/finance/stock/opening/"""
    form = OpeningStockForm(request.POST or None)

    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        try:
            receive_opening_stock(
                variant=data['variant'], quantity=data['quantity'],
                unit_cost=data['unit_cost'], date=data['date'],
                created_by=request.user,
            )
        except LedgerError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                f'{data["quantity"]} x {data["variant"]} added as opening stock.',
            )
            return redirect('finance:stock_detail', variant_id=data['variant'].pk)

    return render(request, 'finance/stock_opening_form.html', {'form': form})


@finance_staff_required
def stock_movement_list(request):
    """GET /manage/finance/stock/movements/ — the whole stock ledger."""
    from .models import StockMovement

    qs = StockMovement.objects.select_related(
        'variant__product', 'transaction',
    ).prefetch_related('consumptions__batch')

    reason = request.GET.get('reason', '').strip()
    if reason:
        qs = qs.filter(reason=reason)

    paginator = Paginator(qs, 50)
    page_obj = paginator.get_page(request.GET.get('page', 1))

    return render(request, 'finance/stock_movements.html', {
        'page_obj': page_obj,
        'reason':   reason,
        'reason_choices': StockMovement.REASON_CHOICES,
    })


# ══════════════════════════════════════════════════════════════════════════════
#  INVESTORS
# ══════════════════════════════════════════════════════════════════════════════

@finance_staff_required
def investor_list(request):
    """GET /manage/finance/investors/"""
    split = ownership_split()
    total_capital = sum((investor.capital_in for investor, _pct in split), Decimal('0.00'))

    return render(request, 'finance/investor_list.html', {
        'split':         split,
        'total_capital': total_capital,
        'total_equity':  type_balance([Account.TYPE_EQUITY]),
        'distributions': ProfitDistribution.objects.prefetch_related(
                             'shares__investor')[:10],
    })


@finance_staff_required
def investor_create(request):
    """GET/POST /manage/finance/investors/new/"""
    form = InvestorForm(request.POST or None)

    if request.method == 'POST' and form.is_valid():
        investor = form.save()
        messages.success(request, f'{investor.name} added.')
        return redirect('finance:investor_detail', pk=investor.pk)

    return render(request, 'finance/investor_form.html', {'form': form, 'is_new': True})


@finance_staff_required
def investor_edit(request, pk):
    """GET/POST /manage/finance/investors/<pk>/edit/"""
    investor = get_object_or_404(Investor, pk=pk)
    form = InvestorForm(request.POST or None, instance=investor)

    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, f'{investor.name} updated.')
        return redirect('finance:investor_detail', pk=investor.pk)

    return render(request, 'finance/investor_form.html', {
        'form': form, 'investor': investor, 'is_new': False,
    })


@finance_staff_required
def investor_detail(request, pk):
    """GET /manage/finance/investors/<pk>/ — their statement."""
    investor = get_object_or_404(
        Investor.objects.select_related('equity_account'), pk=pk,
    )
    percent = dict(
        (inv.pk, pct) for inv, pct in ownership_split()
    ).get(investor.pk, Decimal('0.000'))

    return render(request, 'finance/investor_detail.html', {
        'investor':  investor,
        'statement': investor_statement(investor),
        'percent':   percent,
        'shares':    investor.profit_shares.select_related('distribution'),
        'cannot_delete': why_investor_cannot_be_deleted(investor),
        'remove_url': reverse('finance:investor_delete', kwargs={'pk': investor.pk}),
    })


@finance_staff_required
def investor_capital(request, pk):
    """GET/POST /manage/finance/investors/<pk>/capital/"""
    investor = get_object_or_404(Investor, pk=pk)
    form = CapitalForm(request.POST or None)

    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        action = (
            record_capital if data['movement'] == CapitalForm.MOVEMENT_IN
            else record_drawing
        )
        try:
            action(
                investor=investor, date=data['date'], amount=data['amount'],
                account=data['account'], notes=data['notes'],
                created_by=request.user,
            )
        except LedgerError as exc:
            messages.error(request, str(exc))
        else:
            word = 'in' if data['movement'] == CapitalForm.MOVEMENT_IN else 'out'
            messages.success(
                request,
                f'৳{data["amount"]:,.2f} recorded {word} for {investor.name}.',
            )
            return redirect('finance:investor_detail', pk=investor.pk)

    return render(request, 'finance/investor_capital_form.html', {
        'form': form,
        'investor': investor,
        'money_accounts': accounts_with_balances(types=Account.MONEY_TYPES),
    })


@finance_staff_required
def profit_distribute(request):
    """GET/POST /manage/finance/investors/distribute/"""
    form = ProfitDistributionForm(request.POST or None)

    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        try:
            distribution = distribute_profit(
                period_start=data['period_start'],
                period_end=data['period_end'],
                distribution_date=data['distribution_date'],
                amount=data['amount'],
                notes=data['notes'],
                created_by=request.user,
            )
        except LedgerError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                f'৳{distribution.distributed_amount:,.2f} shared out across '
                f'{distribution.shares.count()} investor(s).',
            )
            return redirect('finance:investor_list')

    return render(request, 'finance/profit_distribute_form.html', {
        'form':  form,
        'split': ownership_split(),
        'profit': period_profit(),
    })


# ══════════════════════════════════════════════════════════════════════════════
#  LOANS
# ══════════════════════════════════════════════════════════════════════════════

@finance_staff_required
def loan_list(request):
    """GET /manage/finance/loans/"""
    qs = Loan.objects.select_related('account').prefetch_related('installments')

    direction = request.GET.get('direction', '').strip()
    if direction in (Loan.DIRECTION_TAKEN, Loan.DIRECTION_GIVEN):
        qs = qs.filter(direction=direction)

    paginator = Paginator(qs.order_by('-start_date', '-id'), 50)
    page_obj = paginator.get_page(request.GET.get('page', 1))

    return render(request, 'finance/loan_list.html', {
        'page_obj':  page_obj,
        'loans':     page_obj,
        'direction': direction,
        'summary':   loan_summary(),
    })


@finance_staff_required
def loan_create(request):
    """GET/POST /manage/finance/loans/new/"""
    form = LoanForm(request.POST or None)

    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        try:
            loan = create_loan(
                direction=data['direction'],
                counterparty_name=data['counterparty_name'],
                principal=data['principal'],
                interest_rate=data['interest_rate'],
                method=data['method'],
                tenure_months=data['tenure_months'],
                start_date=data['start_date'],
                account=data['account'],
                notes=data['notes'],
                created_by=request.user,
            )
        except LedgerError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                f'{loan.loan_no} recorded with {loan.tenure_months} installment(s).',
            )
            return redirect('finance:loan_detail', pk=loan.pk)

    return render(request, 'finance/loan_form.html', {
        'form': form,
        'money_accounts': accounts_with_balances(types=Account.MONEY_TYPES),
    })


@finance_staff_required
def loan_detail(request, pk):
    """GET /manage/finance/loans/<pk>/ — the schedule and what is left."""
    loan = get_object_or_404(Loan.objects.select_related('account'), pk=pk)

    return render(request, 'finance/loan_detail.html', {
        'loan':         loan,
        'installments': loan.installments.all(),
        'payments':     loan.payments.select_related('account', 'transaction'),
        'payment_form': LoanPaymentForm(loan=loan),
        'cancel_url':   reverse('finance:loan_cancel', kwargs={'pk': loan.pk}),
    })


@finance_staff_required
def loan_pay(request, pk):
    """POST /manage/finance/loans/<pk>/pay/"""
    loan = get_object_or_404(Loan, pk=pk)

    if request.method != 'POST':
        return redirect('finance:loan_detail', pk=loan.pk)

    form = LoanPaymentForm(request.POST, loan=loan)
    if not form.is_valid():
        messages.error(request, 'Check the repayment details and try again.')
        return render(request, 'finance/loan_detail.html', {
            'loan': loan,
            'installments': loan.installments.all(),
            'payments': loan.payments.select_related('account'),
            'payment_form': form,
        })

    data = form.cleaned_data
    try:
        payment = record_loan_payment(
            loan=loan, date=data['date'],
            principal_amount=data['principal_amount'],
            interest_amount=data['interest_amount'],
            account=data['account'], installment=data['installment'],
            reference=data['reference'], created_by=request.user,
        )
    except LedgerError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request, f'৳{payment.total_amount:,.2f} recorded against {loan.loan_no}.',
        )
    return redirect('finance:loan_detail', pk=loan.pk)


# ══════════════════════════════════════════════════════════════════════════════
#  UNDO / DELETE
# ══════════════════════════════════════════════════════════════════════════════
#
#  One rule, applied everywhere: if nothing was posted it is really deleted; if
#  money moved it is undone by posting the opposite. The buttons say which is
#  about to happen so the outcome is never a surprise.

def _undo_reason(request):
    return (request.POST.get('reason') or '').strip()


@finance_staff_required
def investor_movement_undo(request, pk, txn_id):
    """POST /manage/finance/investors/<pk>/undo/<txn_id>/"""
    investor = get_object_or_404(Investor, pk=pk)
    txn = get_object_or_404(Transaction, pk=txn_id)

    if request.method != 'POST':
        return redirect('finance:investor_detail', pk=investor.pk)

    try:
        reverse_investor_movement(
            txn, reason=_undo_reason(request), created_by=request.user)
    except LedgerError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            f'{txn.reference_no} undone. The opposite entry has been posted and '
            f'both stay in the history.',
        )
    return redirect('finance:investor_detail', pk=investor.pk)


@finance_staff_required
def investor_delete(request, pk):
    """POST /manage/finance/investors/<pk>/delete/"""
    investor = get_object_or_404(Investor, pk=pk)

    if request.method != 'POST':
        return redirect('finance:investor_detail', pk=investor.pk)

    name = investor.name
    try:
        delete_investor(investor)
    except LedgerError as exc:
        messages.error(request, str(exc))
        return redirect('finance:investor_detail', pk=investor.pk)

    messages.success(request, f'{name} removed. Nothing had been posted for them.')
    return redirect('finance:investor_list')


@finance_staff_required
def distribution_undo(request, pk):
    """POST /manage/finance/investors/distributions/<pk>/undo/"""
    distribution = get_object_or_404(ProfitDistribution, pk=pk)

    if request.method != 'POST':
        return redirect('finance:investor_list')

    try:
        reverse_distribution(
            distribution, reason=_undo_reason(request), created_by=request.user)
    except LedgerError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            'Profit distribution undone. Each investor\'s stake has gone back.',
        )
    return redirect('finance:investor_list')


@finance_staff_required
def loan_cancel(request, pk):
    """POST /manage/finance/loans/<pk>/cancel/"""
    loan = get_object_or_404(Loan, pk=pk)

    if request.method != 'POST':
        return redirect('finance:loan_detail', pk=loan.pk)

    try:
        cancel_loan(loan, reason=_undo_reason(request), created_by=request.user)
    except LedgerError as exc:
        messages.error(request, str(exc))
        return redirect('finance:loan_detail', pk=loan.pk)

    messages.success(
        request,
        f'{loan.loan_no} cancelled. The money movement has been reversed and '
        f'its schedule removed.',
    )
    return redirect('finance:loan_list')


@finance_staff_required
def loan_payment_undo(request, pk, payment_id):
    """POST /manage/finance/loans/<pk>/payments/<payment_id>/undo/"""
    loan = get_object_or_404(Loan, pk=pk)
    payment = get_object_or_404(loan.payments, pk=payment_id)

    if request.method != 'POST':
        return redirect('finance:loan_detail', pk=loan.pk)

    try:
        reverse_loan_payment(
            payment, reason=_undo_reason(request), created_by=request.user)
    except LedgerError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            f'৳{payment.total_amount:,.2f} repayment undone — it is owed again.',
        )
    return redirect('finance:loan_detail', pk=loan.pk)


@finance_staff_required
def stock_movement_undo(request, pk):
    """POST /manage/finance/stock/movements/<pk>/undo/"""
    from .models import StockMovement

    movement = get_object_or_404(StockMovement, pk=pk)

    if request.method != 'POST':
        return redirect('finance:stock_movements')

    try:
        reverse_stock_movement(
            movement, reason=_undo_reason(request), created_by=request.user)
    except LedgerError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, 'Stock entry taken back.')
    return redirect('finance:stock_detail', variant_id=movement.variant_id)


@finance_staff_required
def party_delete(request, pk):
    """POST /manage/finance/parties/<pk>/delete/"""
    party = get_object_or_404(Party, pk=pk)

    if request.method != 'POST':
        return redirect('finance:party_detail', pk=party.pk)

    name = party.name
    try:
        delete_party(party)
    except LedgerError as exc:
        messages.error(request, str(exc))
        return redirect('finance:party_detail', pk=party.pk)

    messages.success(request, f'{name} removed. They had never traded.')
    return redirect('finance:party_list')


@finance_staff_required
def invoice_delete(request, pk):
    """POST /manage/finance/invoices/<pk>/delete/"""
    invoice = get_object_or_404(Invoice, pk=pk)

    if request.method != 'POST':
        return redirect('finance:invoice_detail', pk=invoice.pk)

    label = invoice.display_number
    try:
        delete_draft_invoice(invoice)
    except LedgerError as exc:
        messages.error(request, str(exc))
        return redirect('finance:invoice_detail', pk=invoice.pk)

    messages.success(request, f'{label} deleted. It had never been issued.')
    return redirect('finance:invoice_list')


@finance_staff_required
def purchase_delete(request, pk):
    """POST /manage/finance/purchases/<pk>/delete/"""
    purchase = get_object_or_404(Purchase, pk=pk)

    if request.method != 'POST':
        return redirect('finance:purchase_detail', pk=purchase.pk)

    label = purchase.purchase_no
    try:
        delete_draft_purchase(purchase)
    except LedgerError as exc:
        messages.error(request, str(exc))
        return redirect('finance:purchase_detail', pk=purchase.pk)

    messages.success(request, f'{label} deleted. It had never been confirmed.')
    return redirect('finance:purchase_list')


# ══════════════════════════════════════════════════════════════════════════════
#  REPORTS
# ══════════════════════════════════════════════════════════════════════════════

def _parse_date(value):
    """
    Parse a YYYY-MM-DD query parameter, returning None for anything else.

    Passing a raw string straight into a date filter turns `?as_of=garbage`
    into a 500, so every view that accepts a date goes through here.
    """
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None


def _period_from_request(request):
    """Read a since/as_of window from the query string, defaulting to this month."""
    parse = _parse_date

    since = parse(request.GET.get('since'))
    as_of = parse(request.GET.get('as_of'))

    if since is None and as_of is None:
        today_date = today()
        since = today_date.replace(day=1)
        as_of = today_date

    return since, as_of


@finance_staff_required
def profit_loss(request):
    """GET /manage/finance/reports/profit-loss/"""
    since, as_of = _period_from_request(request)

    income = accounts_with_balances(
        types=[Account.TYPE_INCOME], since=since, as_of=as_of)
    expense = accounts_with_balances(
        types=[Account.TYPE_EXPENSE], since=since, as_of=as_of)

    income_rows = [row for row in income if row.natural_total != 0]
    expense_rows = [row for row in expense if row.natural_total != 0]

    return render(request, 'finance/profit_loss.html', {
        'since':   since,
        'as_of':   as_of,
        'income':  income_rows,
        'expense': expense_rows,
        'result':  period_profit(since=since, as_of=as_of),
    })


@finance_staff_required
def cash_flow(request):
    """GET /manage/finance/reports/cash-flow/ — money in and out, by month."""
    since, as_of = _period_from_request(request)
    book = daybook(since=since, as_of=as_of)

    months = {}
    for row in book['rows']:
        key = row['transaction'].date.strftime('%Y-%m')
        bucket = months.setdefault(key, {
            'label': row['transaction'].date.strftime('%b %Y'),
            'in': Decimal('0.00'), 'out': Decimal('0.00'),
        })
        if row['money_in']:
            bucket['in'] += row['money_in']
        if row['money_out']:
            bucket['out'] += row['money_out']

    rows = []
    running = book['opening_balance']
    for key in sorted(months):
        bucket = months[key]
        opening = running
        running = running + bucket['in'] - bucket['out']
        rows.append({
            **bucket,
            'opening': opening,
            'net': bucket['in'] - bucket['out'],
            'closing': running,
        })

    return render(request, 'finance/cash_flow.html', {
        'since': since, 'as_of': as_of, 'book': book, 'rows': rows,
    })


@finance_staff_required
def export_csv(request, report):
    """
    GET /manage/finance/reports/export/<report>/
    CSV for the reports the owner is most likely to want in a spreadsheet.
    """
    since, as_of = _period_from_request(request)

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = (
        f'attachment; filename="alphacue-{report}-{today()}.csv"'
    )
    writer = csv.writer(response)

    if report == 'profit-loss':
        writer.writerow(['Account', 'Type', 'Amount'])
        for row in accounts_with_balances(
            types=[Account.TYPE_INCOME, Account.TYPE_EXPENSE],
            since=since, as_of=as_of,
        ):
            if row.natural_total:
                writer.writerow([row.name, row.get_type_display(), row.natural_total])
        result = period_profit(since=since, as_of=as_of)
        writer.writerow([])
        writer.writerow(['Income', '', result['income']])
        writer.writerow(['Expense', '', result['expense']])
        writer.writerow(['Net profit', '', result['profit']])

    elif report == 'daybook':
        writer.writerow(['Date', 'Reference', 'Description', 'Account',
                         'In', 'Out', 'Balance'])
        for row in daybook(since=since, as_of=as_of)['rows']:
            writer.writerow([
                row['transaction'].date, row['transaction'].reference_no,
                row['transaction'].description, row['account'].name,
                row['money_in'] or '', row['money_out'] or '',
                row['running_balance'],
            ])

    elif report == 'ageing':
        report_data = ageing_report()
        writer.writerow(['Client'] + report_data['buckets'] + ['Total'])
        for party_row in report_data['parties']:
            writer.writerow(
                [party_row['party'].name]
                + [party_row['buckets'][bucket] for bucket in report_data['buckets']]
                + [party_row['total']]
            )

    elif report == 'stock':
        writer.writerow(['Product', 'Quantity', 'Unit cost', 'Value'])
        for row in stock_valuation()['rows']:
            writer.writerow([
                str(row['variant']), row['quantity'],
                row['unit_cost'], row['value'],
            ])

    elif report == 'margins':
        writer.writerow(['Product', 'Landed cost', 'Selling price',
                         'Margin', 'Margin %', 'On hand'])
        for row in margin_report():
            writer.writerow([
                str(row['variant']), row['cost'], row['price'],
                row['margin'], row['margin_percent'], row['qty_on_hand'],
            ])

    else:
        raise Http404('Unknown report.')

    return response


# ══════════════════════════════════════════════════════════════════════════════
#  LEDGER INSPECTION
# ══════════════════════════════════════════════════════════════════════════════

@finance_staff_required
def transaction_list(request):
    """GET /manage/finance/transactions/ — everything posted, newest first."""
    qs = Transaction.objects.prefetch_related('lines__account')

    source = request.GET.get('source', '').strip()
    if source:
        qs = qs.filter(source_type=source)

    search = request.GET.get('q', '').strip()
    if search:
        qs = qs.filter(Q(description__icontains=search) | Q(lines__memo__icontains=search)).distinct()

    paginator = Paginator(qs, 40)
    page_obj = paginator.get_page(request.GET.get('page', 1))

    ctx = {
        'page_obj': page_obj,
        'source':   source,
        'search':   search,
        'sources':  Transaction.objects.order_by()
                       .values_list('source_type', flat=True).distinct(),
    }
    return render(request, 'finance/transaction_list.html', ctx)


@finance_staff_required
def transaction_detail(request, pk):
    """GET /manage/finance/transactions/<pk>/ — one entry and both its sides."""
    txn = get_object_or_404(
        Transaction.objects.prefetch_related('lines__account'), pk=pk,
    )
    is_managed = txn.source_type in Transaction.MANAGED_SOURCES
    ctx = {
        'txn':          txn,
        'lines':        txn.lines.all(),
        'reversals':    txn.reversals.all(),
        'reverse_form': ReverseTransactionForm(),
        'is_managed':   is_managed,
        'managed_advice': Transaction.MANAGED_SOURCE_ADVICE.get(txn.source_type, ''),
        'source_url':   _source_url(txn),
    }
    return render(request, 'finance/transaction_detail.html', ctx)


def _source_url(txn):
    """Link back to the document that created a managed ledger entry."""
    if not txn.source_id:
        return None
    routes = {
        Transaction.SOURCE_INVOICE:  'finance:invoice_detail',
        Transaction.SOURCE_PURCHASE: 'finance:purchase_detail',
        Transaction.SOURCE_LOAN:     'finance:loan_detail',
    }
    route = routes.get(txn.source_type)
    if route:
        return reverse(route, kwargs={'pk': txn.source_id})
    if txn.source_type == Transaction.SOURCE_PAYMENT:
        return reverse('finance:payment_list')
    if txn.source_type in (Transaction.SOURCE_INVESTOR, Transaction.SOURCE_DISTRIBUTION):
        return reverse('finance:investor_list')
    if txn.source_type == Transaction.SOURCE_STOCK:
        return reverse('finance:stock_movements')
    return None


@finance_staff_required
def transaction_reverse(request, pk):
    """
    POST /manage/finance/transactions/<pk>/reverse/
    Undo an entry by posting its mirror image. The original stays visible.
    """
    txn = get_object_or_404(Transaction, pk=pk)

    if request.method != 'POST':
        return redirect('finance:transaction_detail', pk=txn.pk)

    form = ReverseTransactionForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Please give a reason for the reversal.')
        return redirect('finance:transaction_detail', pk=txn.pk)

    try:
        reversal = reverse_transaction(
            txn,
            reason=form.cleaned_data['reason'],
            created_by=request.user,
        )
    except LedgerError as exc:
        messages.error(request, str(exc))
        return redirect('finance:transaction_detail', pk=txn.pk)

    messages.success(
        request,
        f'{txn.reference_no} reversed by {reversal.reference_no}. '
        f'Both entries stay in the ledger.',
    )
    return redirect('finance:transaction_detail', pk=reversal.pk)


@finance_staff_required
def audit_log(request):
    """
    GET /manage/finance/audit/
    Who posted what, and who reversed what.

    Reads the ledger rather than a separate audit table — every posting already
    carries `created_by` and `created_at`, so a second log would only be another
    thing that could disagree with the first.
    """
    qs = Transaction.objects.select_related(
        'created_by', 'reversal_of',
    ).prefetch_related('lines__account')

    user_id = request.GET.get('user', '')
    if user_id.isdigit():
        qs = qs.filter(created_by_id=int(user_id))

    source = request.GET.get('source', '').strip()
    if source:
        qs = qs.filter(source_type=source)

    only = request.GET.get('only', '').strip()
    if only == 'reversals':
        qs = qs.filter(reversal_of__isnull=False)
    elif only == 'reversed':
        qs = qs.filter(is_reversed=True)
    elif only == 'unattributed':
        qs = qs.filter(created_by__isnull=True)

    paginator = Paginator(qs.order_by('-created_at', '-id'), 50)
    page_obj = paginator.get_page(request.GET.get('page', 1))

    by_user = (
        Transaction.objects.values('created_by__username')
        .annotate(count=Count('id')).order_by('-count')
    )

    ctx = {
        'page_obj':    page_obj,
        'user_id':     user_id,
        'source':      source,
        'only':        only,
        'by_user':     by_user,
        'users':       User.objects.filter(is_staff=True).order_by('username'),
        'sources':     Transaction.objects.order_by()
                          .values_list('source_type', flat=True).distinct(),
        'reversal_count': Transaction.objects.filter(is_reversed=True).count(),
        'total_count': Transaction.objects.count(),
    }
    return render(request, 'finance/audit_log.html', ctx)


@finance_staff_required
def integrations_view(request):
    """
    GET/POST /manage/finance/integrations/
    Shows what the affiliate and store hooks are doing, and offers a backfill
    for activity that predates the finance module.
    """
    from affiliate.models import Commission, WithdrawalRequest

    summary = None
    if request.method == 'POST':
        dry_run = request.POST.get('action') != 'apply'
        summary = backfill_affiliate_history(
            dry_run=dry_run, created_by=request.user,
        )
        if not dry_run:
            posted = summary['commissions_posted'] + summary['withdrawals_posted']
            if posted:
                messages.success(request, f'{posted} entr{"y" if posted == 1 else "ies"} posted.')
            else:
                messages.info(request, 'Nothing needed posting — already up to date.')
            for error in summary['errors']:
                messages.warning(request, error)

    ctx = {
        'summary': summary,
        'post_affiliate': getattr(settings, 'FINANCE_POST_AFFILIATE', True),
        'auto_invoice': getattr(settings, 'FINANCE_AUTO_INVOICE_ON_DELIVERY', False),
        'required_group': getattr(settings, 'FINANCE_REQUIRED_GROUP', ''),
        'commission_owed': affiliate_commission_owed(),
        'approved_commissions': Commission.objects.filter(
            status__in=[Commission.STATUS_APPROVED, Commission.STATUS_PAID],
        ).count(),
        'paid_withdrawals': WithdrawalRequest.objects.filter(
            status=WithdrawalRequest.STATUS_PAID,
        ).count(),
        'posted_affiliate_entries': Transaction.objects.filter(
            source_type=Transaction.SOURCE_AFFILIATE, is_reversed=False,
        ).count(),
    }
    return render(request, 'finance/integrations.html', ctx)


@finance_staff_required
def trial_balance_view(request):
    """
    GET /manage/finance/trial-balance/
    The consistency proof. `total_signed` must be exactly zero — if it is not,
    something bypassed post_transaction() and the ledger needs investigating.
    """
    as_of = _parse_date(request.GET.get('as_of'))
    ctx = {'trial': trial_balance(as_of=as_of), 'as_of': as_of}
    return render(request, 'finance/trial_balance.html', ctx)
