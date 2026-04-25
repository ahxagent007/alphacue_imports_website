from django.urls import path
from . import views

app_name = 'affiliate'

urlpatterns = [
    # M2 — Referral tracking
    path('ref/<str:code>/', views.referral_redirect, name='referral_redirect'),
    path('affiliate/referral-status/', views.referral_status, name='referral_status'),

    # M3 — Registration & status
    path('affiliate/apply/', views.affiliate_apply, name='affiliate_apply'),
    path('affiliate/application-status/', views.application_status, name='application_status'),

    # M4 — Commission engine
    path('affiliate/order-delivered/', views.order_delivered_webhook, name='order_delivered_webhook'),
    path('affiliate/commission-check/<int:order_id>/', views.commission_check, name='commission_check'),
]
