"""
store/order_views.py
---------------------
EC4 — Order Management

Customer-facing:
    /orders/track/          — look up order by number + phone
    /orders/<order_number>/ — order detail / tracking page

Staff-facing:
    /manage/orders/                         — order list with filters
    /manage/orders/<order_number>/          — order detail + status change
    /manage/orders/<order_number>/status/   — AJAX status update
"""

import json
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.admin.views.decorators import staff_member_required
from django.views.decorators.http import require_POST
from django.http import JsonResponse
from django.db.models import Q, Sum, Count
from django.contrib import messages
from django.core.paginator import Paginator

from .models import Order, OrderItem, Category, SiteSettings


def _base_ctx():
    return {
        'categories':    Category.objects.filter(is_active=True).order_by('sort_order'),
        'site_settings': SiteSettings.get(),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  CUSTOMER FACING
# ══════════════════════════════════════════════════════════════════════════════

def order_track(request):
    """
    GET/POST /orders/track/
    Customer enters order number + phone to look up their order.
    Works for guests (no login needed).
    """
    order = None
    error = None

    if request.method == 'POST':
        order_number = request.POST.get('order_number', '').strip().upper()
        phone        = request.POST.get('phone', '').strip()

        if order_number and phone:
            try:
                order = Order.objects.prefetch_related('items').get(
                    order_number=order_number,
                    customer_phone=phone,
                )
            except Order.DoesNotExist:
                error = "No order found with that order number and phone number. Please check and try again."
        else:
            error = "Please enter both your order number and phone number."

    ctx = _base_ctx()
    ctx.update({'order': order, 'error': error})
    return render(request, 'store/order_track.html', ctx)


def order_detail_customer(request, order_number):
    """
    GET /orders/<order_number>/
    Customer order detail — only accessible if they placed it this session,
    or if they provide correct phone via GET param.
    """
    order = get_object_or_404(Order, order_number=order_number)

    # Access control: session-placed order OR phone verification
    placed_orders = request.session.get('placed_orders', [])
    phone_param   = request.GET.get('phone', '').strip()
    is_staff      = request.user.is_staff

    if not is_staff:
        if order_number not in placed_orders:
            if not phone_param or phone_param != order.customer_phone:
                return redirect('store:order_track')

    items = order.items.select_related('variant__product').all()
    ctx   = _base_ctx()
    ctx.update({'order': order, 'items': items})
    return render(request, 'store/order_detail_customer.html', ctx)


# ══════════════════════════════════════════════════════════════════════════════
#  STAFF / OWNER FACING
# ══════════════════════════════════════════════════════════════════════════════

@staff_member_required
def manage_order_list(request):
    """
    GET /manage/orders/
    Paginated order list with status filter, search, and summary stats.
    """
    qs = Order.objects.prefetch_related('items').order_by('-created_at')

    # ── Filters ──────────────────────────────────────────────────────────
    status_filter = request.GET.get('status', '')
    search        = request.GET.get('q', '').strip()
    zone_filter   = request.GET.get('zone', '')

    if status_filter:
        qs = qs.filter(status=status_filter)
    if zone_filter:
        qs = qs.filter(delivery_zone=zone_filter)
    if search:
        qs = qs.filter(
            Q(order_number__icontains=search) |
            Q(customer_name__icontains=search) |
            Q(customer_phone__icontains=search)
        )

    # ── Stats (unfiltered) ────────────────────────────────────────────────
    all_orders  = Order.objects.all()
    stats = {
        'total':     all_orders.count(),
        'pending':   all_orders.filter(status=Order.STATUS_PENDING).count(),
        'confirmed': all_orders.filter(status=Order.STATUS_CONFIRMED).count(),
        'shipped':   all_orders.filter(status=Order.STATUS_SHIPPED).count(),
        'delivered': all_orders.filter(status=Order.STATUS_DELIVERED).count(),
        'cancelled': all_orders.filter(status=Order.STATUS_CANCELLED).count(),
        'revenue':   all_orders.filter(status=Order.STATUS_DELIVERED).aggregate(
                         t=Sum('grand_total'))['t'] or 0,
    }

    # ── Pagination ────────────────────────────────────────────────────────
    paginator   = Paginator(qs, 25)
    page_number = request.GET.get('page', 1)
    page_obj    = paginator.get_page(page_number)

    ctx = _base_ctx()
    ctx.update({
        'page_obj':      page_obj,
        'stats':         stats,
        'status_filter': status_filter,
        'zone_filter':   zone_filter,
        'search':        search,
        'status_choices': Order.STATUS_CHOICES,
        'zone_choices':   Order.ZONE_CHOICES,
    })
    return render(request, 'store/manage_order_list.html', ctx)


@staff_member_required
def manage_order_detail(request, order_number):
    """
    GET  /manage/orders/<order_number>/ — view order + change status form
    POST /manage/orders/<order_number>/ — update status + admin notes
    """
    order = get_object_or_404(Order, order_number=order_number)
    items = order.items.select_related('variant__product').all()

    if request.method == 'POST':
        new_status  = request.POST.get('status', '').strip()
        admin_notes = request.POST.get('admin_notes', '').strip()
        valid_statuses = [s[0] for s in Order.STATUS_CHOICES]

        if new_status and new_status in valid_statuses and new_status != order.status:
            old_status   = order.status
            order.status = new_status
            if admin_notes:
                order.admin_notes = admin_notes
            order.save(update_fields=['status', 'admin_notes', 'updated_at'])

            # Trigger commission on delivery
            if new_status == Order.STATUS_DELIVERED:
                order.trigger_commission()
                messages.success(
                    request,
                    f"Order {order.order_number} marked as Delivered. "
                    f"Commission triggered{'.' if order.affiliate_code else ' (no affiliate).'}"
                )
            else:
                messages.success(
                    request,
                    f"Order {order.order_number} status updated: "
                    f"{old_status.title()} → {new_status.title()}"
                )
        elif admin_notes and admin_notes != order.admin_notes:
            order.admin_notes = admin_notes
            order.save(update_fields=['admin_notes', 'updated_at'])
            messages.success(request, "Admin notes saved.")

        return redirect('store:manage_order_detail', order_number=order_number)

    ctx = _base_ctx()
    ctx.update({
        'order':          order,
        'items':          items,
        'status_choices': Order.STATUS_CHOICES,
    })
    return render(request, 'store/manage_order_detail.html', ctx)


@staff_member_required
@require_POST
def manage_order_status_ajax(request, order_number):
    """
    POST /manage/orders/<order_number>/status/
    AJAX status change — used by the order list quick-action buttons.
    """
    order      = get_object_or_404(Order, order_number=order_number)
    new_status = request.POST.get('status', '').strip()
    valid      = [s[0] for s in Order.STATUS_CHOICES]

    if new_status not in valid:
        return JsonResponse({'success': False, 'error': 'Invalid status'}, status=400)

    old_status   = order.status
    order.status = new_status
    order.save(update_fields=['status', 'updated_at'])

    if new_status == Order.STATUS_DELIVERED:
        order.trigger_commission()

    return JsonResponse({
        'success':    True,
        'old_status': old_status,
        'new_status': new_status,
        'label':      order.get_status_display(),
    })