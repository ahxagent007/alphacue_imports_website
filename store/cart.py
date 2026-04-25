"""
store/cart.py
-------------
Session-based cart engine.

Cart structure stored in request.session['cart']:
{
    "<variant_id>": {
        "variant_id":   int,
        "product_id":   int,
        "product_name": str,
        "variant_name": str,
        "price":        str,   # Decimal stored as string
        "image_url":    str | None,
        "quantity":     int,
        "sku":          str,
    },
    ...
}

All keys are strings because Django sessions serialize to JSON.
"""

from decimal import Decimal

CART_SESSION_KEY = 'cart'


class Cart:
    def __init__(self, request):
        self.session = request.session
        cart = self.session.get(CART_SESSION_KEY)
        if cart is None:
            cart = {}
            self.session[CART_SESSION_KEY] = cart
        self.cart = cart

    # ─── Internals ────────────────────────────────────────────────────────

    def _save(self):
        self.session.modified = True

    def _key(self, variant_id):
        return str(variant_id)

    # ─── Public API ───────────────────────────────────────────────────────

    def add(self, variant, quantity=1, override_quantity=False):
        """
        Add a variant to the cart or update its quantity.
        variant: ProductVariant instance
        """
        from .models import ProductVariant

        key = self._key(variant.pk)

        # Get the best available image
        image_url = None
        try:
            # Try variant-specific image first
            vi = variant.images.first()
            if vi:
                image_url = vi.image.url
            else:
                pi = variant.product.images.filter(is_primary=True).first() \
                     or variant.product.images.first()
                if pi:
                    image_url = pi.image.url
        except Exception:
            pass

        if key not in self.cart:
            self.cart[key] = {
                'variant_id':   variant.pk,
                'product_id':   variant.product_id,
                'product_name': variant.product.name,
                'variant_name': variant.name,
                'price':        str(variant.price),
                'image_url':    image_url,
                'quantity':     0,
                'sku':          variant.sku,
                'slug':         variant.product.slug,
            }

        if override_quantity:
            self.cart[key]['quantity'] = int(quantity)
        else:
            self.cart[key]['quantity'] += int(quantity)

        # Never exceed available stock
        if variant.track_stock:
            self.cart[key]['quantity'] = min(
                self.cart[key]['quantity'],
                variant.stock
            )

        # Remove if quantity dropped to 0
        if self.cart[key]['quantity'] <= 0:
            self.remove(variant.pk)
        else:
            self._save()

    def remove(self, variant_id):
        key = self._key(variant_id)
        if key in self.cart:
            del self.cart[key]
            self._save()

    def update_quantity(self, variant_id, quantity):
        """Set quantity directly. Removes item if quantity <= 0."""
        key = self._key(variant_id)
        if key not in self.cart:
            return
        quantity = int(quantity)
        if quantity <= 0:
            self.remove(variant_id)
        else:
            self.cart[key]['quantity'] = quantity
            self._save()

    def clear(self):
        self.session[CART_SESSION_KEY] = {}
        self.cart = self.session[CART_SESSION_KEY]
        self._save()

    # ─── Computed properties ──────────────────────────────────────────────

    def __iter__(self):
        """Yield enriched cart item dicts."""
        for key, item in self.cart.items():
            price = Decimal(item['price'])
            qty   = item['quantity']
            yield {
                **item,
                'price_decimal':    price,
                'line_total':       price * qty,
                'line_total_fmt':   f"৳{price * qty:,.0f}",
                'price_fmt':        f"৳{price:,.0f}",
            }

    def __len__(self):
        """Total number of individual units in cart."""
        return sum(item['quantity'] for item in self.cart.values())

    def __bool__(self):
        return bool(self.cart)

    @property
    def subtotal(self):
        return sum(
            Decimal(item['price']) * item['quantity']
            for item in self.cart.values()
        )

    @property
    def item_count(self):
        """Number of distinct line items."""
        return len(self.cart)

    def get_delivery_fee(self, is_inside_dhaka=True):
        from .models import SiteSettings
        settings = SiteSettings.get()
        threshold = settings.free_delivery_threshold
        if threshold and self.subtotal >= threshold:
            return Decimal('0.00')
        if is_inside_dhaka:
            return settings.delivery_fee_inside_dhaka
        return settings.delivery_fee_outside_dhaka

    def get_total(self, is_inside_dhaka=True):
        return self.subtotal + self.get_delivery_fee(is_inside_dhaka)
