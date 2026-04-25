from decimal import Decimal
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.db import transaction
from django.views.decorators.http import require_http_methods

from .cart import Cart
from .forms import CheckoutForm
from .models import Order, OrderItem, SiteSettings, Category
from affiliate.utils import get_affiliate_from_request, clear_referral
from affiliate.services import get_affiliate_attribution


def checkout(request):
    """
    GET  /checkout/  — show checkout form pre-filled with cart + delivery fee
    POST /checkout/  — validate form, create Order + OrderItems, clear cart
    """
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
        # Pre-select delivery zone from session
        initial_zone = Order.ZONE_INSIDE if is_inside_dhaka else Order.ZONE_OUTSIDE
        form = CheckoutForm(initial={'delivery_zone': initial_zone})

    ctx = {
        'form':          form,
        'cart':          cart,
        'cart_items':    list(cart),
        'subtotal':      subtotal,
        'delivery_fee':  delivery_fee,
        'grand_total':   grand_total,
        'is_inside_dhaka': is_inside_dhaka,
        'site_settings': settings_obj,
        'categories':    Category.objects.filter(is_active=True).order_by('sort_order'),
    }
    return render(request, 'store/checkout.html', ctx)


@transaction.atomic
def _place_order(request, form, cart, settings_obj):
    """
    Atomically:
      1. Create Order row
      2. Create OrderItem rows (and decrement stock)
      3. Attach affiliate attribution
      4. Clear cart + referral cookie
      5. Redirect to confirmation
    """
    order = form.save(commit=False)

    # Recalculate totals server-side (never trust client)
    is_inside = form.cleaned_data['delivery_zone'] == Order.ZONE_INSIDE
    # Sync session zone to match form selection
    request.session['delivery_inside_dhaka'] = is_inside

    subtotal     = cart.subtotal
    delivery_fee = cart.get_delivery_fee(is_inside)
    grand_total  = subtotal + delivery_fee

    order.subtotal    = subtotal
    order.delivery_fee = delivery_fee
    order.grand_total  = grand_total
    order.user         = request.user if request.user.is_authenticated else None

    # ── Affiliate attribution ──────────────────────────────────────────────
    attribution = get_affiliate_attribution(request)
    order.affiliate_code = attribution.get('affiliate_code') or ''
    order.referral_click = attribution.get('referral_click')

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

        # Deduct stock if tracked
        if variant.track_stock:
            variant.stock = max(0, variant.stock - item['quantity'])
            variant.save(update_fields=['stock'])

    # ── Clear cart and referral ────────────────────────────────────────────
    cart.clear()
    response = redirect('store:order_confirmation', order_number=order.order_number)
    clear_referral(request, response)

    return response


def order_confirmation(request, order_number):
    """
    GET /order/confirmation/<order_number>/
    Thank-you page shown after successful checkout.
    """
    order = get_object_or_404(Order, order_number=order_number)

    # Security: only the session that placed the order or staff can view it
    # We store placed order numbers in session for guest access
    placed_orders = request.session.get('placed_orders', [])
    if order_number not in placed_orders:
        if not request.user.is_staff:
            if not request.user.is_authenticated or order.user != request.user:
                return redirect('store:homepage')
        # Add to session for this user
    placed_orders.append(order_number)
    request.session['placed_orders'] = placed_orders[-20:]  # keep last 20

    ctx = {
        'order':      order,
        'items':      order.items.select_related('variant__product').all(),
        'categories': Category.objects.filter(is_active=True).order_by('sort_order'),
        'site_settings': SiteSettings.get(),
    }
    return render(request, 'store/order_confirmation.html', ctx)
