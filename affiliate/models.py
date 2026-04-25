import string
import random
from decimal import Decimal

from django.db import models
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.core.validators import MinValueValidator, MaxValueValidator

User = get_user_model()


def generate_referral_code(length=8):
    """Generate a unique alphanumeric referral code."""
    chars = string.ascii_uppercase + string.digits
    while True:
        code = ''.join(random.choices(chars, k=length))
        if not AffiliateProfile.objects.filter(referral_code=code).exists():
            return code


class AffiliateProfile(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_APPROVED = 'approved'
    STATUS_REJECTED = 'rejected'
    STATUS_SUSPENDED = 'suspended'

    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending Review'),
        (STATUS_APPROVED, 'Approved'),
        (STATUS_REJECTED, 'Rejected'),
        (STATUS_SUSPENDED, 'Suspended'),
    ]

    PAYMENT_BKASH = 'bkash'
    PAYMENT_NAGAD = 'nagad'

    PAYMENT_METHOD_CHOICES = [
        (PAYMENT_BKASH, 'bKash'),
        (PAYMENT_NAGAD, 'Nagad'),
    ]

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='affiliate_profile'
    )
    referral_code = models.CharField(max_length=20, unique=True, db_index=True, blank=True)
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES,
        default=STATUS_PENDING, db_index=True
    )
    full_name = models.CharField(max_length=150)
    phone_number = models.CharField(max_length=20)
    nid_number = models.CharField(max_length=30, blank=True)
    how_will_promote = models.TextField()
    preferred_payment_method = models.CharField(
        max_length=10, choices=PAYMENT_METHOD_CHOICES, default=PAYMENT_BKASH
    )
    payment_account_number = models.CharField(max_length=30, blank=True)

    balance_pending = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    balance_approved = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    balance_paid = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    total_withdrawn = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))

    is_reseller = models.BooleanField(default=False)
    is_fraud_flagged = models.BooleanField(default=False, db_index=True)
    admin_notes = models.TextField(blank=True)
    rejection_reason = models.TextField(blank=True)

    applied_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Affiliate Profile"
        verbose_name_plural = "Affiliate Profiles"
        ordering = ['-applied_at']
        indexes = [
            models.Index(fields=['status', 'is_fraud_flagged']),
        ]

    def save(self, *args, **kwargs):
        if not self.referral_code:
            self.referral_code = generate_referral_code()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.full_name} ({self.referral_code}) — {self.get_status_display()}"

    @property
    def withdrawal_balance(self):
        pending_total = self.withdrawal_requests.filter(
            status=WithdrawalRequest.STATUS_PENDING
        ).aggregate(total=models.Sum('amount'))['total'] or Decimal('0.00')
        return self.balance_approved - pending_total

    def get_referral_url(self, request=None):
        from django.urls import reverse
        path = reverse('affiliate:referral_redirect', kwargs={'code': self.referral_code})
        if request:
            return request.build_absolute_uri(path)
        return path


class CommissionSetting(models.Model):
    COMMISSION_PERCENTAGE = 'percentage'
    COMMISSION_FLAT = 'flat'

    COMMISSION_TYPE_CHOICES = [
        (COMMISSION_PERCENTAGE, 'Percentage (%)'),
        (COMMISSION_FLAT, 'Flat Amount (৳)'),
    ]

    name = models.CharField(max_length=100)
    commission_type = models.CharField(
        max_length=15, choices=COMMISSION_TYPE_CHOICES, default=COMMISSION_PERCENTAGE
    )
    commission_value = models.DecimalField(
        max_digits=6, decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01')), MaxValueValidator(Decimal('100.00'))]
    )
    minimum_withdrawal_amount = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal('500.00')
    )
    cookie_lifetime_days = models.PositiveIntegerField(default=30)
    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Commission Setting"
        verbose_name_plural = "Commission Settings"

    def __str__(self):
        symbol = '%' if self.commission_type == self.COMMISSION_PERCENTAGE else '৳'
        return f"{self.name}: {self.commission_value}{symbol}"

    @classmethod
    def get_default(cls):
        return cls.objects.filter(is_default=True, is_active=True).first()

    def calculate_commission(self, order_amount):
        if self.commission_type == self.COMMISSION_PERCENTAGE:
            return (order_amount * self.commission_value / Decimal('100')).quantize(Decimal('0.01'))
        return min(self.commission_value, order_amount)


class ReferralClick(models.Model):
    affiliate = models.ForeignKey(
        AffiliateProfile, on_delete=models.CASCADE, related_name='clicks'
    )
    ip_address = models.GenericIPAddressField(db_index=True)
    user_agent = models.TextField(blank=True)
    referred_user = models.ForeignKey(
        User, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='referral_clicks_received'
    )
    landing_url = models.URLField(max_length=500, blank=True)
    session_key = models.CharField(max_length=40, blank=True, db_index=True)
    clicked_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        verbose_name = "Referral Click"
        verbose_name_plural = "Referral Clicks"
        ordering = ['-clicked_at']
        indexes = [
            models.Index(fields=['affiliate', 'ip_address']),
            models.Index(fields=['affiliate', 'clicked_at']),
        ]

    def __str__(self):
        return f"Click for {self.affiliate.referral_code} from {self.ip_address} at {self.clicked_at:%Y-%m-%d %H:%M}"


class Commission(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_APPROVED = 'approved'
    STATUS_PAID = 'paid'
    STATUS_REJECTED = 'rejected'

    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_APPROVED, 'Approved'),
        (STATUS_PAID, 'Paid'),
        (STATUS_REJECTED, 'Rejected'),
    ]

    affiliate = models.ForeignKey(
        AffiliateProfile, on_delete=models.CASCADE, related_name='commissions'
    )
    order_id = models.PositiveIntegerField(db_index=True)
    order_total = models.DecimalField(max_digits=10, decimal_places=2)
    commission_amount = models.DecimalField(max_digits=10, decimal_places=2)
    commission_setting = models.ForeignKey(
        CommissionSetting, null=True, blank=True, on_delete=models.SET_NULL
    )
    status = models.CharField(
        max_length=15, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True
    )
    is_fraud_suspected = models.BooleanField(default=False, db_index=True)
    fraud_reason = models.CharField(max_length=255, blank=True)
    referral_click = models.ForeignKey(
        ReferralClick, null=True, blank=True, on_delete=models.SET_NULL
    )
    referred_user = models.ForeignKey(
        User, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='commissions_generated'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    admin_notes = models.TextField(blank=True)

    class Meta:
        verbose_name = "Commission"
        verbose_name_plural = "Commissions"
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['affiliate', 'order_id'],
                name='unique_commission_per_order'
            )
        ]
        indexes = [
            models.Index(fields=['affiliate', 'status']),
            models.Index(fields=['status', 'is_fraud_suspected']),
        ]

    def __str__(self):
        return f"Commission #{self.pk} — {self.affiliate.referral_code} — ৳{self.commission_amount} ({self.get_status_display()})"


class WithdrawalRequest(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_APPROVED = 'approved'
    STATUS_REJECTED = 'rejected'
    STATUS_PAID = 'paid'

    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_APPROVED, 'Approved'),
        (STATUS_REJECTED, 'Rejected'),
        (STATUS_PAID, 'Paid'),
    ]

    PAYMENT_BKASH = 'bkash'
    PAYMENT_NAGAD = 'nagad'

    PAYMENT_METHOD_CHOICES = [
        (PAYMENT_BKASH, 'bKash'),
        (PAYMENT_NAGAD, 'Nagad'),
    ]

    affiliate = models.ForeignKey(
        AffiliateProfile, on_delete=models.CASCADE, related_name='withdrawal_requests'
    )
    amount = models.DecimalField(
        max_digits=10, decimal_places=2,
        validators=[MinValueValidator(Decimal('1.00'))]
    )
    payment_method = models.CharField(max_length=10, choices=PAYMENT_METHOD_CHOICES)
    payment_account = models.CharField(max_length=30)
    status = models.CharField(
        max_length=15, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True
    )
    transaction_id = models.CharField(max_length=100, blank=True)
    admin_notes = models.TextField(blank=True)
    rejection_reason = models.TextField(blank=True)
    requested_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Withdrawal Request"
        verbose_name_plural = "Withdrawal Requests"
        ordering = ['-requested_at']
        indexes = [
            models.Index(fields=['affiliate', 'status']),
        ]

    def __str__(self):
        return f"Withdrawal #{self.pk} — {self.affiliate.referral_code} — ৳{self.amount} ({self.get_status_display()})"


class ResellerPricing(models.Model):
    affiliate = models.ForeignKey(
        AffiliateProfile, on_delete=models.CASCADE, related_name='reseller_prices'
    )
    product_id = models.PositiveIntegerField(db_index=True)
    base_price_snapshot = models.DecimalField(max_digits=10, decimal_places=2)
    reseller_price = models.DecimalField(
        max_digits=10, decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))]
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Reseller Pricing"
        verbose_name_plural = "Reseller Pricing"
        constraints = [
            models.UniqueConstraint(
                fields=['affiliate', 'product_id'],
                name='unique_reseller_price_per_product'
            )
        ]
        indexes = [
            models.Index(fields=['affiliate', 'is_active']),
        ]

    def __str__(self):
        return f"Reseller price by {self.affiliate.referral_code} for product #{self.product_id}: ৳{self.reseller_price}"

    @property
    def markup_amount(self):
        return self.reseller_price - self.base_price_snapshot

    @property
    def markup_percentage(self):
        if self.base_price_snapshot:
            return ((self.markup_amount / self.base_price_snapshot) * 100).quantize(Decimal('0.1'))
        return Decimal('0.0')


class FraudFlag(models.Model):
    REASON_SELF_REFERRAL = 'self_referral'
    REASON_IP_ABUSE = 'ip_abuse'
    REASON_DUPLICATE_COMMISSION = 'duplicate_commission'
    REASON_SUSPICIOUS_PATTERN = 'suspicious_pattern'
    REASON_MANUAL = 'manual'

    REASON_CHOICES = [
        (REASON_SELF_REFERRAL, 'Self-Referral Attempt'),
        (REASON_IP_ABUSE, 'IP Address Abuse'),
        (REASON_DUPLICATE_COMMISSION, 'Duplicate Commission Attempt'),
        (REASON_SUSPICIOUS_PATTERN, 'Suspicious Order Pattern'),
        (REASON_MANUAL, 'Manually Flagged by Admin'),
    ]

    affiliate = models.ForeignKey(
        AffiliateProfile, on_delete=models.CASCADE, related_name='fraud_flags'
    )
    reason = models.CharField(max_length=30, choices=REASON_CHOICES, db_index=True)
    details = models.TextField()
    order_id = models.PositiveIntegerField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    is_resolved = models.BooleanField(default=False)
    resolved_by = models.ForeignKey(
        User, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='resolved_fraud_flags'
    )
    flagged_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Fraud Flag"
        verbose_name_plural = "Fraud Flags"
        ordering = ['-flagged_at']
        indexes = [
            models.Index(fields=['affiliate', 'is_resolved']),
            models.Index(fields=['reason', 'is_resolved']),
        ]

    def __str__(self):
        return f"FraudFlag #{self.pk} — {self.affiliate.referral_code} — {self.get_reason_display()}"
