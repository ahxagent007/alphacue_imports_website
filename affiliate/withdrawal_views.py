"""
affiliate/withdrawal_views.py
------------------------------
M6 — Withdrawal System

Affiliate-facing:
    /affiliate/withdraw/      — request a new withdrawal
    /affiliate/withdrawals/   — history of all withdrawal requests
"""

from decimal import Decimal
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction

from .models import (
    AffiliateProfile, WithdrawalRequest, CommissionSetting
)
from .withdrawal_forms import WithdrawalRequestForm


def _require_approved_affiliate(request):
    if not request.user.is_authenticated:
        from django.contrib.auth.views import redirect_to_login
        return None, redirect_to_login(request.get_full_path())
    try:
        affiliate = request.user.affiliate_profile
    except AffiliateProfile.DoesNotExist:
        return None, redirect('affiliate:affiliate_apply')
    if affiliate.status != AffiliateProfile.STATUS_APPROVED:
        return None, redirect('affiliate:application_status')
    return affiliate, None


# ─── Withdrawal Request ───────────────────────────────────────────────────────

@login_required
def withdrawal_request(request):
    """
    GET  /affiliate/withdraw/  — show withdrawal form
    POST /affiliate/withdraw/  — submit request
    """
    affiliate, err = _require_approved_affiliate(request)
    if err:
        return err

    # Get minimum withdrawal from CommissionSetting
    setting    = CommissionSetting.get_default()
    min_amount = setting.minimum_withdrawal_amount if setting else Decimal('500.00')
    max_amount = affiliate.withdrawal_balance

    # Guard: nothing to withdraw
    if max_amount <= Decimal('0.00'):
        messages.warning(request, "You have no approved balance available to withdraw.")
        return redirect('affiliate:dashboard')

    # Guard: below minimum
    if max_amount < min_amount:
        messages.warning(
            request,
            f"Your available balance (৳{max_amount:,.0f}) is below the minimum "
            f"withdrawal amount of ৳{min_amount:,.0f}."
        )
        return redirect('affiliate:dashboard')

    # Guard: pending withdrawal already exists
    has_pending = affiliate.withdrawal_requests.filter(
        status=WithdrawalRequest.STATUS_PENDING
    ).exists()
    if has_pending:
        messages.warning(
            request,
            "You already have a pending withdrawal request. "
            "Please wait for it to be processed before submitting another."
        )
        return redirect('affiliate:withdrawal_history')

    if request.method == 'POST':
        form = WithdrawalRequestForm(
            request.POST,
            affiliate=affiliate,
            min_amount=min_amount,
            max_amount=max_amount,
        )
        if form.is_valid():
            return _submit_withdrawal(request, form, affiliate)
    else:
        form = WithdrawalRequestForm(
            affiliate=affiliate,
            min_amount=min_amount,
            max_amount=max_amount,
            initial={'amount': max_amount},
        )

    ctx = {
        'affiliate':  affiliate,
        'form':       form,
        'min_amount': min_amount,
        'max_amount': max_amount,
        'setting':    setting,
    }
    return render(request, 'affiliate/withdrawal_request.html', ctx)


@transaction.atomic
def _submit_withdrawal(request, form, affiliate):
    wr = form.save(commit=False)
    wr.affiliate = affiliate
    wr.status    = WithdrawalRequest.STATUS_PENDING
    wr.save()

    messages.success(
        request,
        f"Withdrawal request of ৳{wr.amount:,.0f} submitted successfully. "
        f"Admin will process it within 1–3 business days."
    )
    return redirect('affiliate:withdrawal_history')


# ─── Withdrawal History ───────────────────────────────────────────────────────

@login_required
def withdrawal_history(request):
    """
    GET /affiliate/withdrawals/
    Full list of all withdrawal requests for this affiliate.
    """
    affiliate, err = _require_approved_affiliate(request)
    if err:
        return err

    from django.core.paginator import Paginator

    qs        = affiliate.withdrawal_requests.order_by('-requested_at')
    paginator = Paginator(qs, 15)
    page_obj  = paginator.get_page(request.GET.get('page', 1))

    # Summary
    total_requested = qs.filter(
        status__in=[WithdrawalRequest.STATUS_APPROVED, WithdrawalRequest.STATUS_PAID]
    ).count()
    from django.db.models import Sum as DSum
    total_paid_amount = qs.filter(
        status=WithdrawalRequest.STATUS_PAID
    ).aggregate(t=DSum('amount'))['t'] or Decimal('0.00')

    ctx = {
        'affiliate':         affiliate,
        'page_obj':          page_obj,
        'total_requested':   total_requested,
        'total_paid_amount': total_paid_amount,
    }
    return render(request, 'affiliate/withdrawal_history.html', ctx)
