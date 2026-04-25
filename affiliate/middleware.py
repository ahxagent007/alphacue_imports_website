"""
affiliate/middleware.py
-----------------------
Captures referral codes from:
  1. URL parameter  → /?ref=CODE  or /ref/CODE/
  2. Cookie         → alphacue_ref
  3. Session        → affiliate_referral_code

Priority: URL param > existing cookie > session
Stores in both cookie and session so it survives across guest/login transitions.
"""

from django.conf import settings
from django.utils import timezone

from .models import AffiliateProfile, ReferralClick

COOKIE_NAME = getattr(settings, 'AFFILIATE_COOKIE_NAME', 'alphacue_ref')
COOKIE_MAX_AGE = getattr(settings, 'AFFILIATE_COOKIE_MAX_AGE', 60 * 60 * 24 * 30)
SESSION_KEY = getattr(settings, 'AFFILIATE_SESSION_KEY', 'affiliate_referral_code')

EXCLUDED_PATHS = (
    '/admin/',
    '/static/',
    '/media/',
    '/favicon.ico',
)


def _get_client_ip(request):
    """Extract real IP, accounting for proxies / cPanel reverse proxy."""
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        return x_forwarded_for.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '0.0.0.0')


def _should_skip(request):
    path = request.path_info
    return any(path.startswith(p) for p in EXCLUDED_PATHS)


def _record_click(request, affiliate, code):
    """
    Write one ReferralClick row per unique session+affiliate combo.
    Avoids hammering the DB on every page load.
    """
    session_click_key = f'affiliate_click_recorded_{code}'
    if request.session.get(session_click_key):
        return

    ip = _get_client_ip(request)
    user_agent = request.META.get('HTTP_USER_AGENT', '')[:500]
    referred_user = request.user if request.user.is_authenticated else None

    try:
        landing_url = request.build_absolute_uri()[:500]
    except Exception:
        landing_url = ''

    ReferralClick.objects.create(
        affiliate=affiliate,
        ip_address=ip,
        user_agent=user_agent,
        referred_user=referred_user,
        landing_url=landing_url,
        session_key=request.session.session_key or '',
        clicked_at=timezone.now(),
    )
    request.session[session_click_key] = True


class AffiliateReferralMiddleware:
    """
    Runs on every request. Resolves the active referral code and
    attaches affiliate to request. Sets cookie on response.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not _should_skip(request):
            self._process_referral(request)

        response = self.get_response(request)

        pending_code = getattr(request, '_affiliate_code_to_set', None)
        if pending_code:
            response.set_cookie(
                COOKIE_NAME,
                pending_code,
                max_age=COOKIE_MAX_AGE,
                httponly=True,
                samesite='Lax',
            )

        return response

    def _process_referral(self, request):
        code_from_url = request.GET.get('ref', '').strip().upper() or None
        code_from_cookie = request.COOKIES.get(COOKIE_NAME, '').strip().upper() or None
        code_from_session = request.session.get(SESSION_KEY, '').strip().upper() or None

        code = code_from_url or code_from_cookie or code_from_session

        if not code:
            request.affiliate = None
            return

        try:
            affiliate = AffiliateProfile.objects.get(
                referral_code=code,
                status=AffiliateProfile.STATUS_APPROVED,
                is_fraud_flagged=False,
            )
        except AffiliateProfile.DoesNotExist:
            request.affiliate = None
            request.session.pop(SESSION_KEY, None)
            request._affiliate_code_to_set = None
            return

        # Self-referral guard
        if request.user.is_authenticated and request.user == affiliate.user:
            request.affiliate = None
            return

        request.session[SESSION_KEY] = code
        request._affiliate_code_to_set = code
        request.affiliate = affiliate

        _record_click(request, affiliate, code)
