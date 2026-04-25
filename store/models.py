import uuid
from decimal import Decimal

from django.db import models
from django.utils.text import slugify
from django.urls import reverse
from django.core.validators import MinValueValidator


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _unique_slug(model_class, name, exclude_pk=None):
    base = slugify(name)
    slug = base
    n = 1
    qs = model_class.objects.all()
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    while qs.filter(slug=slug).exists():
        slug = f"{base}-{n}"
        n += 1
    return slug


def category_image_path(instance, filename):
    ext = filename.rsplit('.', 1)[-1]
    return f"categories/{instance.slug or slugify(instance.name)}.{ext}"


def product_image_path(instance, filename):
    ext = filename.rsplit('.', 1)[-1]
    return f"products/{instance.product.slug}/{uuid.uuid4().hex[:8]}.{ext}"


# ─── Site Settings ────────────────────────────────────────────────────────────

class SiteSettings(models.Model):
    """
    Singleton table — only ONE row should exist.
    Stores delivery fees and other global config.
    Admin can update these without touching code.
    """
    delivery_fee_inside_dhaka = models.DecimalField(
        max_digits=8, decimal_places=2,
        default=Decimal('60.00'),
        help_text="Delivery fee for Dhaka city (৳)"
    )
    delivery_fee_outside_dhaka = models.DecimalField(
        max_digits=8, decimal_places=2,
        default=Decimal('100.00'),
        help_text="Delivery fee for outside Dhaka (৳)"
    )
    free_delivery_threshold = models.DecimalField(
        max_digits=8, decimal_places=2,
        default=Decimal('0.00'),
        help_text="Free delivery for orders above this amount (0 = disabled)"
    )
    site_name = models.CharField(max_length=100, default='AlphaCue Imports')
    contact_phone = models.CharField(max_length=20, blank=True)
    contact_email = models.CharField(max_length=100, blank=True)
    address = models.TextField(blank=True)

    class Meta:
        verbose_name = 'Site Settings'
        verbose_name_plural = 'Site Settings'

    def __str__(self):
        return 'Site Settings'

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def get_delivery_fee(self, is_inside_dhaka=True):
        if self.free_delivery_threshold > 0:
            return self  # handled in checkout with order total check
        return self.delivery_fee_inside_dhaka if is_inside_dhaka else self.delivery_fee_outside_dhaka


# ─── Category ─────────────────────────────────────────────────────────────────

class Category(models.Model):
    """
    Single-level categories — fully manageable from admin.
    No parent/child hierarchy.
    """
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, unique=True, blank=True, db_index=True)
    description = models.TextField(blank=True)
    image = models.ImageField(
        upload_to=category_image_path,
        blank=True, null=True,
        help_text="Category banner/thumbnail image"
    )
    is_active = models.BooleanField(default=True, db_index=True)
    sort_order = models.PositiveSmallIntegerField(
        default=0,
        help_text="Lower number = shown first"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Category'
        verbose_name_plural = 'Categories'
        ordering = ['sort_order', 'name']

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _unique_slug(Category, self.name, self.pk)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('store:category_detail', kwargs={'slug': self.slug})

    def active_product_count(self):
        return self.products.filter(is_active=True).count()


# ─── Product ──────────────────────────────────────────────────────────────────

class Product(models.Model):
    """
    Core product. Price lives on ProductVariant.
    A product must have at least one variant.
    """
    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        related_name='products',
    )
    name = models.CharField(max_length=220)
    slug = models.SlugField(max_length=250, unique=True, blank=True, db_index=True)
    sku = models.CharField(
        max_length=60, unique=True, blank=True,
        help_text="Auto-generated if left blank"
    )
    description = models.TextField(blank=True)
    short_description = models.CharField(
        max_length=300, blank=True,
        help_text="Shown on product listing cards"
    )

    is_active = models.BooleanField(default=True, db_index=True)
    is_featured = models.BooleanField(
        default=False, db_index=True,
        help_text="Show on homepage featured section"
    )

    # SEO
    meta_title = models.CharField(max_length=200, blank=True)
    meta_description = models.CharField(max_length=320, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Product'
        verbose_name_plural = 'Products'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['category', 'is_active']),
            models.Index(fields=['is_featured', 'is_active']),
        ]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _unique_slug(Product, self.name, self.pk)
        if not self.sku:
            self.sku = f"AC-{uuid.uuid4().hex[:8].upper()}"
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('store:product_detail', kwargs={'slug': self.slug})

    @property
    def primary_image(self):
        img = self.images.filter(is_primary=True).first()
        if not img:
            img = self.images.first()
        return img

    @property
    def primary_image_url(self):
        img = self.primary_image
        if img and img.image:
            return img.image.url
        return None

    @property
    def default_variant(self):
        """Return the first active variant — used for price display on listing pages."""
        return self.variants.filter(is_active=True).order_by('sort_order', 'id').first()

    @property
    def min_price(self):
        """Lowest price across active variants."""
        result = self.variants.filter(is_active=True).aggregate(
            min_p=models.Min('price')
        )
        return result['min_p'] or Decimal('0.00')

    @property
    def max_price(self):
        result = self.variants.filter(is_active=True).aggregate(
            max_p=models.Max('price')
        )
        return result['max_p'] or Decimal('0.00')

    @property
    def is_in_stock(self):
        return self.variants.filter(is_active=True, stock__gt=0).exists()

    @property
    def display_price(self):
        """Price string for listing cards."""
        mn = self.min_price
        mx = self.max_price
        if mn == mx:
            return f"৳{mn:,.0f}"
        return f"৳{mn:,.0f} – ৳{mx:,.0f}"


# ─── Product Variant ──────────────────────────────────────────────────────────

class ProductVariant(models.Model):
    """
    Each variant is a purchasable SKU (e.g. Red / Large).
    Stores its own price, compare price, and stock.
    Variant attributes are free-form key-value pairs stored on VariantAttribute.
    """
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name='variants',
    )
    name = models.CharField(
        max_length=150,
        help_text="e.g. 'Red / Large' or 'Blue 128GB' — shown to buyer"
    )
    sku = models.CharField(
        max_length=80, unique=True, blank=True,
        help_text="Auto-generated if left blank"
    )
    price = models.DecimalField(
        max_digits=10, decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))],
        help_text="Selling price in ৳"
    )
    compare_price = models.DecimalField(
        max_digits=10, decimal_places=2,
        null=True, blank=True,
        help_text="Crossed-out original price (optional)"
    )
    stock = models.PositiveIntegerField(default=0)
    track_stock = models.BooleanField(
        default=True,
        help_text="Uncheck to allow unlimited orders"
    )
    is_active = models.BooleanField(default=True, db_index=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        verbose_name = 'Product Variant'
        verbose_name_plural = 'Product Variants'
        ordering = ['sort_order', 'id']
        indexes = [
            models.Index(fields=['product', 'is_active']),
        ]

    def save(self, *args, **kwargs):
        if not self.sku:
            base_sku = self.product.sku if self.product_id else 'VAR'
            self.sku = f"{base_sku}-{uuid.uuid4().hex[:4].upper()}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.product.name} — {self.name}"

    @property
    def is_available(self):
        if not self.is_active:
            return False
        if self.track_stock and self.stock <= 0:
            return False
        return True

    @property
    def discount_percentage(self):
        if self.compare_price and self.compare_price > self.price:
            return int(((self.compare_price - self.price) / self.compare_price) * 100)
        return 0


# ─── Variant Attribute ────────────────────────────────────────────────────────

class VariantAttribute(models.Model):
    """
    Free-form key-value attributes per variant.
    e.g. key='Color', value='Red'
         key='Size',  value='XL'
    Displayed as attribute pills on the product page.
    """
    variant = models.ForeignKey(
        ProductVariant,
        on_delete=models.CASCADE,
        related_name='attributes',
    )
    key = models.CharField(max_length=50, help_text="e.g. Color, Size, Storage")
    value = models.CharField(max_length=100, help_text="e.g. Red, XL, 128GB")

    class Meta:
        verbose_name = 'Variant Attribute'
        verbose_name_plural = 'Variant Attributes'
        ordering = ['key', 'value']
        unique_together = [['variant', 'key']]

    def __str__(self):
        return f"{self.key}: {self.value}"


# ─── Product Image ────────────────────────────────────────────────────────────

class ProductImage(models.Model):
    """
    Multiple images per product. One marked as primary.
    Can also be linked to a specific variant.
    """
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name='images',
    )
    variant = models.ForeignKey(
        ProductVariant,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='images',
        help_text="Link image to a specific variant (optional)"
    )
    image = models.ImageField(upload_to=product_image_path)
    alt_text = models.CharField(
        max_length=200, blank=True,
        help_text="Accessibility alt text for this image"
    )
    is_primary = models.BooleanField(
        default=False,
        help_text="Primary image shown on listing cards"
    )
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        verbose_name = 'Product Image'
        verbose_name_plural = 'Product Images'
        ordering = ['-is_primary', 'sort_order']

    def save(self, *args, **kwargs):
        # Ensure only one primary image per product
        if self.is_primary:
            ProductImage.objects.filter(
                product=self.product, is_primary=True
            ).exclude(pk=self.pk).update(is_primary=False)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Image for {self.product.name} {'(Primary)' if self.is_primary else ''}"


# ─── Order ────────────────────────────────────────────────────────────────────

class Order(models.Model):
    STATUS_PENDING    = 'pending'
    STATUS_CONFIRMED  = 'confirmed'
    STATUS_SHIPPED    = 'shipped'
    STATUS_DELIVERED  = 'delivered'
    STATUS_CANCELLED  = 'cancelled'

    STATUS_CHOICES = [
        (STATUS_PENDING,   'Pending'),
        (STATUS_CONFIRMED, 'Confirmed'),
        (STATUS_SHIPPED,   'Shipped'),
        (STATUS_DELIVERED, 'Delivered'),
        (STATUS_CANCELLED, 'Cancelled'),
    ]

    ZONE_INSIDE  = 'inside'
    ZONE_OUTSIDE = 'outside'
    ZONE_CHOICES = [
        (ZONE_INSIDE,  'Inside Dhaka'),
        (ZONE_OUTSIDE, 'Outside Dhaka'),
    ]

    # ── Customer info (guest-friendly — no User FK required) ──────────────
    user = models.ForeignKey(
        'auth.User',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='orders',
    )
    customer_name    = models.CharField(max_length=150)
    customer_phone   = models.CharField(max_length=20)
    customer_email   = models.EmailField(blank=True)

    # ── Delivery address ──────────────────────────────────────────────────
    address_line     = models.CharField(max_length=300, help_text="House / Road / Area")
    city             = models.CharField(max_length=100, default='Dhaka')
    delivery_zone    = models.CharField(max_length=10, choices=ZONE_CHOICES, default=ZONE_INSIDE)
    delivery_note    = models.TextField(blank=True, help_text="Any special instruction for delivery")

    # ── Pricing ───────────────────────────────────────────────────────────
    subtotal         = models.DecimalField(max_digits=10, decimal_places=2)
    delivery_fee     = models.DecimalField(max_digits=8,  decimal_places=2)
    grand_total      = models.DecimalField(max_digits=10, decimal_places=2)

    # ── Status ────────────────────────────────────────────────────────────
    status           = models.CharField(max_length=15, choices=STATUS_CHOICES,
                                        default=STATUS_PENDING, db_index=True)

    # ── Affiliate attribution ─────────────────────────────────────────────
    affiliate_code   = models.CharField(max_length=20, blank=True, default='',
                                        help_text="Referral code captured at checkout")
    referral_click   = models.ForeignKey(
        'affiliate.ReferralClick',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='orders',
    )

    # ── Internal ──────────────────────────────────────────────────────────
    order_number     = models.CharField(max_length=20, unique=True, blank=True, db_index=True)
    admin_notes      = models.TextField(blank=True)
    created_at       = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at       = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name         = 'Order'
        verbose_name_plural  = 'Orders'
        ordering             = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'created_at']),
            models.Index(fields=['customer_phone']),
        ]

    def save(self, *args, **kwargs):
        if not self.order_number:
            import random, string
            while True:
                num = 'AC-' + ''.join(random.choices(string.digits, k=6))
                if not Order.objects.filter(order_number=num).exists():
                    self.order_number = num
                    break
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Order {self.order_number} — {self.customer_name} ({self.get_status_display()})"

    @property
    def is_inside_dhaka(self):
        return self.delivery_zone == self.ZONE_INSIDE

    def trigger_commission(self):
        """Call after status changes to delivered. Returns Commission or None."""
        if not self.affiliate_code:
            return None
        from affiliate.services import trigger_commission_on_delivery
        return trigger_commission_on_delivery(
            order_id       = self.pk,
            order_total    = self.grand_total,
            affiliate_code = self.affiliate_code,
            buyer_user     = self.user,
            referral_click = self.referral_click,
        )


class OrderItem(models.Model):
    """One row per variant per order."""
    order     = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items')
    variant   = models.ForeignKey(
        'store.ProductVariant',
        on_delete=models.PROTECT,
        related_name='order_items',
    )
    product_name  = models.CharField(max_length=220)
    variant_name  = models.CharField(max_length=150)
    sku           = models.CharField(max_length=80)
    unit_price    = models.DecimalField(max_digits=10, decimal_places=2)
    quantity      = models.PositiveIntegerField()

    class Meta:
        verbose_name        = 'Order Item'
        verbose_name_plural = 'Order Items'

    def __str__(self):
        return f"{self.product_name} ({self.variant_name}) x{self.quantity}"

    @property
    def line_total(self):
        if self.unit_price is None or self.quantity is None:
            return 0
        return self.unit_price * self.quantity