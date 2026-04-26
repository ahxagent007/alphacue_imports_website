from django.contrib import admin
from django.utils import timezone
from django.utils.html import format_html, mark_safe
from django.db import models
from django.contrib import messages as dj_messages
from django.db.models import Sum, Count

from .models import (
    AffiliateProfile, CommissionSetting, ReferralClick,
    Commission, WithdrawalRequest, ResellerPricing, FraudFlag
)


# ─── Inlines ──────────────────────────────────────────────────────────────────

class CommissionInline(admin.TabularInline):
    model = Commission
    fields = ('order_id', 'order_total', 'commission_amount', 'status', 'is_fraud_suspected', 'created_at')
    readonly_fields = ('order_id', 'order_total', 'commission_amount', 'status', 'is_fraud_suspected', 'created_at')
    extra = 0
    max_num = 20
    can_delete = False
    ordering = ['-created_at']
    verbose_name_plural = 'Commission History'


class WithdrawalInline(admin.TabularInline):
    model = WithdrawalRequest
    fields = ('amount', 'payment_method', 'payment_account', 'status', 'transaction_id', 'requested_at')
    readonly_fields = ('amount', 'payment_method', 'payment_account', 'status', 'transaction_id', 'requested_at')
    extra = 0
    max_num = 10
    can_delete = False
    verbose_name_plural = 'Withdrawal History'


class FraudFlagInline(admin.TabularInline):
    model = FraudFlag
    fields = ('reason', 'details', 'order_id', 'ip_address', 'is_resolved', 'flagged_at')
    readonly_fields = ('reason', 'details', 'order_id', 'ip_address', 'flagged_at')
    extra = 0
    max_num = 10
    can_delete = False
    verbose_name_plural = 'Fraud Flags'


# ─── Affiliate Profile Admin ──────────────────────────────────────────────────

@admin.register(AffiliateProfile)
class AffiliateProfileAdmin(admin.ModelAdmin):
    list_display = (
        'full_name', 'referral_code', 'status_badge', 'phone_number',
        'balance_pending_col', 'balance_approved_col',
        'available_col', 'is_fraud_flagged', 'applied_at'
    )
    list_filter = ('status', 'is_reseller', 'is_fraud_flagged', 'preferred_payment_method')
    search_fields = ('full_name', 'referral_code', 'phone_number', 'user__email')
    readonly_fields = (
        'referral_code', 'applied_at', 'approved_at', 'updated_at',
        'balance_pending', 'balance_approved', 'balance_paid',
        'total_withdrawn', 'withdrawal_balance_display', 'commission_summary',
    )
    fieldsets = (
        ('👤 Affiliate Info', {'fields': ('user', 'full_name', 'phone_number', 'nid_number', 'referral_code')}),
        ('📋 Application', {'fields': ('how_will_promote', 'status', 'admin_notes', 'rejection_reason')}),
        ('💳 Payment', {'fields': ('preferred_payment_method', 'payment_account_number')}),
        ('💰 Balances', {'fields': ('balance_pending', 'balance_approved', 'balance_paid', 'total_withdrawn', 'withdrawal_balance_display')}),
        ('📊 Commission Summary', {'fields': ('commission_summary',), 'classes': ('collapse',)}),
        ('⚠️ Flags', {'fields': ('is_reseller', 'is_fraud_flagged')}),
        ('🕐 Timestamps', {'fields': ('applied_at', 'approved_at', 'updated_at'), 'classes': ('collapse',)}),
    )
    actions = ['approve_affiliates', 'reject_affiliates', 'suspend_affiliates', 'clear_fraud_flags']
    inlines = [CommissionInline, WithdrawalInline, FraudFlagInline]

    def status_badge(self, obj):
        colors = {'pending': '#f59e0b', 'approved': '#10b981', 'rejected': '#ef4444', 'suspended': '#6b7280'}
        color = colors.get(obj.status, '#6b7280')
        return format_html(
            '<span style="background:{};color:white;padding:2px 10px;border-radius:12px;font-size:11px;font-weight:700;">{}</span>',
            color, obj.get_status_display()
        )
    status_badge.short_description = 'Status'

    def balance_pending_col(self, obj):
        if obj.balance_pending > 0:
            return format_html('<span style="color:#92600A;font-weight:700;">৳{}</span>', f"{obj.balance_pending:,.0f}")
        return '৳0'
    balance_pending_col.short_description = 'Pending'

    def balance_approved_col(self, obj):
        if obj.balance_approved > 0:
            return format_html('<span style="color:#065F46;font-weight:700;">৳{}</span>', f"{obj.balance_approved:,.0f}")
        return '৳0'
    balance_approved_col.short_description = 'Approved'

    def available_col(self, obj):
        bal = obj.withdrawal_balance
        if bal > 0:
            return format_html(
                '<span style="background:#D1FAE5;color:#065F46;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:700;">৳{}</span>',
                f"{bal:,.0f}"
            )
        return mark_safe('<span style="color:#999;">৳0</span>')
    available_col.short_description = 'Available'

    def withdrawal_balance_display(self, obj):
        return format_html('<strong>৳{}</strong>', f"{obj.withdrawal_balance:,.0f}")
    withdrawal_balance_display.short_description = 'Available to Withdraw'

    def commission_summary(self, obj):
        s = obj.commissions.aggregate(
            total=Count('id'),
            pending=Sum('commission_amount', filter=models.Q(status='pending')),
            approved=Sum('commission_amount', filter=models.Q(status='approved')),
            paid=Sum('commission_amount', filter=models.Q(status='paid')),
            fraud=Count('id', filter=models.Q(is_fraud_suspected=True)),
        )
        return format_html(
            '<table style="font-size:12px;">'
            '<tr><td style="padding:2px 12px 2px 0;color:#666;">Total</td><td style="font-weight:700;">{}</td></tr>'
            '<tr><td style="padding:2px 12px 2px 0;color:#92600A;">Pending</td><td style="font-weight:700;color:#92600A;">৳{}</td></tr>'
            '<tr><td style="padding:2px 12px 2px 0;color:#065F46;">Approved</td><td style="font-weight:700;color:#065F46;">৳{}</td></tr>'
            '<tr><td style="padding:2px 12px 2px 0;color:#1D4ED8;">Paid</td><td style="font-weight:700;color:#1D4ED8;">৳{}</td></tr>'
            '<tr><td style="padding:2px 12px 2px 0;color:#991B1B;">Fraud Suspected</td><td style="font-weight:700;color:#991B1B;">{}</td></tr>'
            '</table>',
            s['total'] or 0, f"{s['pending'] or 0:,.0f}",
            f"{s['approved'] or 0:,.0f}", f"{s['paid'] or 0:,.0f}", s['fraud'] or 0,
        )
    commission_summary.short_description = 'Commission Summary'

    @admin.action(description='✅ Approve selected affiliates')
    def approve_affiliates(self, request, queryset):
        n = queryset.filter(status=AffiliateProfile.STATUS_PENDING).update(
            status=AffiliateProfile.STATUS_APPROVED, approved_at=timezone.now()
        )
        self.message_user(request, f"{n} affiliate(s) approved.")

    @admin.action(description='❌ Reject selected affiliates')
    def reject_affiliates(self, request, queryset):
        n = queryset.filter(status=AffiliateProfile.STATUS_PENDING).update(status=AffiliateProfile.STATUS_REJECTED)
        self.message_user(request, f"{n} affiliate(s) rejected.")

    @admin.action(description='🚫 Suspend selected affiliates')
    def suspend_affiliates(self, request, queryset):
        n = queryset.exclude(status=AffiliateProfile.STATUS_SUSPENDED).update(status=AffiliateProfile.STATUS_SUSPENDED)
        self.message_user(request, f"{n} affiliate(s) suspended.")

    @admin.action(description='🛡️ Clear fraud flags for selected affiliates')
    def clear_fraud_flags(self, request, queryset):
        cleared = 0
        for affiliate in queryset.filter(is_fraud_flagged=True):
            affiliate.fraud_flags.filter(is_resolved=False).update(
                is_resolved=True, resolved_by=request.user, resolved_at=timezone.now()
            )
            cleared += 1
        queryset.filter(is_fraud_flagged=True).update(is_fraud_flagged=False)
        self.message_user(request, f"Fraud flags cleared for {cleared} affiliate(s).")


# ─── Commission Setting Admin ──────────────────────────────────────────────────

@admin.register(CommissionSetting)
class CommissionSettingAdmin(admin.ModelAdmin):
    list_display = ('name', 'product_id', 'commission_type', 'commission_value', 'minimum_withdrawal_amount', 'is_default', 'is_active')
    list_filter = ('commission_type', 'is_active', 'is_default')
    search_fields = ('name',)
    fieldsets = (
        ('📋 Info', {'fields': ('name', 'is_active', 'is_default')}),
        ('🎯 Scope', {'fields': ('product_id',), 'description': 'Leave blank for global default. Set Product ID for per-product rate.'}),
        ('💰 Commission', {'fields': ('commission_type', 'commission_value')}),
        ('⚙️ Withdrawal & Cookie', {'fields': ('minimum_withdrawal_amount', 'cookie_lifetime_days')}),
    )


# ─── Referral Click Admin ──────────────────────────────────────────────────────

@admin.register(ReferralClick)
class ReferralClickAdmin(admin.ModelAdmin):
    list_display = ('affiliate', 'ip_address', 'referred_user', 'landing_url_short', 'clicked_at')
    list_filter = ('clicked_at',)
    search_fields = ('affiliate__referral_code', 'ip_address')
    readonly_fields = ('clicked_at',)
    date_hierarchy = 'clicked_at'

    def landing_url_short(self, obj):
        if obj.landing_url:
            return obj.landing_url[:60] + ('...' if len(obj.landing_url) > 60 else '')
        return '—'
    landing_url_short.short_description = 'Landing URL'


# ─── Commission Admin ──────────────────────────────────────────────────────────

@admin.register(Commission)
class CommissionAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'affiliate_col', 'order_id', 'order_total_col',
        'commission_col', 'status_badge', 'fraud_badge', 'created_at'
    )
    list_filter = ('status', 'is_fraud_suspected', 'created_at')
    search_fields = ('affiliate__referral_code', 'affiliate__full_name', 'order_id')
    readonly_fields = ('created_at', 'approved_at', 'paid_at')
    actions = ['approve_commissions', 'mark_paid', 'reject_commissions']
    date_hierarchy = 'created_at'
    fieldsets = (
        ('📦 Details', {'fields': ('affiliate', 'order_id', 'order_total', 'commission_amount', 'commission_setting')}),
        ('📊 Status', {'fields': ('status', 'admin_notes')}),
        ('⚠️ Fraud', {'fields': ('is_fraud_suspected', 'fraud_reason')}),
        ('🔗 Attribution', {'fields': ('referral_click', 'referred_user'), 'classes': ('collapse',)}),
        ('🕐 Timestamps', {'fields': ('created_at', 'approved_at', 'paid_at'), 'classes': ('collapse',)}),
    )

    def affiliate_col(self, obj):
        return format_html(
            '<span style="font-weight:700;color:#A07830;">{}</span><br>'
            '<span style="font-size:11px;color:#999;">{}</span>',
            obj.affiliate.referral_code, obj.affiliate.full_name
        )
    affiliate_col.short_description = 'Affiliate'

    def order_total_col(self, obj):
        return format_html('৳{}', f"{obj.order_total:,.0f}")
    order_total_col.short_description = 'Order Total'

    def commission_col(self, obj):
        return format_html('<span style="font-weight:700;color:#065F46;">৳{}</span>', f"{obj.commission_amount:,.0f}")
    commission_col.short_description = 'Commission'

    def status_badge(self, obj):
        colors = {'pending': '#f59e0b', 'approved': '#10b981', 'paid': '#3b82f6', 'rejected': '#ef4444'}
        color = colors.get(obj.status, '#6b7280')
        return format_html(
            '<span style="background:{};color:white;padding:2px 10px;border-radius:12px;font-size:11px;font-weight:700;">{}</span>',
            color, obj.get_status_display()
        )
    status_badge.short_description = 'Status'

    def fraud_badge(self, obj):
        if obj.is_fraud_suspected:
            return format_html('<span style="background:#FEE2E2;color:#991B1B;padding:2px 8px;border-radius:8px;font-size:11px;font-weight:700;">⚠ Suspected</span>')
        return mark_safe('<span style="color:#10b981;font-size:11px;">✓ Clean</span>')
    fraud_badge.short_description = 'Fraud'

    @admin.action(description='✅ Approve selected commissions')
    def approve_commissions(self, request, queryset):
        qs = queryset.filter(status=Commission.STATUS_PENDING, is_fraud_suspected=False)
        count = 0
        for c in qs:
            c.status = Commission.STATUS_APPROVED
            c.approved_at = timezone.now()
            c.save(update_fields=['status', 'approved_at'])
            AffiliateProfile.objects.filter(pk=c.affiliate_id).update(
                balance_pending=models.F('balance_pending') - c.commission_amount,
                balance_approved=models.F('balance_approved') + c.commission_amount
            )
            count += 1
        self.message_user(request, f"{count} commission(s) approved.")

    @admin.action(description='💳 Mark selected commissions as paid')
    def mark_paid(self, request, queryset):
        qs = queryset.filter(status=Commission.STATUS_APPROVED)
        count = 0
        for c in qs:
            c.status = Commission.STATUS_PAID
            c.paid_at = timezone.now()
            c.save(update_fields=['status', 'paid_at'])
            AffiliateProfile.objects.filter(pk=c.affiliate_id).update(
                balance_approved=models.F('balance_approved') - c.commission_amount,
                balance_paid=models.F('balance_paid') + c.commission_amount
            )
            count += 1
        self.message_user(request, f"{count} commission(s) marked as paid.")

    @admin.action(description='❌ Reject selected commissions')
    def reject_commissions(self, request, queryset):
        qs = queryset.filter(status=Commission.STATUS_PENDING)
        count = 0
        for c in qs:
            c.status = Commission.STATUS_REJECTED
            c.save(update_fields=['status'])
            AffiliateProfile.objects.filter(pk=c.affiliate_id).update(
                balance_pending=models.F('balance_pending') - c.commission_amount
            )
            count += 1
        self.message_user(request, f"{count} commission(s) rejected.")


# ─── Withdrawal Request Admin ──────────────────────────────────────────────────

@admin.register(WithdrawalRequest)
class WithdrawalRequestAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'affiliate_col', 'amount_col', 'method_badge',
        'payment_account', 'status_badge', 'transaction_id',
        'requested_at', 'processed_at'
    )
    list_filter = ('status', 'payment_method', 'requested_at')
    search_fields = ('affiliate__referral_code', 'affiliate__full_name', 'payment_account', 'transaction_id')
    readonly_fields = ('requested_at', 'affiliate_balance_info')
    actions = ['approve_withdrawals', 'reject_withdrawals']
    date_hierarchy = 'requested_at'
    fieldsets = (
        ('👤 Affiliate', {'fields': ('affiliate', 'affiliate_balance_info')}),
        ('💳 Request', {'fields': ('amount', 'payment_method', 'payment_account')}),
        ('📊 Status', {'fields': ('status', 'transaction_id', 'admin_notes', 'rejection_reason')}),
        ('🕐 Timestamps', {'fields': ('requested_at', 'processed_at'), 'classes': ('collapse',)}),
    )

    def affiliate_col(self, obj):
        return format_html(
            '<span style="font-weight:700;color:#A07830;">{}</span><br>'
            '<span style="font-size:11px;color:#999;">{} · {}</span>',
            obj.affiliate.referral_code, obj.affiliate.full_name, obj.affiliate.phone_number
        )
    affiliate_col.short_description = 'Affiliate'

    def amount_col(self, obj):
        return format_html('<span style="font-weight:700;font-size:13px;color:#065F46;">৳{}</span>', f"{obj.amount:,.0f}")
    amount_col.short_description = 'Amount'

    def method_badge(self, obj):
        colors = {'bkash': '#E40584', 'nagad': '#F0681A'}
        color = colors.get(obj.payment_method, '#666')
        return format_html(
            '<span style="background:{};color:white;padding:2px 10px;border-radius:10px;font-size:11px;font-weight:700;">{}</span>',
            color, obj.get_payment_method_display()
        )
    method_badge.short_description = 'Method'

    def status_badge(self, obj):
        styles = {
            'pending':  'background:#FEF3C7;color:#92600A;border:1px solid #D4A017;',
            'approved': 'background:#D1FAE5;color:#065F46;border:1px solid #10B981;',
            'rejected': 'background:#FEE2E2;color:#991B1B;border:1px solid #EF4444;',
            'paid':     'background:#DBEAFE;color:#1D4ED8;border:1px solid #3B82F6;',
        }
        style = styles.get(obj.status, 'color:#666;')
        return format_html(
            '<span style="{}padding:2px 10px;border-radius:10px;font-size:11px;font-weight:700;">{}</span>',
            style, obj.get_status_display()
        )
    status_badge.short_description = 'Status'

    def affiliate_balance_info(self, obj):
        aff = obj.affiliate
        return format_html(
            '<table style="font-size:12px;">'
            '<tr><td style="padding:2px 16px 2px 0;color:#666;">Approved Balance</td><td style="font-weight:700;color:#065F46;">৳{}</td></tr>'
            '<tr><td style="padding:2px 16px 2px 0;color:#666;">Available to Withdraw</td><td style="font-weight:700;">৳{}</td></tr>'
            '<tr><td style="padding:2px 16px 2px 0;color:#666;">Total Withdrawn</td><td style="font-weight:700;">৳{}</td></tr>'
            '</table>',
            f"{aff.balance_approved:,.0f}", f"{aff.withdrawal_balance:,.0f}", f"{aff.total_withdrawn:,.0f}",
        )
    affiliate_balance_info.short_description = 'Affiliate Balance'

    def save_model(self, request, obj, form, change):
        if change:
            try:
                old_status = WithdrawalRequest.objects.get(pk=obj.pk).status
            except WithdrawalRequest.DoesNotExist:
                old_status = None
            super().save_model(request, obj, form, change)
            if obj.status == WithdrawalRequest.STATUS_PAID and old_status != WithdrawalRequest.STATUS_PAID:
                obj.processed_at = timezone.now()
                obj.save(update_fields=['processed_at'])
                # Only deduct when actually paid — not on approve
                AffiliateProfile.objects.filter(pk=obj.affiliate_id).update(
                    balance_approved=models.F('balance_approved') - obj.amount,
                    total_withdrawn=models.F('total_withdrawn') + obj.amount
                )
                self.message_user(request, f"✅ ৳{obj.amount:,.0f} paid to {obj.affiliate.referral_code}. Balance updated.")
            elif obj.status in (WithdrawalRequest.STATUS_APPROVED, WithdrawalRequest.STATUS_REJECTED) and old_status not in (WithdrawalRequest.STATUS_PAID,):
                obj.processed_at = timezone.now()
                obj.save(update_fields=['processed_at'])
                # No balance change on approve/reject — withdrawal_balance property
                # excludes pending+approved automatically
        else:
            super().save_model(request, obj, form, change)

    @admin.action(description='✅ Approve selected withdrawals')
    def approve_withdrawals(self, request, queryset):
        n = queryset.filter(status=WithdrawalRequest.STATUS_PENDING).update(
            status=WithdrawalRequest.STATUS_APPROVED, processed_at=timezone.now()
        )
        self.message_user(request, f"{n} withdrawal(s) approved. Enter transaction IDs and mark as Paid.")

    @admin.action(description='❌ Reject selected withdrawals')
    def reject_withdrawals(self, request, queryset):
        n = queryset.filter(status=WithdrawalRequest.STATUS_PENDING).update(
            status=WithdrawalRequest.STATUS_REJECTED, processed_at=timezone.now()
        )
        self.message_user(request, f"{n} withdrawal(s) rejected.", level=dj_messages.WARNING)


# ─── Fraud Flag Admin ──────────────────────────────────────────────────────────

@admin.register(FraudFlag)
class FraudFlagAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'affiliate_col', 'reason_badge', 'order_id',
        'ip_address', 'details_short', 'resolved_badge', 'flagged_at'
    )
    list_filter = ('reason', 'is_resolved', 'flagged_at')
    search_fields = ('affiliate__referral_code', 'affiliate__full_name', 'ip_address', 'details')
    readonly_fields = ('flagged_at', 'affiliate_fraud_summary')
    date_hierarchy = 'flagged_at'
    actions = ['resolve_flags', 'flag_affiliates', 'clear_affiliate_flags']
    fieldsets = (
        ('🚩 Details', {'fields': ('affiliate', 'affiliate_fraud_summary', 'reason', 'details')}),
        ('📦 Related', {'fields': ('order_id', 'ip_address')}),
        ('✅ Resolution', {'fields': ('is_resolved', 'resolved_by', 'resolved_at')}),
        ('🕐 Timestamps', {'fields': ('flagged_at',), 'classes': ('collapse',)}),
    )

    def affiliate_col(self, obj):
        flag = ''
        if obj.affiliate.is_fraud_flagged:
            flag = mark_safe(' <span style="background:#FEE2E2;color:#991B1B;padding:1px 5px;border-radius:4px;font-size:10px;">FLAGGED</span>')
        return format_html(
            '<span style="font-weight:700;color:#A07830;">{}</span>{}<br>'
            '<span style="font-size:11px;color:#999;">{}</span>',
            obj.affiliate.referral_code, flag, obj.affiliate.full_name
        )
    affiliate_col.short_description = 'Affiliate'

    def reason_badge(self, obj):
        colors = {
            'self_referral':          ('#FEE2E2', '#991B1B'),
            'ip_abuse':               ('#FEF3C7', '#92600A'),
            'duplicate_commission':   ('#EDE9FE', '#6D28D9'),
            'suspicious_pattern':     ('#FFEDD5', '#9A3412'),
            'manual':                 ('#F1F5F9', '#334155'),
        }
        bg, fg = colors.get(obj.reason, ('#F1F5F9', '#334155'))
        return format_html(
            '<span style="background:{};color:{};padding:2px 8px;border-radius:8px;font-size:11px;font-weight:700;white-space:nowrap;">{}</span>',
            bg, fg, obj.get_reason_display()
        )
    reason_badge.short_description = 'Reason'

    def details_short(self, obj):
        return obj.details[:80] + ('…' if len(obj.details) > 80 else '')
    details_short.short_description = 'Details'

    def resolved_badge(self, obj):
        if obj.is_resolved:
            by = obj.resolved_by.username if obj.resolved_by else '—'
            return format_html(
                '<span style="color:#065F46;font-weight:600;font-size:11px;">✓ Resolved</span><br>'
                '<span style="font-size:10px;color:#999;">by {}</span>', by
            )
        return format_html(
            '<span style="background:#FEE2E2;color:#991B1B;padding:2px 8px;border-radius:8px;font-size:11px;font-weight:700;">⚠ Open</span>'
        )
    resolved_badge.short_description = 'Status'

    def affiliate_fraud_summary(self, obj):
        aff = obj.affiliate
        open_f  = aff.fraud_flags.filter(is_resolved=False).count()
        total_f = aff.fraud_flags.count()
        sus_c   = aff.commissions.filter(is_fraud_suspected=True).count()
        return format_html(
            '<table style="font-size:12px;">'
            '<tr><td style="padding:2px 16px 2px 0;color:#666;">Open Flags</td><td style="font-weight:700;color:#991B1B;">{}</td></tr>'
            '<tr><td style="padding:2px 16px 2px 0;color:#666;">Total Flags</td><td style="font-weight:700;">{}</td></tr>'
            '<tr><td style="padding:2px 16px 2px 0;color:#666;">Suspected Commissions</td><td style="font-weight:700;color:#92600A;">{}</td></tr>'
            '<tr><td style="padding:2px 16px 2px 0;color:#666;">Account Flagged</td><td style="font-weight:700;">{}</td></tr>'
            '</table>',
            open_f, total_f, sus_c, '🔴 Yes' if aff.is_fraud_flagged else '🟢 No',
        )
    affiliate_fraud_summary.short_description = 'Fraud Summary'

    @admin.action(description='✅ Resolve selected flags')
    def resolve_flags(self, request, queryset):
        count = 0
        for flag in queryset.filter(is_resolved=False):
            flag.is_resolved = True
            flag.resolved_by = request.user
            flag.resolved_at = timezone.now()
            flag.save(update_fields=['is_resolved', 'resolved_by', 'resolved_at'])
            count += 1
        self.message_user(request, f"{count} flag(s) resolved.")

    @admin.action(description='🚩 Flag affiliate accounts for selected flags')
    def flag_affiliates(self, request, queryset):
        ids = set(queryset.values_list('affiliate_id', flat=True))
        AffiliateProfile.objects.filter(pk__in=ids).update(is_fraud_flagged=True)
        self.message_user(request, f"{len(ids)} affiliate(s) flagged.", level=dj_messages.WARNING)

    @admin.action(description='🛡️ Clear fraud flag from affiliate accounts')
    def clear_affiliate_flags(self, request, queryset):
        ids = set(queryset.values_list('affiliate_id', flat=True))
        FraudFlag.objects.filter(affiliate_id__in=ids, is_resolved=False).update(
            is_resolved=True, resolved_by=request.user, resolved_at=timezone.now()
        )
        AffiliateProfile.objects.filter(pk__in=ids).update(is_fraud_flagged=False)
        self.message_user(request, f"Fraud flags cleared for {len(ids)} affiliate(s).")


# ─── Reseller Pricing Admin ────────────────────────────────────────────────────

@admin.register(ResellerPricing)
class ResellerPricingAdmin(admin.ModelAdmin):
    list_display = ('affiliate', 'product_id', 'base_price_snapshot', 'reseller_price', 'markup_display', 'is_active', 'updated_at')
    list_filter = ('is_active',)
    search_fields = ('affiliate__referral_code', 'product_id')

    def markup_display(self, obj):
        return f"৳{obj.markup_amount} ({obj.markup_percentage}%)"
    markup_display.short_description = 'Markup'