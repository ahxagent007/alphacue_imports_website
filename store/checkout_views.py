from decimal import Decimal
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.db import transaction
from django.conf import settings as dj_settings

from .cart import Cart
from .forms import CheckoutForm
from .models import Order, OrderItem, SiteSettings, Category
from affiliate.utils import clear_referral
from affiliate.models import AffiliateProfile, ReferralClick

COOKIE_NAME  = getattr(dj_settings, 'AFFILIATE_COOKIE_NAME', 'alphacue_ref')
SESSION_KEY  = getattr(dj_settings, 'AFFILIATE_SESSION_KEY', 'affiliate_referral_code')


def _resolve_affiliate_at_checkout(request):
    """
    Resolve affiliate at checkout time.
    Reads directly from session and cookie — does NOT rely on request.affiliate
    (which is set by middleware on GET requests only, not POST).
    Returns (affiliate_code, referral_click) or (None, None).
    """
    # Read code from session first, then cookie
    code = (
        request.session.get(SESSION_KEY, '').strip().upper()
        or request.COOKIES.get(COOKIE_NAME, '').strip().upper()
    )

    if not code:
        return '', None

    # Validate the code
    try:
        affiliate = AffiliateProfile.objects.get(
            referral_code=code,
            status=AffiliateProfile.STATUS_APPROVED,
            is_fraud_flagged=False,
        )
    except AffiliateProfile.DoesNotExist:
        return '', None

    # Self-referral guard
    if request.user.is_authenticated and request.user == affiliate.user:
        return '', None

    # Find most recent referral click for attribution
    referral_click = None
    try:
        session_key = request.session.session_key or ''
        if session_key:
            referral_click = (
                ReferralClick.objects
                .filter(affiliate=affiliate, session_key=session_key)
                .order_by('-clicked_at')
                .first()
            )
        if not referral_click:
            from django.utils import timezone
            cutoff = timezone.now() - timezone.timedelta(days=30)
            referral_click = (
                ReferralClick.objects
                .filter(affiliate=affiliate, clicked_at__gte=cutoff)
                .order_by('-clicked_at')
                .first()
            )
    except Exception:
        pass

    return code, referral_click


def checkout(request):
    cart = Cart(request)

    if not cart:
        messages.warning(request, "Your cart is empty.")
        return redirect('store:product_list')

    is_inside_dhaka = request.session.get('delivery_inside_dhaka', True)
    settings_obj    = SiteSettings.get()
    delivery_fee    = cart.get_delivery_fee(is_inside_dhaka)
    subtotal        = cart.subtotal
    grand_total     = subtotal + delivery_fee

    if request.method == 'POST':
        form = CheckoutForm(request.POST)
        if form.is_valid():
            return _place_order(request, form, cart, settings_obj)
    else:
        initial_zone = Order.ZONE_INSIDE if is_inside_dhaka else Order.ZONE_OUTSIDE
        form = CheckoutForm(initial={'delivery_zone': initial_zone})

    ctx = {
        'form':            form,
        'cart':            cart,
        'cart_items':      list(cart),
        'subtotal':        subtotal,
        'delivery_fee':    delivery_fee,
        'grand_total':     grand_total,
        'is_inside_dhaka': is_inside_dhaka,
        'site_settings':   settings_obj,
        'categories':      Category.objects.filter(is_active=True).order_by('sort_order'),
    }
    return render(request, 'store/checkout.html', ctx)


@transaction.atomic
def _place_order(request, form, cart, settings_obj):
    order = form.save(commit=False)

    is_inside    = form.cleaned_data['delivery_zone'] == Order.ZONE_INSIDE
    request.session['delivery_inside_dhaka'] = is_inside

    subtotal     = cart.subtotal
    delivery_fee = cart.get_delivery_fee(is_inside)
    grand_total  = subtotal + delivery_fee

    order.subtotal     = subtotal
    order.delivery_fee = delivery_fee
    order.grand_total  = grand_total
    order.user         = request.user if request.user.is_authenticated else None

    # ── Affiliate attribution — read directly from session/cookie ──────────
    affiliate_code, referral_click = _resolve_affiliate_at_checkout(request)
    order.affiliate_code = affiliate_code
    order.referral_click = referral_click

    order.save()

    # ── Create order items + deduct stock ──────────────────────────────────
    from .models import ProductVariant
    for item in cart:
        try:
            variant = ProductVariant.objects.select_for_update().get(pk=item['variant_id'])
        except ProductVariant.DoesNotExist:
            continue

        OrderItem.objects.create(
            order        = order,
            variant      = variant,
            product_name = item['product_name'],
            variant_name = item['variant_name'],
            sku          = item['sku'],
            unit_price   = Decimal(item['price']),
            quantity     = item['quantity'],
        )

        if variant.track_stock:
            variant.stock = max(0, variant.stock - item['quantity'])
            variant.save(update_fields=['stock'])

    # ── Store order in session BEFORE clearing anything ────────────────────
    placed_orders = request.session.get('placed_orders', [])
    placed_orders.append(order.order_number)
    request.session['placed_orders'] = placed_orders[-20:]
    request.session.modified = True

    # ── Clear cart and referral ────────────────────────────────────────────
    cart.clear()
    response = redirect('store:order_confirmation', order_number=order.order_number)
    clear_referral(request, response)

    return response


def order_confirmation(request, order_number):
    order = get_object_or_404(Order, order_number=order_number)

    placed_orders = request.session.get('placed_orders', [])
    is_owner  = request.user.is_authenticated and order.user == request.user
    in_session = order_number in placed_orders

    if not in_session and not is_owner and not request.user.is_staff:
        return redirect('store:homepage')

    if order_number not in placed_orders:
        placed_orders.append(order_number)
        request.session['placed_orders'] = placed_orders[-20:]

    ctx = {
        'order':         order,
        'items':         order.items.select_related('variant__product').all(),
        'categories':    Category.objects.filter(is_active=True).order_by('sort_order'),
        'site_settings': SiteSettings.get(),
    }
    return render(request, 'store/order_confirmation.html', ctx)