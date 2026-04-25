"""
affiliate/fraud_views.py
------------------------
M7 — Fraud Management UI (staff only)

/affiliate/admin/fraud/                  — dashboard + flag list
/affiliate/admin/fraud/<id>/resolve/     — resolve a flag
/affiliate/admin/affiliates/<id>/flag/   — manually flag an affiliate
/affiliate/admin/affiliates/<id>/clear/  — clear fraud flag
"""

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.core.paginator import Paginator
from django.db.models import Q

from .models import AffiliateProfile, FraudFlag, Commission
from .fraud_service import (
    get_fraud_stats,
    manually_flag_affiliate,
    resolve_fraud_flag,
    clear_affiliate_fraud_flag,
)


@staff_member_required
def fraud_dashboard(request):
    """
    GET /affiliate/admin/fraud/
    Overview stats + filterable flag list.
    """
    stats = get_fraud_stats()

    reason_filter = request.GET.get('reason', '')
    search        = request.GET.get('q', '').strip()

    qs = FraudFlag.objects.select_related('affiliate', 'resolved_by').order_by('-flagged_at')

    if reason_filter:
        qs = qs.filter(reason=reason_filter)
    if search:
        qs = qs.filter(
            Q(affiliate__referral_code__icontains=search) |
            Q(affiliate__full_name__icontains=search) |
            Q(details__icontains=search)
        )

    show_resolved = request.GET.get('resolved', '') == '1'
    if not show_resolved:
        qs = qs.filter(is_resolved=False)

    paginator  = Paginator(qs, 25)
    page_obj   = paginator.get_page(request.GET.get('page', 1))

    ctx = {
        'stats':          stats,
        'page_obj':       page_obj,
        'reason_filter':  reason_filter,
        'search':         search,
        'show_resolved':  show_resolved,
        'reason_choices': FraudFlag.REASON_CHOICES,
    }
    return render(request, 'affiliate/fraud_dashboard.html', ctx)


@staff_member_required
@require_POST
def resolve_flag(request, flag_id):
    """POST /affiliate/admin/fraud/<id>/resolve/"""
    flag = get_object_or_404(FraudFlag, pk=flag_id)
    if not flag.is_resolved:
        resolve_fraud_flag(flag, request.user)
        messages.success(request, f"Flag #{flag.pk} resolved.")
    return redirect('affiliate:fraud_dashboard')


@staff_member_required
@require_POST
def flag_affiliate(request, affiliate_id):
    """POST /affiliate/admin/affiliates/<id>/flag/"""
    affiliate = get_object_or_404(AffiliateProfile, pk=affiliate_id)
    details   = request.POST.get('details', 'Manually flagged by admin.').strip()
    manually_flag_affiliate(affiliate, FraudFlag.REASON_MANUAL, details, request.user)
    messages.warning(request, f"Affiliate {affiliate.referral_code} has been flagged.")
    return redirect('affiliate:fraud_dashboard')


@staff_member_required
@require_POST
def clear_affiliate(request, affiliate_id):
    """POST /affiliate/admin/affiliates/<id>/clear/"""
    affiliate = get_object_or_404(AffiliateProfile, pk=affiliate_id)
    clear_affiliate_fraud_flag(affiliate, request.user)
    messages.success(request, f"Fraud flag cleared for {affiliate.referral_code}.")
    return redirect('affiliate:fraud_dashboard')
