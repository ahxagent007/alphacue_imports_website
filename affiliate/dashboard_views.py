"""
affiliate/dashboard_views.py
-----------------------------
M5 — Affiliate Dashboard

All views require login + approved affiliate status.
"""

from decimal import Decimal
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.db.models import Sum, Count, Q
from django.utils import timezone
from datetime import timedelta

from .models import AffiliateProfile, Commission, ReferralClick, WithdrawalRequest


def _require_approved_affiliate(request):
    """
    Returns (affiliate, None) if OK.
    Returns (None, redirect_response) if not approved.
    """
    if not request.user.is_authenticated:
        from django.contrib.auth.views import redirect_to_login
        return None, redirect_to_login(request.get_full_path())

    try:
        affiliate = request.user.affiliate_profile
    except AffiliateProfile.DoesNotExist:
        return None, redirect('affiliate:affiliate_apply')

    if affiliate.status == AffiliateProfile.STATUS_PENDING:
        return None, redirect('affiliate:application_status')

    if affiliate.status in (AffiliateProfile.STATUS_REJECTED, AffiliateProfile.STATUS_SUSPENDED):
        return None, redirect('affiliate:application_status')

    return affiliate, None


# ─── Main Dashboard ───────────────────────────────────────────────────────────

@login_required
def dashboard(request):
    """
    /affiliate/dashboard/
    Main overview: stats, earnings, recent activity, referral link.
    """
    affiliate, err = _require_approved_affiliate(request)
    if err:
        return err

    now   = timezone.now()
    day30 = now - timedelta(days=30)
    day7  = now - timedelta(days=7)

    # ── Click stats ───────────────────────────────────────────────────────
    total_clicks   = affiliate.clicks.count()
    clicks_30d     = affiliate.clicks.filter(clicked_at__gte=day30).count()
    clicks_7d      = affiliate.clicks.filter(clicked_at__gte=day7).count()

    # ── Commission stats ──────────────────────────────────────────────────
    commissions    = affiliate.commissions.all()
    total_orders   = commissions.filter(
        is_fraud_suspected=False
    ).count()

    earned_total   = commissions.filter(
        status__in=[Commission.STATUS_APPROVED, Commission.STATUS_PAID],
        is_fraud_suspected=False
    ).aggregate(t=Sum('commission_amount'))['t'] or Decimal('0.00')

    earned_30d     = commissions.filter(
        created_at__gte=day30,
        is_fraud_suspected=False
    ).aggregate(t=Sum('commission_amount'))['t'] or Decimal('0.00')

    # ── Conversion rate ───────────────────────────────────────────────────
    conversion_rate = 0
    if clicks_30d > 0 and total_orders > 0:
        orders_30d = commissions.filter(
            created_at__gte=day30, is_fraud_suspected=False
        ).count()
        conversion_rate = round((orders_30d / clicks_30d) * 100, 1)

    # ── Recent commissions (last 10) ──────────────────────────────────────
    recent_commissions = commissions.filter(
        is_fraud_suspected=False
    ).order_by('-created_at')[:10]

    # ── Click chart data — last 14 days ───────────────────────────────────
    chart_labels = []
    chart_clicks = []
    for i in range(13, -1, -1):
        day = (now - timedelta(days=i)).date()
        cnt = affiliate.clicks.filter(clicked_at__date=day).count()
        chart_labels.append(day.strftime('%d %b'))
        chart_clicks.append(cnt)

    # ── Withdrawal info ───────────────────────────────────────────────────
    pending_withdrawal = affiliate.withdrawal_requests.filter(
        status=WithdrawalRequest.STATUS_PENDING
    ).aggregate(t=Sum('amount'))['t'] or Decimal('0.00')

    # Featured products for share links
    try:
        from store.models import Product, ProductImage
        from django.db.models import Prefetch
        share_products = (
            Product.objects
            .filter(is_active=True)
            .prefetch_related(
                Prefetch('images', queryset=ProductImage.objects.filter(is_primary=True))
            )
            .order_by('-is_featured', '-created_at')[:12]
        )
    except Exception:
        share_products = []

    base_url = request.build_absolute_uri('/').rstrip('/')

    ctx = {
        'affiliate':         affiliate,
        'total_clicks':      total_clicks,
        'clicks_30d':        clicks_30d,
        'clicks_7d':         clicks_7d,
        'total_orders':      total_orders,
        'conversion_rate':   conversion_rate,
        'earned_total':      earned_total,
        'earned_30d':        earned_30d,
        'balance_pending':   affiliate.balance_pending,
        'balance_approved':  affiliate.balance_approved,
        'withdrawal_balance': affiliate.withdrawal_balance,
        'pending_withdrawal': pending_withdrawal,
        'recent_commissions': recent_commissions,
        'chart_labels':      chart_labels,
        'chart_clicks':      chart_clicks,
        'referral_url':      f"{base_url}/ref/{affiliate.referral_code}/",
        'share_products':    share_products,
        'base_url':          base_url,
    }
    return render(request, 'affiliate/dashboard.html', ctx)


# ─── Commission History ───────────────────────────────────────────────────────

@login_required
def commission_history(request):
    """
    /affiliate/commissions/
    Full paginated commission history with status filter.
    """
    affiliate, err = _require_approved_affiliate(request)
    if err:
        return err

    from django.core.paginator import Paginator

    status_filter = request.GET.get('status', '')
    qs = affiliate.commissions.filter(is_fraud_suspected=False).order_by('-created_at')
    if status_filter:
        qs = qs.filter(status=status_filter)

    paginator  = Paginator(qs, 20)
    page_obj   = paginator.get_page(request.GET.get('page', 1))

    # Summary totals
    summary = affiliate.commissions.filter(is_fraud_suspected=False).aggregate(
        total_pending=Sum('commission_amount', filter=Q(status=Commission.STATUS_PENDING)),
        total_approved=Sum('commission_amount', filter=Q(status=Commission.STATUS_APPROVED)),
        total_paid=Sum('commission_amount', filter=Q(status=Commission.STATUS_PAID)),
    )

    ctx = {
        'affiliate':      affiliate,
        'page_obj':       page_obj,
        'status_filter':  status_filter,
        'status_choices': Commission.STATUS_CHOICES,
        'summary':        summary,
    }
    return render(request, 'affiliate/commission_history.html', ctx)


# ─── Click History ────────────────────────────────────────────────────────────

@login_required
def click_history(request):
    """
    /affiliate/clicks/
    Paginated click log.
    """
    affiliate, err = _require_approved_affiliate(request)
    if err:
        return err

    from django.core.paginator import Paginator

    qs       = affiliate.clicks.order_by('-clicked_at')
    paginator = Paginator(qs, 25)
    page_obj  = paginator.get_page(request.GET.get('page', 1))

    ctx = {
        'affiliate': affiliate,
        'page_obj':  page_obj,
        'total_clicks': affiliate.clicks.count(),
    }
    return render(request, 'affiliate/click_history.html', ctx)


# ─── M6 Placeholders (replaced in M6) ────────────────────────────────────────

@login_required
def withdrawal_request(request):
    affiliate, err = _require_approved_affiliate(request)
    if err:
        return err
    return render(request, 'affiliate/withdrawal_placeholder.html', {'affiliate': affiliate})


@login_required
def withdrawal_history(request):
    affiliate, err = _require_approved_affiliate(request)
    if err:
        return err
    return render(request, 'affiliate/withdrawal_placeholder.html', {'affiliate': affiliate})