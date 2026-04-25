from django.contrib import admin
from django.utils import timezone
from django.utils.html import format_html
from django.db import models

from .models import (
    AffiliateProfile, CommissionSetting, ReferralClick,
    Commission, WithdrawalRequest, ResellerPricing, FraudFlag
)


class CommissionInline(admin.TabularInline):
    model = Commission
    fields = ('order_id', 'order_total', 'commission_amount', 'status', 'created_at')
    readonly_fields = ('order_id', 'order_total', 'commission_amount', 'created_at')
    extra = 0
    max_num = 20
    can_delete = False
    ordering = ['-created_at']


class WithdrawalInline(admin.TabularInline):
    model = WithdrawalRequest
    fields = ('amount', 'payment_method', 'payment_account', 'status', 'requested_at')
    readonly_fields = ('amount', 'payment_method', 'payment_account', 'requested_at')
    extra = 0
    max_num = 10
    can_delete = False


@admin.register(AffiliateProfile)
class AffiliateProfileAdmin(admin.ModelAdmin):
    list_display = (
        'full_name', 'referral_code', 'status_badge', 'phone_number',
        'balance_pending', 'balance_approved', 'is_reseller', 'is_fraud_flagged', 'applied_at'
    )
    list_filter = ('status', 'is_reseller', 'is_fraud_flagged', 'preferred_payment_method')
    search_fields = ('full_name', 'referral_code', 'phone_number', 'user__email')
    readonly_fields = (
        'referral_code', 'applied_at', 'approved_at', 'updated_at',
        'balance_pending', 'balance_approved', 'balance_paid',
        'total_withdrawn', 'withdrawal_balance_display'
    )
    fieldsets = (
        ('👤 Affiliate Info', {
            'fields': ('user', 'full_name', 'phone_number', 'nid_number', 'referral_code')
        }),
        ('📋 Application', {
            'fields': ('how_will_promote', 'status', 'admin_notes', 'rejection_reason')
        }),
        ('💳 Payment', {
            'fields': ('preferred_payment_method', 'payment_account_number')
        }),
        ('💰 Balances', {
            'fields': (
                'balance_pending', 'balance_approved', 'balance_paid',
                'total_withdrawn', 'withdrawal_balance_display'
            )
        }),
        ('⚠️ Flags', {
            'fields': ('is_reseller', 'is_fraud_flagged')
        }),
        ('🕐 Timestamps', {
            'fields': ('applied_at', 'approved_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    actions = ['approve_affiliates', 'reject_affiliates', 'suspend_affiliates']
    inlines = [CommissionInline, WithdrawalInline]

    def status_badge(self, obj):
        colors = {
            'pending': '#f59e0b',
            'approved': '#10b981',
            'rejected': '#ef4444',
            'suspended': '#6b7280',
        }
        color = colors.get(obj.status, '#6b7280')
        return format_html(
            '<span style="background:{};color:white;padding:2px 8px;'
            'border-radius:4px;font-size:11px">{}</span>',
            color, obj.get_status_display()
        )
    status_badge.short_description = 'Status'

    def withdrawal_balance_display(self, obj):
        return f"৳{obj.withdrawal_balance}"
    withdrawal_balance_display.short_description = 'Available to Withdraw'

    @admin.action(description='✅ Approve selected affiliates')
    def approve_affiliates(self, request, queryset):
        updated = queryset.filter(status=AffiliateProfile.STATUS_PENDING).update(
            status=AffiliateProfile.STATUS_APPROVED,
            approved_at=timezone.now()
        )
        self.message_user(request, f"{updated} affiliate(s) approved.")

    @admin.action(description='❌ Reject selected affiliates')
    def reject_affiliates(self, request, queryset):
        updated = queryset.filter(status=AffiliateProfile.STATUS_PENDING).update(
            status=AffiliateProfile.STATUS_REJECTED
        )
        self.message_user(request, f"{updated} affiliate(s) rejected.")

    @admin.action(description='🚫 Suspend selected affiliates')
    def suspend_affiliates(self, request, queryset):
        updated = queryset.exclude(status=AffiliateProfile.STATUS_SUSPENDED).update(
            status=AffiliateProfile.STATUS_SUSPENDED
        )
        self.message_user(request, f"{updated} affiliate(s) suspended.")


@admin.register(CommissionSetting)
class CommissionSettingAdmin(admin.ModelAdmin):
    list_display = (
        'name', 'commission_type', 'commission_value',
        'minimum_withdrawal_amount', 'cookie_lifetime_days', 'is_default', 'is_active'
    )
    list_filter = ('commission_type', 'is_active', 'is_default')
    search_fields = ('name',)


@admin.register(ReferralClick)
class ReferralClickAdmin(admin.ModelAdmin):
    list_display = ('affiliate', 'ip_address', 'referred_user', 'clicked_at')
    list_filter = ('clicked_at',)
    search_fields = ('affiliate__referral_code', 'ip_address')
    readonly_fields = ('clicked_at',)
    date_hierarchy = 'clicked_at'


@admin.register(Commission)
class CommissionAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'affiliate', 'order_id', 'order_total',
        'commission_amount', 'status_badge', 'is_fraud_suspected', 'created_at'
    )
    list_filter = ('status', 'is_fraud_suspected', 'created_at')
    search_fields = ('affiliate__referral_code', 'order_id')
    readonly_fields = ('created_at', 'approved_at', 'paid_at')
    actions = ['approve_commissions', 'mark_paid', 'reject_commissions']
    date_hierarchy = 'created_at'

    def status_badge(self, obj):
        colors = {
            'pending': '#f59e0b',
            'approved': '#10b981',
            'paid': '#3b82f6',
            'rejected': '#ef4444',
        }
        color = colors.get(obj.status, '#6b7280')
        return format_html(
            '<span style="background:{};color:white;padding:2px 8px;'
            'border-radius:4px;font-size:11px">{}</span>',
            color, obj.get_status_display()
        )
    status_badge.short_description = 'Status'

    @admin.action(description='✅ Approve selected commissions')
    def approve_commissions(self, request, queryset):
        qs = queryset.filter(status=Commission.STATUS_PENDING, is_fraud_suspected=False)
        for commission in qs:
            commission.status = Commission.STATUS_APPROVED
            commission.approved_at = timezone.now()
            commission.save(update_fields=['status', 'approved_at'])
            AffiliateProfile.objects.filter(pk=commission.affiliate_id).update(
                balance_pending=models.F('balance_pending') - commission.commission_amount,
                balance_approved=models.F('balance_approved') + commission.commission_amount
            )
        self.message_user(request, f"{qs.count()} commission(s) approved.")

    @admin.action(description='💳 Mark selected commissions as paid')
    def mark_paid(self, request, queryset):
        qs = queryset.filter(status=Commission.STATUS_APPROVED)
        for commission in qs:
            commission.status = Commission.STATUS_PAID
            commission.paid_at = timezone.now()
            commission.save(update_fields=['status', 'paid_at'])
            AffiliateProfile.objects.filter(pk=commission.affiliate_id).update(
                balance_approved=models.F('balance_approved') - commission.commission_amount,
                balance_paid=models.F('balance_paid') + commission.commission_amount
            )
        self.message_user(request, f"{qs.count()} commission(s) marked as paid.")

    @admin.action(description='❌ Reject selected commissions')
    def reject_commissions(self, request, queryset):
        qs = queryset.filter(status=Commission.STATUS_PENDING)
        for commission in qs:
            commission.status = Commission.STATUS_REJECTED
            commission.save(update_fields=['status'])
            AffiliateProfile.objects.filter(pk=commission.affiliate_id).update(
                balance_pending=models.F('balance_pending') - commission.commission_amount
            )
        self.message_user(request, f"{qs.count()} commission(s) rejected.")


@admin.register(WithdrawalRequest)
class WithdrawalRequestAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'affiliate', 'amount', 'payment_method',
        'payment_account', 'status', 'requested_at'
    )
    list_filter = ('status', 'payment_method', 'requested_at')
    search_fields = ('affiliate__referral_code', 'payment_account', 'transaction_id')
    readonly_fields = ('requested_at',)
    actions = ['approve_withdrawals', 'mark_withdrawals_paid', 'reject_withdrawals']

    @admin.action(description='✅ Approve selected withdrawals')
    def approve_withdrawals(self, request, queryset):
        updated = queryset.filter(status=WithdrawalRequest.STATUS_PENDING).update(
            status=WithdrawalRequest.STATUS_APPROVED,
            processed_at=timezone.now()
        )
        self.message_user(request, f"{updated} withdrawal(s) approved.")

    @admin.action(description='💳 Mark as paid')
    def mark_withdrawals_paid(self, request, queryset):
        qs = queryset.filter(status=WithdrawalRequest.STATUS_APPROVED)
        for w in qs:
            w.status = WithdrawalRequest.STATUS_PAID
            w.processed_at = timezone.now()
            w.save(update_fields=['status', 'processed_at'])
            AffiliateProfile.objects.filter(pk=w.affiliate_id).update(
                balance_approved=models.F('balance_approved') - w.amount,
                total_withdrawn=models.F('total_withdrawn') + w.amount
            )
        self.message_user(request, f"{qs.count()} withdrawal(s) marked as paid.")

    @admin.action(description='❌ Reject selected withdrawals')
    def reject_withdrawals(self, request, queryset):
        updated = queryset.filter(status=WithdrawalRequest.STATUS_PENDING).update(
            status=WithdrawalRequest.STATUS_REJECTED,
            processed_at=timezone.now()
        )
        self.message_user(request, f"{updated} withdrawal(s) rejected.")


@admin.register(ResellerPricing)
class ResellerPricingAdmin(admin.ModelAdmin):
    list_display = (
        'affiliate', 'product_id', 'base_price_snapshot',
        'reseller_price', 'markup_display', 'is_active', 'updated_at'
    )
    list_filter = ('is_active',)
    search_fields = ('affiliate__referral_code', 'product_id')

    def markup_display(self, obj):
        return f"৳{obj.markup_amount} ({obj.markup_percentage}%)"
    markup_display.short_description = 'Markup'


@admin.register(FraudFlag)
class FraudFlagAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'affiliate', 'reason', 'order_id',
        'ip_address', 'is_resolved', 'flagged_at'
    )
    list_filter = ('reason', 'is_resolved', 'flagged_at')
    search_fields = ('affiliate__referral_code', 'ip_address')
    readonly_fields = ('flagged_at',)
    actions = ['resolve_flags']

    @admin.action(description='✅ Mark selected flags as resolved')
    def resolve_flags(self, request, queryset):
        updated = queryset.filter(is_resolved=False).update(
            is_resolved=True,
            resolved_by=request.user,
            resolved_at=timezone.now()
        )
        self.message_user(request, f"{updated} flag(s) resolved.")
