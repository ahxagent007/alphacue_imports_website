"""
store/cart_views.py
-------------------
All cart-related views kept separate from product views for clarity.
"""

import json
from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse
from django.views.decorators.http import require_POST, require_GET
from django.views.decorators.csrf import csrf_exempt

from .cart import Cart
from .models import ProductVariant, SiteSettings


# ─── Cart Page ────────────────────────────────────────────────────────────────

def cart_detail(request):
    """
    GET /cart/
    Full cart page showing all items, delivery zone toggle, and totals.
    """
    cart = Cart(request)
    settings = SiteSettings.get()

    # Delivery zone: stored in session, defaults to inside Dhaka
    is_inside_dhaka = request.session.get('delivery_inside_dhaka', True)
    delivery_fee = cart.get_delivery_fee(is_inside_dhaka)
    total = cart.subtotal + delivery_fee

    ctx = {
        'cart': cart,
        'cart_items': list(cart),
        'is_inside_dhaka': is_inside_dhaka,
        'delivery_fee': delivery_fee,
        'subtotal': cart.subtotal,
        'grand_total': total,
        'site_settings': settings,
        'categories': _get_categories(),
    }
    return render(request, 'store/cart.html', ctx)


# ─── Add to Cart ──────────────────────────────────────────────────────────────

@require_POST
def cart_add(request):
    """
    POST /cart/add/
    Accepts JSON: { variant_id, quantity }
    Returns JSON: { success, cart_count, message }
    """
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, Exception):
        data = request.POST

    variant_id = data.get('variant_id')
    quantity   = int(data.get('quantity', 1))

    if not variant_id:
        return JsonResponse({'success': False, 'error': 'variant_id required'}, status=400)

    variant = get_object_or_404(ProductVariant, pk=variant_id, is_active=True)

    if not variant.is_available:
        return JsonResponse({'success': False, 'error': 'This variant is out of stock'}, status=400)

    if quantity < 1:
        return JsonResponse({'success': False, 'error': 'Quantity must be at least 1'}, status=400)

    cart = Cart(request)
    cart.add(variant, quantity=quantity)

    return JsonResponse({
        'success':    True,
        'cart_count': len(cart),
        'message':    f"'{variant.product.name}' added to cart",
        'subtotal':   str(cart.subtotal),
    })


# ─── Update Quantity ──────────────────────────────────────────────────────────

@require_POST
def cart_update(request):
    """
    POST /cart/update/
    Accepts JSON: { variant_id, quantity }
    quantity=0 removes the item.
    """
    try:
        data = json.loads(request.body)
    except Exception:
        data = request.POST

    variant_id = data.get('variant_id')
    quantity   = int(data.get('quantity', 0))

    if not variant_id:
        return JsonResponse({'success': False, 'error': 'variant_id required'}, status=400)

    cart = Cart(request)
    cart.update_quantity(variant_id, quantity)

    is_inside_dhaka = request.session.get('delivery_inside_dhaka', True)

    return JsonResponse({
        'success':      True,
        'cart_count':   len(cart),
        'subtotal':     str(cart.subtotal),
        'delivery_fee': str(cart.get_delivery_fee(is_inside_dhaka)),
        'grand_total':  str(cart.get_total(is_inside_dhaka)),
    })


# ─── Remove Item ──────────────────────────────────────────────────────────────

@require_POST
def cart_remove(request):
    """
    POST /cart/remove/
    Accepts JSON: { variant_id }
    """
    try:
        data = json.loads(request.body)
    except Exception:
        data = request.POST

    variant_id = data.get('variant_id')
    if not variant_id:
        return JsonResponse({'success': False, 'error': 'variant_id required'}, status=400)

    cart = Cart(request)
    cart.remove(variant_id)

    is_inside_dhaka = request.session.get('delivery_inside_dhaka', True)

    return JsonResponse({
        'success':      True,
        'cart_count':   len(cart),
        'subtotal':     str(cart.subtotal),
        'delivery_fee': str(cart.get_delivery_fee(is_inside_dhaka)),
        'grand_total':  str(cart.get_total(is_inside_dhaka)),
        'cart_empty':   len(cart) == 0,
    })


# ─── Set Delivery Zone ────────────────────────────────────────────────────────

@require_POST
def set_delivery_zone(request):
    """
    POST /cart/delivery-zone/
    Accepts JSON: { inside_dhaka: true|false }
    Updates session and returns new delivery fee + total.
    """
    try:
        data = json.loads(request.body)
    except Exception:
        data = request.POST

    is_inside = str(data.get('inside_dhaka', 'true')).lower() in ('true', '1', 'yes')
    request.session['delivery_inside_dhaka'] = is_inside

    cart = Cart(request)
    delivery_fee = cart.get_delivery_fee(is_inside)
    grand_total  = cart.subtotal + delivery_fee

    return JsonResponse({
        'success':      True,
        'inside_dhaka': is_inside,
        'delivery_fee': str(delivery_fee),
        'grand_total':  str(grand_total),
        'delivery_fee_fmt': f"৳{delivery_fee:,.0f}",
        'grand_total_fmt':  f"৳{grand_total:,.0f}",
    })


# ─── Shared helper ────────────────────────────────────────────────────────────

def _get_categories():
    from .models import Category
    return Category.objects.filter(is_active=True).order_by('sort_order', 'name')
