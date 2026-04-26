"""
affiliate/admin_views.py
------------------------
Custom admin management pages for:
  - Affiliate approval queue
  - Commission approval queue
  - Withdrawal processing queue
  - Quick action endpoints (AJAX POST)
All views require staff access.
"""

import json
from decimal import Decimal
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.admin.views.decorators import staff_member_required
from django.views.decorators.http import require_POST
from django.http import JsonResponse
from django.contrib import messages
from django.db import models, transaction
from django.utils import timezone
from django.core.paginator import Paginator
from django.db.models import Sum, Count, Q

from .models import (
    AffiliateProfile, Commission, WithdrawalRequest, FraudFlag
)


# ─── Admin Hub ────────────────────────────────────────────────────────────────

@staff_member_required
def admin_hub(request):
    """
    /affiliate/admin/hub/
    Master overview — counts of pending items requiring attention.
    """
    ctx = {
        'pending_affiliates':   AffiliateProfile.objects.filter(status=AffiliateProfile.STATUS_PENDING).count(),
        'pending_commissions':  Commission.objects.filter(status=Commission.STATUS_PENDING, is_fraud_suspected=False).count(),
        'suspected_commissions':Commission.objects.filter(is_fraud_suspected=True, status=Commission.STATUS_PENDING).count(),
        'pending_withdrawals':  WithdrawalRequest.objects.filter(status=WithdrawalRequest.STATUS_PENDING).count(),
        'approved_withdrawals': WithdrawalRequest.objects.filter(status=WithdrawalRequest.STATUS_APPROVED).count(),
        'open_fraud_flags':     FraudFlag.objects.filter(is_resolved=False).count(),
        'flagged_affiliates':   AffiliateProfile.objects.filter(is_fraud_flagged=True).count(),

        # Recent pending items
        'recent_affiliates':    AffiliateProfile.objects.filter(status=AffiliateProfile.STATUS_PENDING).order_by('-applied_at')[:5],
        'recent_withdrawals':   WithdrawalRequest.objects.filter(status=WithdrawalRequest.STATUS_PENDING).select_related('affiliate').order_by('requested_at')[:5],
        'recent_commissions':   Commission.objects.filter(status=Commission.STATUS_PENDING, is_fraud_suspected=False).select_related('affiliate').order_by('-created_at')[:5],
    }
    return render(request, 'affiliate/admin/hub.html', ctx)


# ─── Affiliate Approval Queue ─────────────────────────────────────────────────

@staff_member_required
def affiliate_queue(request):
    """
    /affiliate/admin/affiliates/
    Pending affiliate applications with approve/reject actions.
    """
    status_filter = request.GET.get('status', 'pending')
    search = request.GET.get('q', '').strip()

    qs = AffiliateProfile.objects.select_related('user').order_by('-applied_at')
    if status_filter:
        qs = qs.filter(status=status_filter)
    if search:
        qs = qs.filter(
            Q(full_name__icontains=search) |
            Q(referral_code__icontains=search) |
            Q(phone_number__icontains=search)
        )

    paginator = Paginator(qs, 20)
    page_obj  = paginator.get_page(request.GET.get('page', 1))

    ctx = {
        'page_obj':       page_obj,
        'status_filter':  status_filter,
        'search':         search,
        'status_choices': AffiliateProfile.STATUS_CHOICES,
        'count_pending':  AffiliateProfile.objects.filter(status='pending').count(),
        'count_approved': AffiliateProfile.objects.filter(status='approved').count(),
        'count_rejected': AffiliateProfile.objects.filter(status='rejected').count(),
        'count_suspended':AffiliateProfile.objects.filter(status='suspended').count(),
    }
    return render(request, 'affiliate/admin/affiliate_queue.html', ctx)


@staff_member_required
@require_POST
def affiliate_action(request, affiliate_id):
    """
    POST /affiliate/admin/affiliates/<id>/action/
    Quick action: approve / reject / suspend / clear_fraud
    """
    affiliate = get_object_or_404(AffiliateProfile, pk=affiliate_id)
    action    = request.POST.get('action', '').strip()

    if action == 'approve':
        affiliate.status = AffiliateProfile.STATUS_APPROVED
        affiliate.approved_at = timezone.now()
        affiliate.rejection_reason = ''
        affiliate.save(update_fields=['status', 'approved_at', 'rejection_reason'])
        messages.success(request, f"✅ {affiliate.full_name} approved.")

    elif action == 'reject':
        reason = request.POST.get('reason', '').strip()
        affiliate.status = AffiliateProfile.STATUS_REJECTED
        affiliate.rejection_reason = reason
        affiliate.save(update_fields=['status', 'rejection_reason'])
        messages.warning(request, f"❌ {affiliate.full_name} rejected.")

    elif action == 'suspend':
        affiliate.status = AffiliateProfile.STATUS_SUSPENDED
        affiliate.save(update_fields=['status'])
        messages.warning(request, f"🚫 {affiliate.full_name} suspended.")

    elif action == 'clear_fraud':
        affiliate.fraud_flags.filter(is_resolved=False).update(
            is_resolved=True, resolved_by=request.user, resolved_at=timezone.now()
        )
        affiliate.is_fraud_flagged = False
        affiliate.save(update_fields=['is_fraud_flagged'])
        messages.success(request, f"🛡️ Fraud flag cleared for {affiliate.full_name}.")

    return redirect(request.META.get('HTTP_REFERER', 'affiliate:affiliate_queue'))


# ─── Commission Approval Queue ────────────────────────────────────────────────

@staff_member_required
def commission_queue(request):
    """
    /affiliate/admin/commissions/
    Pending commissions — approve, reject, or review suspected fraud.
    """
    tab    = request.GET.get('tab', 'pending')
    search = request.GET.get('q', '').strip()

    if tab == 'suspected':
        qs = Commission.objects.filter(is_fraud_suspected=True, status=Commission.STATUS_PENDING)
    elif tab == 'approved':
        qs = Commission.objects.filter(status=Commission.STATUS_APPROVED)
    elif tab == 'paid':
        qs = Commission.objects.filter(status=Commission.STATUS_PAID)
    else:
        qs = Commission.objects.filter(status=Commission.STATUS_PENDING, is_fraud_suspected=False)

    if search:
        qs = qs.filter(
            Q(affiliate__referral_code__icontains=search) |
            Q(affiliate__full_name__icontains=search) |
            Q(order_id__icontains=search)
        )

    qs = qs.select_related('affiliate').order_by('-created_at')
    paginator = Paginator(qs, 25)
    page_obj  = paginator.get_page(request.GET.get('page', 1))

    ctx = {
        'page_obj':        page_obj,
        'tab':             tab,
        'search':          search,
        'count_pending':   Commission.objects.filter(status='pending', is_fraud_suspected=False).count(),
        'count_suspected': Commission.objects.filter(is_fraud_suspected=True, status='pending').count(),
        'count_approved':  Commission.objects.filter(status='approved').count(),
        'count_paid':      Commission.objects.filter(status='paid').count(),
    }
    return render(request, 'affiliate/admin/commission_queue.html', ctx)


@staff_member_required
@require_POST
@transaction.atomic
def commission_action(request, commission_id):
    """
    POST /affiliate/admin/commissions/<id>/action/
    Quick action: approve / reject / mark_paid
    """
    commission = get_object_or_404(Commission, pk=commission_id)
    action     = request.POST.get('action', '').strip()

    if action == 'approve' and commission.status == Commission.STATUS_PENDING:
        commission.status      = Commission.STATUS_APPROVED
        commission.approved_at = timezone.now()
        commission.save(update_fields=['status', 'approved_at'])
        AffiliateProfile.objects.filter(pk=commission.affiliate_id).update(
            balance_pending=models.F('balance_pending') - commission.commission_amount,
            balance_approved=models.F('balance_approved') + commission.commission_amount,
        )
        messages.success(request, f"✅ Commission #{commission.pk} approved — ৳{commission.commission_amount:,.0f} moved to approved balance.")

    elif action == 'reject' and commission.status == Commission.STATUS_PENDING:
        commission.status = Commission.STATUS_REJECTED
        commission.save(update_fields=['status'])
        AffiliateProfile.objects.filter(pk=commission.affiliate_id).update(
            balance_pending=models.F('balance_pending') - commission.commission_amount,
        )
        messages.warning(request, f"❌ Commission #{commission.pk} rejected.")

    elif action == 'mark_paid' and commission.status == Commission.STATUS_APPROVED:
        commission.status  = Commission.STATUS_PAID
        commission.paid_at = timezone.now()
        commission.save(update_fields=['status', 'paid_at'])
        AffiliateProfile.objects.filter(pk=commission.affiliate_id).update(
            balance_approved=models.F('balance_approved') - commission.commission_amount,
            balance_paid=models.F('balance_paid') + commission.commission_amount,
        )
        messages.success(request, f"💳 Commission #{commission.pk} marked as paid.")

    elif action == 'clear_fraud' and commission.is_fraud_suspected:
        commission.is_fraud_suspected = False
        commission.fraud_reason = ''
        commission.save(update_fields=['is_fraud_suspected', 'fraud_reason'])
        AffiliateProfile.objects.filter(pk=commission.affiliate_id).update(
            balance_pending=models.F('balance_pending') + commission.commission_amount,
        )
        messages.success(request, f"🛡️ Fraud flag cleared — commission #{commission.pk} moved to pending.")

    return redirect(request.META.get('HTTP_REFERER', 'affiliate:commission_queue'))


# ─── Withdrawal Processing Queue ──────────────────────────────────────────────

@staff_member_required
def withdrawal_queue(request):
    """
    /affiliate/admin/withdrawals/
    Process withdrawal requests — approve, reject, mark paid.
    """
    tab    = request.GET.get('tab', 'pending')
    search = request.GET.get('q', '').strip()

    status_map = {
        'pending':  WithdrawalRequest.STATUS_PENDING,
        'approved': WithdrawalRequest.STATUS_APPROVED,
        'paid':     WithdrawalRequest.STATUS_PAID,
        'rejected': WithdrawalRequest.STATUS_REJECTED,
    }
    qs = WithdrawalRequest.objects.filter(
        status=status_map.get(tab, WithdrawalRequest.STATUS_PENDING)
    ).select_related('affiliate')

    if search:
        qs = qs.filter(
            Q(affiliate__referral_code__icontains=search) |
            Q(affiliate__full_name__icontains=search) |
            Q(payment_account__icontains=search) |
            Q(transaction_id__icontains=search)
        )

    if tab == 'pending':
        qs = qs.order_by('requested_at')   # oldest first — FIFO
    else:
        qs = qs.order_by('-processed_at')

    paginator = Paginator(qs, 20)
    page_obj  = paginator.get_page(request.GET.get('page', 1))

    # Totals for approved tab (ready to pay out)
    approved_total = WithdrawalRequest.objects.filter(
        status=WithdrawalRequest.STATUS_APPROVED
    ).aggregate(t=Sum('amount'))['t'] or Decimal('0.00')

    ctx = {
        'page_obj':        page_obj,
        'tab':             tab,
        'search':          search,
        'approved_total':  approved_total,
        'count_pending':   WithdrawalRequest.objects.filter(status='pending').count(),
        'count_approved':  WithdrawalRequest.objects.filter(status='approved').count(),
        'count_paid':      WithdrawalRequest.objects.filter(status='paid').count(),
        'count_rejected':  WithdrawalRequest.objects.filter(status='rejected').count(),
    }
    return render(request, 'affiliate/admin/withdrawal_queue.html', ctx)


@staff_member_required
@require_POST
@transaction.atomic
def withdrawal_action(request, withdrawal_id):
    """
    POST /affiliate/admin/withdrawals/<id>/action/
    Quick action: approve / reject / mark_paid (with transaction_id)
    """
    wr     = get_object_or_404(WithdrawalRequest, pk=withdrawal_id)
    action = request.POST.get('action', '').strip()

    if action == 'approve' and wr.status == WithdrawalRequest.STATUS_PENDING:
        wr.status       = WithdrawalRequest.STATUS_APPROVED
        wr.processed_at = timezone.now()
        wr.save(update_fields=['status', 'processed_at'])
        messages.success(request, f"✅ Withdrawal #{wr.pk} (৳{wr.amount:,.0f}) approved for {wr.affiliate.full_name}.")

    elif action == 'reject' and wr.status in (WithdrawalRequest.STATUS_PENDING, WithdrawalRequest.STATUS_APPROVED):
        reason = request.POST.get('reason', '').strip()
        wr.status           = WithdrawalRequest.STATUS_REJECTED
        wr.rejection_reason = reason
        wr.processed_at     = timezone.now()
        wr.save(update_fields=['status', 'rejection_reason', 'processed_at'])
        # No balance restore needed — we never deducted on approve.
        # The withdrawal_balance property now excludes pending+approved,
        # so rejecting automatically makes the balance available again.
        messages.warning(request, f"❌ Withdrawal #{wr.pk} rejected. Balance restored to affiliate.")

    elif action == 'mark_paid' and wr.status == WithdrawalRequest.STATUS_APPROVED:
        txn_id = request.POST.get('transaction_id', '').strip()
        if not txn_id:
            messages.error(request, "⚠️ Transaction ID is required to mark as paid.")
            return redirect(request.META.get('HTTP_REFERER', 'affiliate:withdrawal_queue'))

        wr.status         = WithdrawalRequest.STATUS_PAID
        wr.transaction_id = txn_id
        wr.processed_at   = timezone.now()
        wr.save(update_fields=['status', 'transaction_id', 'processed_at'])

        # Deduct from affiliate balance_approved and add to total_withdrawn
        # Note: balance is already "reserved" (excluded from withdrawal_balance)
        # since this request was in APPROVED status. Now we make it permanent.
        AffiliateProfile.objects.filter(pk=wr.affiliate_id).update(
            balance_approved=models.F('balance_approved') - wr.amount,
            total_withdrawn=models.F('total_withdrawn') + wr.amount,
        )
        messages.success(
            request,
            f"💳 ৳{wr.amount:,.0f} marked as paid to {wr.affiliate.full_name} "
            f"({wr.get_payment_method_display()} {wr.payment_account}). TXN: {txn_id}"
        )

    return redirect(request.META.get('HTTP_REFERER', 'affiliate:withdrawal_queue'))