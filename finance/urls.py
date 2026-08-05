from django.urls import path

from . import views

app_name = 'finance'

urlpatterns = [
    path('manage/finance/', views.dashboard, name='dashboard'),

    # ── Accounts ──────────────────────────────────────────────────────────
    path('manage/finance/accounts/', views.account_list, name='account_list'),
    path('manage/finance/accounts/new/', views.account_create, name='account_create'),
    path('manage/finance/accounts/<int:pk>/', views.account_detail, name='account_detail'),
    path('manage/finance/accounts/<int:pk>/edit/', views.account_edit, name='account_edit'),
    path('manage/finance/accounts/<int:pk>/opening-balance/',
         views.opening_balance, name='opening_balance'),

    # ── Money movements ───────────────────────────────────────────────────
    path('manage/finance/expenses/', views.expense_list, name='expense_list'),
    path('manage/finance/expenses/new/', views.expense_create, name='expense_create'),
    path('manage/finance/transfers/new/', views.transfer_create, name='transfer_create'),
    path('manage/finance/daybook/', views.daybook_view, name='daybook'),

    # ── Parties ───────────────────────────────────────────────────────────
    path('manage/finance/parties/', views.party_list, name='party_list'),
    path('manage/finance/parties/new/', views.party_create, name='party_create'),
    path('manage/finance/parties/<int:pk>/', views.party_detail, name='party_detail'),
    path('manage/finance/parties/<int:pk>/edit/', views.party_edit, name='party_edit'),

    # ── Lookups ───────────────────────────────────────────────────────────
    path('manage/finance/api/products/', views.product_search, name='product_search'),

    # ── Invoices ──────────────────────────────────────────────────────────
    path('manage/finance/invoices/', views.invoice_list, name='invoice_list'),
    path('manage/finance/invoices/new/', views.invoice_create, name='invoice_create'),
    path('manage/finance/invoices/walk-in/', views.invoice_walkin, name='invoice_walkin'),
    path('manage/finance/invoices/from-order/<str:order_number>/',
         views.invoice_from_order, name='invoice_from_order'),
    path('manage/finance/invoices/<int:pk>/', views.invoice_detail, name='invoice_detail'),
    path('manage/finance/invoices/<int:pk>/edit/', views.invoice_edit, name='invoice_edit'),
    path('manage/finance/invoices/<int:pk>/issue/', views.invoice_issue, name='invoice_issue'),
    path('manage/finance/invoices/<int:pk>/cancel/', views.invoice_cancel, name='invoice_cancel'),
    path('manage/finance/invoices/<int:pk>/print/', views.invoice_print, name='invoice_print'),
    path('manage/finance/invoices/<int:pk>/share/', views.invoice_share, name='invoice_share'),

    # Public — no login. Only works for issued invoices, via an unguessable token.
    path('invoice/<str:token>/', views.invoice_public, name='invoice_public'),

    # ── Payments & dues ───────────────────────────────────────────────────
    path('manage/finance/dues/', views.dues_dashboard, name='dues_dashboard'),
    path('manage/finance/dues/ageing/', views.ageing_view, name='ageing'),
    path('manage/finance/payments/', views.payment_list, name='payment_list'),
    path('manage/finance/payments/new/', views.payment_create, name='payment_create'),
    path('manage/finance/payments/<int:pk>/reverse/', views.payment_reverse, name='payment_reverse'),
    path('manage/finance/payments/supplier/', views.supplier_payment_create,
         name='supplier_payment_create'),
    path('manage/finance/parties/<int:pk>/statement/', views.party_statement_view,
         name='party_statement'),

    # ── Purchases ─────────────────────────────────────────────────────────
    path('manage/finance/purchases/', views.purchase_list, name='purchase_list'),
    path('manage/finance/purchases/new/', views.purchase_create, name='purchase_create'),
    path('manage/finance/purchases/<int:pk>/', views.purchase_detail, name='purchase_detail'),
    path('manage/finance/purchases/<int:pk>/edit/', views.purchase_edit, name='purchase_edit'),
    path('manage/finance/purchases/<int:pk>/order/', views.purchase_order, name='purchase_order'),
    path('manage/finance/purchases/<int:pk>/receive/', views.purchase_receive, name='purchase_receive'),
    path('manage/finance/purchases/<int:pk>/cancel/', views.purchase_cancel, name='purchase_cancel'),
    path('manage/finance/margins/', views.margin_view, name='margins'),

    # ── Catalogue ─────────────────────────────────────────────────────────
    path('manage/finance/products/', views.product_list, name='product_list'),
    path('manage/finance/products/new/', views.product_create, name='product_create'),
    path('manage/finance/products/<int:pk>/edit/', views.product_edit, name='product_edit'),
    path('manage/finance/products/categories/new/', views.category_create, name='category_create'),

    # ── Stock ─────────────────────────────────────────────────────────────
    path('manage/finance/stock/', views.stock_list, name='stock_list'),
    path('manage/finance/stock/<int:variant_id>/', views.stock_detail, name='stock_detail'),
    path('manage/finance/stock/adjust/', views.stock_adjust, name='stock_adjust'),
    path('manage/finance/stock/opening/', views.stock_opening, name='stock_opening'),
    path('manage/finance/stock/movements/', views.stock_movement_list, name='stock_movements'),

    # ── Investors ─────────────────────────────────────────────────────────
    path('manage/finance/investors/', views.investor_list, name='investor_list'),
    path('manage/finance/investors/new/', views.investor_create, name='investor_create'),
    path('manage/finance/investors/<int:pk>/', views.investor_detail, name='investor_detail'),
    path('manage/finance/investors/<int:pk>/edit/', views.investor_edit, name='investor_edit'),
    path('manage/finance/investors/<int:pk>/capital/', views.investor_capital, name='investor_capital'),
    path('manage/finance/investors/distribute/', views.profit_distribute, name='profit_distribute'),

    # ── Loans ─────────────────────────────────────────────────────────────
    path('manage/finance/loans/', views.loan_list, name='loan_list'),
    path('manage/finance/loans/new/', views.loan_create, name='loan_create'),
    path('manage/finance/loans/<int:pk>/', views.loan_detail, name='loan_detail'),
    path('manage/finance/loans/<int:pk>/pay/', views.loan_pay, name='loan_pay'),

    # ── Undo / delete ─────────────────────────────────────────────────────
    path('manage/finance/investors/<int:pk>/undo/<int:txn_id>/',
         views.investor_movement_undo, name='investor_movement_undo'),
    path('manage/finance/investors/<int:pk>/delete/',
         views.investor_delete, name='investor_delete'),
    path('manage/finance/investors/distributions/<int:pk>/undo/',
         views.distribution_undo, name='distribution_undo'),
    path('manage/finance/loans/<int:pk>/cancel/', views.loan_cancel, name='loan_cancel'),
    path('manage/finance/loans/<int:pk>/payments/<int:payment_id>/undo/',
         views.loan_payment_undo, name='loan_payment_undo'),
    path('manage/finance/stock/movements/<int:pk>/undo/',
         views.stock_movement_undo, name='stock_movement_undo'),
    path('manage/finance/parties/<int:pk>/delete/', views.party_delete, name='party_delete'),
    path('manage/finance/invoices/<int:pk>/delete/', views.invoice_delete, name='invoice_delete'),
    path('manage/finance/purchases/<int:pk>/delete/', views.purchase_delete, name='purchase_delete'),

    # ── Reports ───────────────────────────────────────────────────────────
    path('manage/finance/reports/profit-loss/', views.profit_loss, name='profit_loss'),
    path('manage/finance/reports/cash-flow/', views.cash_flow, name='cash_flow'),
    path('manage/finance/reports/export/<str:report>/', views.export_csv, name='export_csv'),

    # ── Ledger inspection ─────────────────────────────────────────────────
    path('manage/finance/transactions/', views.transaction_list, name='transaction_list'),
    path('manage/finance/transactions/<int:pk>/', views.transaction_detail, name='transaction_detail'),
    path('manage/finance/transactions/<int:pk>/reverse/', views.transaction_reverse, name='transaction_reverse'),
    path('manage/finance/trial-balance/', views.trial_balance_view, name='trial_balance'),
    path('manage/finance/audit/', views.audit_log, name='audit_log'),
    path('manage/finance/integrations/', views.integrations_view, name='integrations'),
]
