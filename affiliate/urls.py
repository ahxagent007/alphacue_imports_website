from django.urls import path
from . import views, dashboard_views, withdrawal_views, fraud_views, analytics_views, registration_views

app_name = 'affiliate'

urlpatterns = [
    # M2 — Referral tracking
    path('ref/<str:code>/', views.referral_redirect, name='referral_redirect'),
    path('affiliate/referral-status/', views.referral_status, name='referral_status'),

    # Registration
    path('accounts/register/', registration_views.register, name='register'),

    # M3 — Registration & status
    path('affiliate/apply/', views.affiliate_apply, name='affiliate_apply'),
    path('affiliate/application-status/', views.application_status, name='application_status'),

    # M4 — Commission engine
    path('affiliate/order-delivered/', views.order_delivered_webhook, name='order_delivered_webhook'),
    path('affiliate/commission-check/<int:order_id>/', views.commission_check, name='commission_check'),

    # M5 — Dashboard
    path('affiliate/dashboard/', dashboard_views.dashboard, name='dashboard'),
    path('affiliate/commissions/', dashboard_views.commission_history, name='commission_history'),
    path('affiliate/clicks/', dashboard_views.click_history, name='click_history'),

    # M6 — Withdrawal system
    path('affiliate/withdraw/', withdrawal_views.withdrawal_request, name='withdrawal_request'),
    path('affiliate/withdrawals/', withdrawal_views.withdrawal_history, name='withdrawal_history'),

    # M7 — Fraud management (staff only)
    path('affiliate/admin/fraud/', fraud_views.fraud_dashboard, name='fraud_dashboard'),
    path('affiliate/admin/fraud/<int:flag_id>/resolve/', fraud_views.resolve_flag, name='resolve_flag'),
    path('affiliate/admin/affiliates/<int:affiliate_id>/flag/', fraud_views.flag_affiliate, name='flag_affiliate'),
    path('affiliate/admin/affiliates/<int:affiliate_id>/clear/', fraud_views.clear_affiliate, name='clear_affiliate'),

    # M8 — Admin analytics (staff only)
    path('affiliate/admin/analytics/', analytics_views.analytics_dashboard, name='analytics_dashboard'),
]