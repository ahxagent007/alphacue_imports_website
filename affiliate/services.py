"""
affiliate/services.py
---------------------
Commission Engine — all business logic lives here, NOT in views or signals.

Public API:
    process_order_commission(order_id, order_total, affiliate_code,
                             buyer_user=None, referral_click=None)
        → Called when an order is first placed to store affiliate attribution.

    trigger_commission_on_delivery(order_id, order_total,
                                   affiliate_code=None, buyer_user=None)
        → Called when order.status changes to 'delivered'.
          Creates Commission, updates affiliate balances, runs fraud checks.

    get_commission_for_order(order_id)
        → Returns Commission or None.
"""

import logging
from decimal import Decimal

from django.db import transaction, IntegrityError
from django.utils import timezone

from .models import (
    AffiliateProfile,
    Commission,
    CommissionSetting,
    FraudFlag,
    ReferralClick,
)

logger = logging.getLogger(__name__)

# How many orders from the same IP within this many days triggers fraud flag
FRAUD_IP_ORDER_THRESHOLD = 3
FRAUD_IP_WINDOW_DAYS = 7


def _get_client_ip_from_click(referral_click):
    if referral_click:
        return referral_click.ip_address
    return None


# ─── Fraud Checks ────────────────────────────────────────────────────────────

def _check_self_referral(affiliate, buyer_user):
    """Return True (fraud) if buyer is the affiliate themselves."""
    if buyer_user and affiliate.user == buyer_user:
        return True, "Buyer is the affiliate (self-referral)"
    return False, ""


def _check_duplicate_commission(affiliate, order_id):
    """Return True (fraud) if commission already exists for this order."""
    if Commission.objects.filter(affiliate=affiliate, order_id=order_id).exists():
        return True, f"Commission already exists for order #{order_id}"
    return False, ""


def _check_ip_abuse(affiliate, buyer_ip):
    """
    Flag if the same IP has generated too many commissions for this
    affiliate within the fraud window — indicates click farming.
    """
    if not buyer_ip:
        return False, ""

    cutoff = timezone.now() - timezone.timedelta(days=FRAUD_IP_WINDOW_DAYS)
    count = Commission.objects.filter(
        affiliate=affiliate,
        referral_click__ip_address=buyer_ip,
        created_at__gte=cutoff,
    ).count()

    if count >= FRAUD_IP_ORDER_THRESHOLD:
        return True, (
            f"IP {buyer_ip} generated {count} commissions for this affiliate "
            f"in the last {FRAUD_IP_WINDOW_DAYS} days"
        )
    return False, ""


def _run_fraud_checks(affiliate, order_id, buyer_user, buyer_ip):
    """
    Run all fraud checks. Returns (is_fraud, reason).
    Records a FraudFlag row if fraud is detected.
    """
    checks = [
        (FraudFlag.REASON_SELF_REFERRAL,        _check_self_referral(affiliate, buyer_user)),
        (FraudFlag.REASON_DUPLICATE_COMMISSION,  _check_duplicate_commission(affiliate, order_id)),
        (FraudFlag.REASON_IP_ABUSE,              _check_ip_abuse(affiliate, buyer_ip)),
    ]

    for reason_code, (is_fraud, detail) in checks:
        if is_fraud:
            FraudFlag.objects.create(
                affiliate=affiliate,
                reason=reason_code,
                details=detail,
                order_id=order_id,
                ip_address=buyer_ip,
            )
            logger.warning(
                "Fraud detected for affiliate %s, order #%s: %s",
                affiliate.referral_code, order_id, detail
            )
            return True, detail

    return False, ""


# ─── Core Commission Creation ─────────────────────────────────────────────────

@transaction.atomic
def trigger_commission_on_delivery(
    order_id,
    order_total,
    affiliate_code=None,
    buyer_user=None,
    referral_click=None,
):
    """
    Main entry point. Call this when an order is marked as delivered.

    Parameters
    ----------
    order_id       : int   — your Order model's PK
    order_total    : Decimal — gross order amount in BDT
    affiliate_code : str   — referral code string (from order record)
    buyer_user     : User  — the user who placed the order (can be None for guests)
    referral_click : ReferralClick — the click that led to this order (optional)

    Returns
    -------
    commission : Commission | None
        None if no valid affiliate found, fraud detected, or duplicate.
    """
    order_total = Decimal(str(order_total))

    # ── 1. Resolve affiliate ──────────────────────────────────────────────
    affiliate = _resolve_affiliate(affiliate_code)
    if not affiliate:
        logger.info("No valid affiliate for order #%s (code=%s)", order_id, affiliate_code)
        return None

    # ── 2. Duplicate guard (fast check before fraud checks) ──────────────
    if Commission.objects.filter(affiliate=affiliate, order_id=order_id).exists():
        logger.warning(
            "Duplicate commission attempt blocked: affiliate %s, order #%s",
            affiliate.referral_code, order_id
        )
        return None

    # ── 3. Fraud checks ───────────────────────────────────────────────────
    buyer_ip = _get_client_ip_from_click(referral_click)
    is_fraud, fraud_reason = _run_fraud_checks(
        affiliate, order_id, buyer_user, buyer_ip
    )

    # ── 4. Get commission setting ─────────────────────────────────────────
    setting = CommissionSetting.get_default()
    if not setting:
        logger.error(
            "No default CommissionSetting found. Cannot create commission for order #%s.",
            order_id
        )
        return None

    commission_amount = setting.calculate_commission(order_total)

    # ── 5. Create Commission row ──────────────────────────────────────────
    try:
        commission = Commission.objects.create(
            affiliate=affiliate,
            order_id=order_id,
            order_total=order_total,
            commission_amount=commission_amount,
            commission_setting=setting,
            status=Commission.STATUS_PENDING,
            is_fraud_suspected=is_fraud,
            fraud_reason=fraud_reason,
            referral_click=referral_click,
            referred_user=buyer_user,
        )
    except IntegrityError:
        # Race condition — another request already created it
        logger.warning(
            "IntegrityError creating commission: affiliate %s, order #%s — already exists",
            affiliate.referral_code, order_id
        )
        return None

    # ── 6. Update affiliate balance (only if NOT fraud) ───────────────────
    if not is_fraud:
        AffiliateProfile.objects.filter(pk=affiliate.pk).update(
            balance_pending=models.F('balance_pending') + commission_amount
        )
        logger.info(
            "Commission #%s created: affiliate %s earned ৳%s for order #%s",
            commission.pk, affiliate.referral_code, commission_amount, order_id
        )
    else:
        # Flag the affiliate if enough fraud flags accumulate
        _auto_flag_affiliate_if_needed(affiliate)

    return commission


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _resolve_affiliate(affiliate_code):
    """Return an approved, non-flagged AffiliateProfile or None."""
    if not affiliate_code:
        return None
    try:
        return AffiliateProfile.objects.get(
            referral_code=affiliate_code.strip().upper(),
            status=AffiliateProfile.STATUS_APPROVED,
            is_fraud_flagged=False,
        )
    except AffiliateProfile.DoesNotExist:
        return None


def _auto_flag_affiliate_if_needed(affiliate):
    """
    Automatically set is_fraud_flagged=True on an affiliate if they have
    3 or more unresolved fraud flags.
    """
    unresolved_count = affiliate.fraud_flags.filter(is_resolved=False).count()
    if unresolved_count >= 3 and not affiliate.is_fraud_flagged:
        AffiliateProfile.objects.filter(pk=affiliate.pk).update(is_fraud_flagged=True)
        logger.warning(
            "Affiliate %s auto-flagged for fraud (%d unresolved flags)",
            affiliate.referral_code, unresolved_count
        )


def get_commission_for_order(order_id):
    """Return the Commission for a given order_id, or None."""
    return Commission.objects.filter(order_id=order_id).select_related('affiliate').first()


# ─── Order Attribution Store ──────────────────────────────────────────────────

def get_affiliate_attribution(request):
    """
    Extract affiliate attribution data from a request at checkout time.
    Returns a dict ready to save onto your Order model.

    Usage in your checkout view:
        attribution = get_affiliate_attribution(request)
        order.affiliate_code = attribution['affiliate_code']
        order.referral_click  = attribution['referral_click']
        order.save()
    """
    from .utils import get_affiliate_from_request

    affiliate = get_affiliate_from_request(request)
    if not affiliate:
        return {'affiliate_code': None, 'referral_click': None}

    # Find the most recent click for this session
    session_key = request.session.session_key or ''
    referral_click = None
    if session_key:
        referral_click = (
            ReferralClick.objects
            .filter(affiliate=affiliate, session_key=session_key)
            .order_by('-clicked_at')
            .first()
        )

    return {
        'affiliate_code': affiliate.referral_code,
        'referral_click': referral_click,
        'affiliate': affiliate,
    }


# Django F() expression needs models imported
from django.db import models
