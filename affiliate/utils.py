"""
affiliate/utils.py
------------------
Reusable helpers for resolving the active affiliate from any request.
Call get_affiliate_from_request(request) in your order/checkout views.
"""

from django.conf import settings
from .models import AffiliateProfile

COOKIE_NAME = getattr(settings, 'AFFILIATE_COOKIE_NAME', 'alphacue_ref')
SESSION_KEY = getattr(settings, 'AFFILIATE_SESSION_KEY', 'affiliate_referral_code')


def get_affiliate_from_request(request):
    """
    Resolve the active affiliate for a request.
    Checks: middleware cache → session → cookie
    Returns AffiliateProfile or None.
    """
    affiliate = getattr(request, 'affiliate', None)
    if affiliate is not None:
        return affiliate

    code = (
        request.session.get(SESSION_KEY, '').strip().upper()
        or request.COOKIES.get(COOKIE_NAME, '').strip().upper()
    )

    if not code:
        return None

    try:
        affiliate = AffiliateProfile.objects.get(
            referral_code=code,
            status=AffiliateProfile.STATUS_APPROVED,
            is_fraud_flagged=False,
        )
    except AffiliateProfile.DoesNotExist:
        return None

    if request.user.is_authenticated and request.user == affiliate.user:
        return None

    return affiliate


def clear_referral(request, response=None):
    """
    Remove referral from session and cookie.
    Call this after a successful order to prevent re-attribution.
    """
    request.session.pop(SESSION_KEY, None)

    keys_to_delete = [
        k for k in request.session.keys()
        if k.startswith('affiliate_click_recorded_')
    ]
    for key in keys_to_delete:
        del request.session[key]

    if response is not None:
        response.delete_cookie(COOKIE_NAME)

    if hasattr(request, 'affiliate'):
        request.affiliate = None


def get_referral_code_from_request(request):
    """Return the raw referral code string, or empty string."""
    affiliate = get_affiliate_from_request(request)
    return affiliate.referral_code if affiliate else ''
