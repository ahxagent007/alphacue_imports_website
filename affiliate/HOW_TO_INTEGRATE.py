"""
affiliate/HOW_TO_INTEGRATE.py
==============================
This file shows you exactly how to connect the commission engine
to your Order model. It is a GUIDE — not a runnable file.

You have two integration approaches. Pick the one that fits your setup.
"""


# ═══════════════════════════════════════════════════════════════════════
# APPROACH A — Trigger from your Order view (RECOMMENDED for cPanel)
# ═══════════════════════════════════════════════════════════════════════
#
# In your order management view (wherever you mark an order delivered),
# add this call:
#
#   from affiliate.services import trigger_commission_on_delivery
#
#   def mark_order_delivered(request, order_id):
#       order = get_object_or_404(Order, pk=order_id)
#       order.status = 'delivered'
#       order.save()
#
#       # Trigger commission
#       trigger_commission_on_delivery(
#           order_id      = order.pk,
#           order_total   = order.total_amount,
#           affiliate_code= order.affiliate_code,   # you stored this at checkout
#           buyer_user    = order.user,              # can be None for guests
#           referral_click= order.referral_click,    # optional ForeignKey
#       )
#       ...


# ═══════════════════════════════════════════════════════════════════════
# APPROACH B — Store affiliate info on your Order model at checkout
# ═══════════════════════════════════════════════════════════════════════
#
# Step 1: Add two fields to your Order model
# -------------------------------------------
#
#   class Order(models.Model):
#       ...
#       affiliate_code  = models.CharField(max_length=20, blank=True, default='')
#       referral_click  = models.ForeignKey(
#           'affiliate.ReferralClick',
#           null=True, blank=True,
#           on_delete=models.SET_NULL,
#       )
#
#
# Step 2: Capture affiliate at checkout (in your checkout view)
# -------------------------------------------------------------
#
#   from affiliate.services import get_affiliate_attribution
#   from affiliate.utils import clear_referral
#
#   def checkout_view(request):
#       ...
#       attribution = get_affiliate_attribution(request)
#
#       order = Order.objects.create(
#           user           = request.user,
#           total_amount   = cart_total,
#           affiliate_code = attribution['affiliate_code'] or '',
#           referral_click = attribution['referral_click'],
#           ...
#       )
#
#       # Clear the referral cookie + session after order is placed
#       response = redirect('order_success', pk=order.pk)
#       clear_referral(request, response)
#       return response
#
#
# Step 3: Trigger commission when order is delivered
# ---------------------------------------------------
#
#   from affiliate.services import trigger_commission_on_delivery
#
#   def admin_mark_delivered(request, order_id):
#       order = get_object_or_404(Order, pk=order_id)
#       order.status = 'delivered'
#       order.save()
#
#       if order.affiliate_code:
#           trigger_commission_on_delivery(
#               order_id       = order.pk,
#               order_total    = order.total_amount,
#               affiliate_code = order.affiliate_code,
#               buyer_user     = order.user,
#               referral_click = order.referral_click,
#           )


# ═══════════════════════════════════════════════════════════════════════
# APPROACH C — HTTP webhook (for external order systems)
# ═══════════════════════════════════════════════════════════════════════
#
# POST /affiliate/order-delivered/
# Header: X-Affiliate-Secret: your-secret-from-settings
# Content-Type: application/json
#
# {
#   "order_id": 1001,
#   "order_total": "2500.00",
#   "affiliate_code": "ABC12345",
#   "buyer_user_id": 42
# }
#
# Response (success):
# {
#   "success": true,
#   "commission_id": 7,
#   "affiliate_code": "ABC12345",
#   "commission_amount": "250.00",
#   "status": "pending",
#   "fraud_suspected": false
# }


# ═══════════════════════════════════════════════════════════════════════
# TESTING (Django shell)
# ═══════════════════════════════════════════════════════════════════════
#
#   python manage.py shell
#
#   from decimal import Decimal
#   from affiliate.services import trigger_commission_on_delivery
#   from affiliate.models import Commission, AffiliateProfile
#
#   # Make sure you have an approved affiliate and default CommissionSetting
#   commission = trigger_commission_on_delivery(
#       order_id=9999,
#       order_total=Decimal('1000.00'),
#       affiliate_code='YOUR_CODE_HERE',
#   )
#   print(commission)
#   # → Commission #1 — YOURCODE — ৳100.00 (Pending)
#
#   # Verify balance updated
#   aff = AffiliateProfile.objects.get(referral_code='YOUR_CODE_HERE')
#   print(aff.balance_pending)
#   # → 100.00
#
#   # Try duplicate — should return None
#   result = trigger_commission_on_delivery(
#       order_id=9999,
#       order_total=Decimal('1000.00'),
#       affiliate_code='YOUR_CODE_HERE',
#   )
#   print(result)   # → None (duplicate blocked)
