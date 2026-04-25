"""
affiliate/analytics_service.py
--------------------------------
M8 — Admin Analytics

All heavy DB queries live here, not in views.
Returns plain dicts/lists — no ORM objects passed to templates directly
so all data is JSON-serialisable for chart rendering.
"""

from decimal import Decimal
from django.db.models import Sum, Count, Avg, F, Q
from django.utils import timezone
from datetime import timedelta

from .models import AffiliateProfile, Commission, ReferralClick, WithdrawalRequest


# ─── Overview KPIs ────────────────────────────────────────────────────────────

def get_overview_stats():
    """Top-level numbers for the analytics header cards."""
    now   = timezone.now()
    day30 = now - timedelta(days=30)
    day7  = now - timedelta(days=7)

    total_affiliates   = AffiliateProfile.objects.filter(status=AffiliateProfile.STATUS_APPROVED).count()
    new_affiliates_30d = AffiliateProfile.objects.filter(
        status=AffiliateProfile.STATUS_APPROVED,
        approved_at__gte=day30,
    ).count()

    comm_qs = Commission.objects.filter(is_fraud_suspected=False)

    total_commissions  = comm_qs.aggregate(t=Sum('commission_amount'))['t'] or Decimal('0.00')
    commissions_30d    = comm_qs.filter(created_at__gte=day30).aggregate(
        t=Sum('commission_amount'))['t'] or Decimal('0.00')

    total_orders       = comm_qs.count()
    orders_30d         = comm_qs.filter(created_at__gte=day30).count()

    total_clicks       = ReferralClick.objects.count()
    clicks_30d         = ReferralClick.objects.filter(clicked_at__gte=day30).count()

    pending_payouts    = AffiliateProfile.objects.aggregate(
        t=Sum('balance_approved'))['t'] or Decimal('0.00')

    total_paid_out     = AffiliateProfile.objects.aggregate(
        t=Sum('total_withdrawn'))['t'] or Decimal('0.00')

    return {
        'total_affiliates':    total_affiliates,
        'new_affiliates_30d':  new_affiliates_30d,
        'total_commissions':   total_commissions,
        'commissions_30d':     commissions_30d,
        'total_orders':        total_orders,
        'orders_30d':          orders_30d,
        'total_clicks':        total_clicks,
        'clicks_30d':          clicks_30d,
        'pending_payouts':     pending_payouts,
        'total_paid_out':      total_paid_out,
    }


# ─── Top Affiliates ───────────────────────────────────────────────────────────

def get_top_affiliates(limit=10, period_days=30):
    """
    Returns top affiliates ranked by commission earned in period.
    Each row: affiliate info + clicks + orders + earnings + conversion_rate
    """
    cutoff = timezone.now() - timedelta(days=period_days)

    # Affiliates with most commission in period
    top = (
        Commission.objects
        .filter(is_fraud_suspected=False, created_at__gte=cutoff)
        .values('affiliate')
        .annotate(
            period_earnings=Sum('commission_amount'),
            period_orders=Count('id'),
        )
        .order_by('-period_earnings')[:limit]
    )

    result = []
    for row in top:
        try:
            aff = AffiliateProfile.objects.get(pk=row['affiliate'])
        except AffiliateProfile.DoesNotExist:
            continue

        clicks = aff.clicks.filter(clicked_at__gte=cutoff).count()
        conversion = round(
            (row['period_orders'] / clicks * 100) if clicks > 0 else 0, 1
        )
        result.append({
            'affiliate':        aff,
            'period_earnings':  row['period_earnings'],
            'period_orders':    row['period_orders'],
            'period_clicks':    clicks,
            'conversion_rate':  conversion,
            'total_earnings':   aff.balance_pending + aff.balance_approved + aff.balance_paid + aff.total_withdrawn,
        })

    return result


# ─── Revenue Contribution ─────────────────────────────────────────────────────

def get_revenue_contribution(period_days=30):
    """
    Total order revenue attributed to affiliates vs direct (no affiliate).
    """
    from store.models import Order

    cutoff   = timezone.now() - timedelta(days=period_days)
    all_orders = Order.objects.filter(
        status=Order.STATUS_DELIVERED,
        created_at__gte=cutoff,
    )

    total_revenue    = all_orders.aggregate(t=Sum('grand_total'))['t'] or Decimal('0.00')
    affiliate_revenue = all_orders.exclude(
        affiliate_code=''
    ).aggregate(t=Sum('grand_total'))['t'] or Decimal('0.00')
    direct_revenue   = total_revenue - affiliate_revenue

    affiliate_pct = round(
        float(affiliate_revenue / total_revenue * 100) if total_revenue > 0 else 0, 1
    )

    return {
        'total_revenue':     total_revenue,
        'affiliate_revenue': affiliate_revenue,
        'direct_revenue':    direct_revenue,
        'affiliate_pct':     affiliate_pct,
        'direct_pct':        round(100 - affiliate_pct, 1),
    }


# ─── Commission Trend (daily for chart) ──────────────────────────────────────

def get_commission_trend(days=30):
    """Daily commission totals for the past N days — used for line chart."""
    now    = timezone.now()
    labels = []
    values = []

    for i in range(days - 1, -1, -1):
        day = (now - timedelta(days=i)).date()
        total = Commission.objects.filter(
            is_fraud_suspected=False,
            created_at__date=day,
        ).aggregate(t=Sum('commission_amount'))['t'] or Decimal('0.00')
        labels.append(day.strftime('%d %b'))
        values.append(float(total))

    return {'labels': labels, 'values': values}


# ─── Click Trend ──────────────────────────────────────────────────────────────

def get_click_trend(days=30):
    """Daily click counts for the past N days."""
    now    = timezone.now()
    labels = []
    values = []

    for i in range(days - 1, -1, -1):
        day = (now - timedelta(days=i)).date()
        cnt = ReferralClick.objects.filter(clicked_at__date=day).count()
        labels.append(day.strftime('%d %b'))
        values.append(cnt)

    return {'labels': labels, 'values': values}


# ─── Commission Status Breakdown ─────────────────────────────────────────────

def get_commission_status_breakdown():
    """Counts and totals by status for doughnut chart."""
    qs = Commission.objects.filter(is_fraud_suspected=False)
    result = []
    for status, label in Commission.STATUS_CHOICES:
        agg = qs.filter(status=status).aggregate(
            count=Count('id'),
            total=Sum('commission_amount'),
        )
        result.append({
            'status': status,
            'label':  label,
            'count':  agg['count'] or 0,
            'total':  float(agg['total'] or 0),
        })
    return result


# ─── Pending Withdrawals ──────────────────────────────────────────────────────

def get_pending_withdrawals():
    """List of pending withdrawal requests for admin attention."""
    return (
        WithdrawalRequest.objects
        .filter(status=WithdrawalRequest.STATUS_PENDING)
        .select_related('affiliate')
        .order_by('requested_at')
    )
