"""
affiliate/fraud_service.py
--------------------------
M7 — Anti-Fraud System

Additional fraud detection logic beyond the basic checks in services.py.
Also provides utility functions for the fraud management UI.

Checks added in M7:
  - Rapid order pattern: multiple orders in short time from same affiliate
  - New account abuse: orders placed too soon after affiliate approval
  - Velocity check: unusually high commission amount relative to history
"""

import logging
from decimal import Decimal
from django.utils import timezone
from django.db.models import Count, Sum, Avg

from .models import AffiliateProfile, Commission, FraudFlag, ReferralClick

logger = logging.getLogger(__name__)

# ── Thresholds (can be moved to CommissionSetting later) ──────────────────────
RAPID_ORDER_COUNT    = 10      # orders in RAPID_ORDER_HOURS triggers flag
RAPID_ORDER_HOURS    = 1
NEW_ACCOUNT_DAYS     = 0       # 0 = disabled (new account check disabled)
HIGH_VALUE_MULTIPLIER = 10     # flag if commission > 10x affiliate's average


# ─── Additional Pattern Checks ────────────────────────────────────────────────

def check_rapid_orders(affiliate, order_id):
    """
    Flag if affiliate receives too many orders in a short window.
    Indicates coordinated fake purchases.
    """
    cutoff = timezone.now() - timezone.timedelta(hours=RAPID_ORDER_HOURS)
    recent_count = Commission.objects.filter(
        affiliate=affiliate,
        created_at__gte=cutoff,
        is_fraud_suspected=False,
    ).count()

    if recent_count >= RAPID_ORDER_COUNT:
        return (
            True,
            f"{recent_count} commissions in the last {RAPID_ORDER_HOURS}h — "
            f"possible coordinated abuse"
        )
    return False, ""


def check_new_account_order(affiliate, order_id):
    """
    Flag if a large order comes in within days of affiliate approval.
    Set NEW_ACCOUNT_DAYS = 0 to disable this check entirely.
    """
    if NEW_ACCOUNT_DAYS == 0:
        return False, ""

    if not affiliate.approved_at:
        return False, ""

    days_since_approval = (timezone.now() - affiliate.approved_at).days
    if days_since_approval < NEW_ACCOUNT_DAYS:
        return (
            True,
            f"Order placed only {days_since_approval} day(s) after affiliate approval"
        )
    return False, ""


def check_high_value_commission(affiliate, commission_amount):
    """
    Flag if this commission is much higher than the affiliate's historical average.
    Skipped if the affiliate has fewer than 3 approved/paid commissions (no reliable baseline).
    """
    history = Commission.objects.filter(
        affiliate=affiliate,
        is_fraud_suspected=False,
        status__in=[Commission.STATUS_APPROVED, Commission.STATUS_PAID],
    )

    # Need at least 3 data points for a meaningful average
    if history.count() < 3:
        return False, ""

    avg = history.aggregate(a=Avg('commission_amount'))['a']

    if avg and commission_amount > (avg * HIGH_VALUE_MULTIPLIER):
        return (
            True,
            f"Commission ৳{commission_amount:.0f} is {commission_amount/avg:.1f}x "
            f"above affiliate's average of ৳{avg:.0f}"
        )
    return False, ""


# ─── Run All M7 Checks ────────────────────────────────────────────────────────

def run_extended_fraud_checks(affiliate, order_id, commission_amount):
    """
    Run M7 pattern checks on top of the basic M4 checks.
    Called from services.py after basic checks pass.
    Returns (is_fraud, reason).
    """
    checks = [
        (FraudFlag.REASON_SUSPICIOUS_PATTERN, check_rapid_orders(affiliate, order_id)),
        (FraudFlag.REASON_SUSPICIOUS_PATTERN, check_new_account_order(affiliate, order_id)),
        (FraudFlag.REASON_SUSPICIOUS_PATTERN, check_high_value_commission(affiliate, commission_amount)),
    ]

    for reason_code, (is_fraud, detail) in checks:
        if is_fraud:
            FraudFlag.objects.get_or_create(
                affiliate=affiliate,
                reason=reason_code,
                order_id=order_id,
                defaults={'details': detail},
            )
            logger.warning(
                "Extended fraud check triggered for affiliate %s, order #%s: %s",
                affiliate.referral_code, order_id, detail
            )
            return True, detail

    return False, ""


# ─── Manual Flag / Unflag ─────────────────────────────────────────────────────

def manually_flag_affiliate(affiliate, reason, details, flagged_by_user):
    """Create a manual FraudFlag and mark affiliate as flagged."""
    FraudFlag.objects.create(
        affiliate=affiliate,
        reason=FraudFlag.REASON_MANUAL,
        details=f"[Manual by {flagged_by_user.username}] {details}",
        is_resolved=False,
    )
    AffiliateProfile.objects.filter(pk=affiliate.pk).update(is_fraud_flagged=True)
    logger.warning("Affiliate %s manually flagged by %s", affiliate.referral_code, flagged_by_user)


def resolve_fraud_flag(flag, resolved_by_user):
    """Resolve a single FraudFlag."""
    flag.is_resolved    = True
    flag.resolved_by    = resolved_by_user
    flag.resolved_at    = timezone.now()
    flag.save(update_fields=['is_resolved', 'resolved_by', 'resolved_at'])


def clear_affiliate_fraud_flag(affiliate, cleared_by_user):
    """
    Resolve all open flags and remove is_fraud_flagged from affiliate.
    Called when admin manually clears a flagged affiliate.
    """
    open_flags = affiliate.fraud_flags.filter(is_resolved=False)
    for flag in open_flags:
        resolve_fraud_flag(flag, cleared_by_user)
    AffiliateProfile.objects.filter(pk=affiliate.pk).update(is_fraud_flagged=False)
    logger.info("Affiliate %s fraud flag cleared by %s", affiliate.referral_code, cleared_by_user)


# ─── Dashboard Stats for Admin UI ─────────────────────────────────────────────

def get_fraud_stats():
    """Summary stats for the fraud management dashboard."""
    return {
        'total_flags':       FraudFlag.objects.count(),
        'open_flags':        FraudFlag.objects.filter(is_resolved=False).count(),
        'flagged_affiliates':AffiliateProfile.objects.filter(is_fraud_flagged=True).count(),
        'flags_by_reason':   (
            FraudFlag.objects
            .filter(is_resolved=False)
            .values('reason')
            .annotate(count=Count('id'))
            .order_by('-count')
        ),
        'suspected_commissions': Commission.objects.filter(is_fraud_suspected=True).count(),
    }