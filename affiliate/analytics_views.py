"""
affiliate/analytics_views.py
-----------------------------
M8 — Admin Analytics Views (staff only)

/affiliate/admin/analytics/   — main analytics dashboard
"""

import json
from django.shortcuts import render
from django.contrib.admin.views.decorators import staff_member_required

from .analytics_service import (
    get_overview_stats,
    get_top_affiliates,
    get_revenue_contribution,
    get_commission_trend,
    get_click_trend,
    get_commission_status_breakdown,
    get_pending_withdrawals,
)


@staff_member_required
def analytics_dashboard(request):
    """
    GET /affiliate/admin/analytics/
    Full analytics overview for admins.
    """
    period = int(request.GET.get('period', 30))
    if period not in (7, 30, 90):
        period = 30

    stats          = get_overview_stats()
    top_affiliates = get_top_affiliates(limit=10, period_days=period)
    revenue        = get_revenue_contribution(period_days=period)
    comm_trend     = get_commission_trend(days=period)
    click_trend    = get_click_trend(days=period)
    status_breakdown = get_commission_status_breakdown()
    pending_withdrawals = get_pending_withdrawals()

    ctx = {
        'period':             period,
        'stats':              stats,
        'top_affiliates':     top_affiliates,
        'revenue':            revenue,
        'pending_withdrawals': pending_withdrawals,

        # JSON for charts
        'comm_trend_json':    json.dumps(comm_trend),
        'click_trend_json':   json.dumps(click_trend),
        'status_breakdown_json': json.dumps(status_breakdown),
    }
    return render(request, 'affiliate/analytics_dashboard.html', ctx)
