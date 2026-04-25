"""
affiliate/views.py
------------------
M2 — Referral tracking
M3 — Registration & approval
M4 — Commission trigger endpoint (order delivery webhook / internal API)
"""

from django.shortcuts import redirect, render
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.conf import settings

from .models import AffiliateProfile, Commission
from .forms import AffiliateApplicationForm
from .utils import get_affiliate_from_request
from .services import trigger_commission_on_delivery, get_commission_for_order

COOKIE_NAME = getattr(settings, 'AFFILIATE_COOKIE_NAME', 'alphacue_ref')
COOKIE_MAX_AGE = getattr(settings, 'AFFILIATE_COOKIE_MAX_AGE', 60 * 60 * 24 * 30)
SESSION_KEY = getattr(settings, 'AFFILIATE_SESSION_KEY', 'affiliate_referral_code')


# ─── M2: Referral Tracking ───────────────────────────────────────────────────

@require_GET
def referral_redirect(request, code):
    code = code.strip().upper()
    redirect_to = request.GET.get('next', '/')
    try:
        affiliate = AffiliateProfile.objects.get(
            referral_code=code,
            status=AffiliateProfile.STATUS_APPROVED,
            is_fraud_flagged=False,
        )
    except AffiliateProfile.DoesNotExist:
        return redirect(redirect_to)
    if request.user.is_authenticated and request.user == affiliate.user:
        return redirect(redirect_to)
    request.session[SESSION_KEY] = code
    response = redirect(redirect_to)
    response.set_cookie(COOKIE_NAME, code, max_age=COOKIE_MAX_AGE, httponly=True, samesite='Lax')
    return response


@require_GET
def referral_status(request):
    if not settings.DEBUG and not request.user.is_staff:
        return JsonResponse({'error': 'Forbidden'}, status=403)
    affiliate = get_affiliate_from_request(request)
    return JsonResponse({
        'has_referral': affiliate is not None,
        'referral_code': affiliate.referral_code if affiliate else None,
        'affiliate_name': affiliate.full_name if affiliate else None,
        'cookie_value': request.COOKIES.get(COOKIE_NAME),
        'session_value': request.session.get(SESSION_KEY),
    })


# ─── M3: Registration & Approval ────────────────────────────────────────────

def affiliate_apply(request):
    # Redirect already-applied users to their status page
    if request.user.is_authenticated and hasattr(request.user, 'affiliate_profile'):
        return redirect('affiliate:application_status')

    if request.method == 'POST':
        # Must be logged in to submit
        if not request.user.is_authenticated:
            messages.warning(request, 'Please log in or create an account to apply as an affiliate.')
            return redirect(f"/accounts/login/?next={request.path}")

        form = AffiliateApplicationForm(request.POST)
        if form.is_valid():
            profile = form.save(commit=False)
            profile.user = request.user
            profile.status = AffiliateProfile.STATUS_PENDING
            profile.save()
            messages.success(
                request,
                'Your application has been submitted! We will review it within 1–2 business days.'
            )
            return redirect('affiliate:application_status')
    else:
        initial = {}
        if request.user.is_authenticated:
            if request.user.get_full_name():
                initial['full_name'] = request.user.get_full_name()
        form = AffiliateApplicationForm(initial=initial)

    return render(request, 'affiliate/apply.html', {
        'form': form,
        'user_logged_in': request.user.is_authenticated,
    })


@login_required
def application_status(request):
    try:
        profile = request.user.affiliate_profile
    except AffiliateProfile.DoesNotExist:
        return redirect('affiliate:affiliate_apply')
    return render(request, 'affiliate/application_status.html', {'profile': profile})


# ─── M4: Commission Trigger ───────────────────────────────────────────────────

@csrf_exempt
@require_POST
def order_delivered_webhook(request):
    """
    POST /affiliate/order-delivered/
    ---------------------------------
    Internal endpoint called by your order management system when an order
    is marked as delivered. Protected by a shared secret token.

    Expected POST body (JSON or form):
        order_id       : int
        order_total    : decimal string  e.g. "1250.00"
        affiliate_code : str             e.g. "ABC12345"
        buyer_user_id  : int (optional)

    Returns JSON response.
    """
    import json
    from decimal import Decimal, InvalidOperation
    from django.contrib.auth import get_user_model

    User = get_user_model()

    # ── Secret token check ────────────────────────────────────────────────
    expected_token = getattr(settings, 'AFFILIATE_WEBHOOK_SECRET', None)
    if expected_token:
        provided_token = request.headers.get('X-Affiliate-Secret', '')
        if provided_token != expected_token:
            return JsonResponse({'error': 'Unauthorized'}, status=401)

    # ── Parse body ────────────────────────────────────────────────────────
    try:
        if request.content_type == 'application/json':
            data = json.loads(request.body)
        else:
            data = request.POST
    except (json.JSONDecodeError, Exception):
        return JsonResponse({'error': 'Invalid request body'}, status=400)

    order_id = data.get('order_id')
    order_total = data.get('order_total')
    affiliate_code = data.get('affiliate_code', '')
    buyer_user_id = data.get('buyer_user_id')

    if not order_id or not order_total:
        return JsonResponse({'error': 'order_id and order_total are required'}, status=400)

    try:
        order_id = int(order_id)
        order_total = Decimal(str(order_total))
    except (ValueError, InvalidOperation):
        return JsonResponse({'error': 'Invalid order_id or order_total'}, status=400)

    # ── Resolve buyer user ────────────────────────────────────────────────
    buyer_user = None
    if buyer_user_id:
        try:
            buyer_user = User.objects.get(pk=int(buyer_user_id))
        except (User.DoesNotExist, ValueError):
            pass

    # ── Trigger commission ────────────────────────────────────────────────
    commission = trigger_commission_on_delivery(
        order_id=order_id,
        order_total=order_total,
        affiliate_code=affiliate_code,
        buyer_user=buyer_user,
    )

    if commission is None:
        return JsonResponse({
            'success': False,
            'message': 'No commission created (no affiliate, duplicate, or fraud detected)',
            'order_id': order_id,
        })

    return JsonResponse({
        'success': True,
        'commission_id': commission.pk,
        'affiliate_code': commission.affiliate.referral_code,
        'commission_amount': str(commission.commission_amount),
        'status': commission.status,
        'fraud_suspected': commission.is_fraud_suspected,
        'order_id': order_id,
    })


@require_GET
def commission_check(request, order_id):
    """
    GET /affiliate/commission-check/<order_id>/
    -------------------------------------------
    Staff-only. Check if a commission exists for a given order.
    Useful for testing and support.
    """
    if not request.user.is_staff:
        return JsonResponse({'error': 'Forbidden'}, status=403)

    commission = get_commission_for_order(order_id)
    if not commission:
        return JsonResponse({'exists': False, 'order_id': order_id})

    return JsonResponse({
        'exists': True,
        'commission_id': commission.pk,
        'affiliate_code': commission.affiliate.referral_code,
        'commission_amount': str(commission.commission_amount),
        'status': commission.status,
        'fraud_suspected': commission.is_fraud_suspected,
        'created_at': commission.created_at.isoformat(),
    })