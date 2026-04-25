from django.contrib import admin
from django.utils.html import format_html, mark_safe
from django.db.models import Sum

from .models import (
    SiteSettings, Category, Product,
    ProductVariant, VariantAttribute, ProductImage
)


# ─── Site Settings ────────────────────────────────────────────────────────────

@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    fieldsets = (
        ('🏪 Store Info', {
            'fields': ('site_name', 'contact_phone', 'contact_email', 'address')
        }),
        ('🚚 Delivery Fees', {
            'fields': (
                'delivery_fee_inside_dhaka',
                'delivery_fee_outside_dhaka',
                'free_delivery_threshold',
            )
        }),
    )

    def has_add_permission(self, request):
        return not SiteSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


# ─── Category ─────────────────────────────────────────────────────────────────

@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = (
        'name', 'slug', 'product_count_display',
        'sort_order', 'is_active', 'image_preview'
    )
    list_filter = ('is_active',)
    search_fields = ('name', 'slug')
    prepopulated_fields = {'slug': ('name',)}
    list_editable = ('sort_order', 'is_active')
    ordering = ['sort_order', 'name']

    def product_count_display(self, obj):
        count = obj.products.filter(is_active=True).count()
        return count
    product_count_display.short_description = 'Active Products'

    def image_preview(self, obj):
        if obj.image:
            return format_html(
                '<img src="{}" style="height:40px;border-radius:4px;">',
                obj.image.url
            )
        return mark_safe('<span style="color:#666;">—</span>')
    image_preview.short_description = 'Image'


# ─── Inlines ──────────────────────────────────────────────────────────────────

class VariantAttributeInline(admin.TabularInline):
    model = VariantAttribute
    extra = 2
    fields = ('key', 'value')


class ProductVariantInline(admin.StackedInline):
    model = ProductVariant
    extra = 1
    fields = (
        ('name', 'sku'),
        ('price', 'compare_price'),
        ('stock', 'track_stock'),
        ('is_active', 'sort_order'),
    )
    show_change_link = True


class ProductImageInline(admin.TabularInline):
    model = ProductImage
    extra = 1
    fields = ('image', 'image_preview_thumb', 'alt_text', 'variant', 'is_primary', 'sort_order')
    readonly_fields = ('image_preview_thumb',)

    def image_preview_thumb(self, obj):
        if obj.pk and obj.image:
            return format_html(
                '<img src="{}" style="height:50px;border-radius:4px;">',
                obj.image.url
            )
        return mark_safe('<span style="color:#666;">—</span>')
    image_preview_thumb.short_description = 'Preview'


# ─── Product ──────────────────────────────────────────────────────────────────

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = (
        'primary_image_thumb', 'name', 'category',
        'price_range_display', 'stock_display',
        'is_active', 'is_featured', 'created_at'
    )
    list_filter = ('category', 'is_active', 'is_featured', 'created_at')
    search_fields = ('name', 'sku', 'slug')
    prepopulated_fields = {'slug': ('name',)}
    list_editable = ('is_active', 'is_featured')
    readonly_fields = ('sku', 'created_at', 'updated_at', 'primary_image_large')
    date_hierarchy = 'created_at'
    inlines = [ProductVariantInline, ProductImageInline]
    actions = ['activate_products', 'deactivate_products', 'mark_featured', 'unmark_featured']

    fieldsets = (
        ('📦 Product Info', {
            'fields': (
                'category', 'name', 'slug', 'sku',
                'short_description', 'description',
            )
        }),
        ('🔍 SEO', {
            'fields': ('meta_title', 'meta_description'),
            'classes': ('collapse',),
        }),
        ('⚙️ Status', {
            'fields': ('is_active', 'is_featured'),
        }),
        ('🖼️ Primary Image Preview', {
            'fields': ('primary_image_large',),
        }),
        ('🕐 Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

    def primary_image_thumb(self, obj):
        try:
            url = obj.primary_image_url
            if url:
                return format_html(
                    '<img src="{}" style="height:44px;width:44px;'
                    'object-fit:cover;border-radius:6px;">',
                    url
                )
        except Exception:
            pass
        return mark_safe('<span style="color:#666;font-size:11px;">No image</span>')
    primary_image_thumb.short_description = ''

    def primary_image_large(self, obj):
        try:
            url = obj.primary_image_url
            if url:
                return format_html(
                    '<img src="{}" style="max-height:200px;border-radius:8px;">',
                    url
                )
        except Exception:
            pass
        return 'No image uploaded yet.'
    primary_image_large.short_description = 'Current Primary Image'

    def price_range_display(self, obj):
        try:
            return obj.display_price
        except Exception:
            return '—'
    price_range_display.short_description = 'Price'

    def stock_display(self, obj):
        try:
            total = obj.variants.filter(is_active=True).aggregate(
                total=Sum('stock')
            )['total'] or 0
            color = '#10b981' if total > 0 else '#ef4444'
            return format_html(
                '<span style="color:{}"><b>{}</b></span>',
                color,
                f"{total} units"
            )
        except Exception:
            return '—'
    stock_display.short_description = 'Total Stock'

    @admin.action(description='✅ Activate selected products')
    def activate_products(self, request, queryset):
        updated = queryset.update(is_active=True)
        self.message_user(request, f"{updated} product(s) activated.")

    @admin.action(description='❌ Deactivate selected products')
    def deactivate_products(self, request, queryset):
        updated = queryset.update(is_active=False)
        self.message_user(request, f"{updated} product(s) deactivated.")

    @admin.action(description='⭐ Mark as featured')
    def mark_featured(self, request, queryset):
        updated = queryset.update(is_featured=True)
        self.message_user(request, f"{updated} product(s) marked as featured.")

    @admin.action(description='Remove from featured')
    def unmark_featured(self, request, queryset):
        updated = queryset.update(is_featured=False)
        self.message_user(request, f"{updated} product(s) removed from featured.")


# ─── Standalone ProductVariant admin ─────────────────────────────────────────

@admin.register(ProductVariant)
class ProductVariantAdmin(admin.ModelAdmin):
    list_display = (
        'product', 'name', 'sku',
        'price', 'compare_price', 'stock',
        'is_active', 'discount_display'
    )
    list_filter = ('is_active', 'track_stock', 'product__category')
    search_fields = ('product__name', 'name', 'sku')
    list_editable = ('price', 'stock', 'is_active')
    readonly_fields = ('sku',)
    inlines = [VariantAttributeInline]

    def discount_display(self, obj):
        try:
            pct = obj.discount_percentage
            if pct:
                return format_html(
                    '<span style="background:#2E7D52;color:white;'
                    'padding:2px 6px;border-radius:4px;font-size:11px">'
                    '{}% OFF</span>',
                    pct
                )
        except Exception:
            pass
        return mark_safe('<span style="color:#666;">—</span>')
    discount_display.short_description = 'Discount'


# ─── Order Admin ─────────────────────────────────────────────────────────────

from .models import Order, OrderItem
from django.utils import timezone


class OrderItemInline(admin.TabularInline):
    model         = OrderItem
    fields        = ('product_name', 'variant_name', 'sku', 'unit_price', 'quantity', 'line_total_display')
    readonly_fields = ('product_name', 'variant_name', 'sku', 'unit_price', 'quantity', 'line_total_display')
    extra         = 0
    can_delete    = False

    def line_total_display(self, obj):
        return f"৳{obj.line_total:,.0f}"
    line_total_display.short_description = 'Line Total'


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display  = (
        'order_number', 'customer_name', 'customer_phone',
        'grand_total_display', 'status_badge',
        'delivery_zone', 'affiliate_code', 'created_at'
    )
    list_filter   = ('status', 'delivery_zone', 'created_at')
    search_fields = ('order_number', 'customer_name', 'customer_phone', 'affiliate_code')
    readonly_fields = (
        'order_number', 'subtotal', 'delivery_fee', 'grand_total',
        'affiliate_code', 'referral_click', 'created_at', 'updated_at',
    )
    date_hierarchy = 'created_at'
    inlines       = [OrderItemInline]
    actions       = [
        'mark_confirmed', 'mark_shipped',
        'mark_delivered', 'mark_cancelled',
    ]
    fieldsets = (
        ('📦 Order Info', {
            'fields': ('order_number', 'status', 'admin_notes')
        }),
        ('👤 Customer', {
            'fields': ('user', 'customer_name', 'customer_phone', 'customer_email')
        }),
        ('🚚 Delivery', {
            'fields': ('delivery_zone', 'address_line', 'city', 'delivery_note')
        }),
        ('💰 Pricing', {
            'fields': ('subtotal', 'delivery_fee', 'grand_total')
        }),
        ('🔗 Affiliate', {
            'fields': ('affiliate_code', 'referral_click'),
            'classes': ('collapse',),
        }),
        ('🕐 Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

    def grand_total_display(self, obj):
        return format_html('<b>৳{}</b>', f"{obj.grand_total:,.0f}")
    grand_total_display.short_description = 'Total'

    def status_badge(self, obj):
        colors = {
            'pending':   '#f59e0b',
            'confirmed': '#3b82f6',
            'shipped':   '#8b5cf6',
            'delivered': '#10b981',
            'cancelled': '#ef4444',
        }
        color = colors.get(obj.status, '#6b7280')
        return format_html(
            '<span style="background:{};color:white;padding:2px 10px;'
            'border-radius:12px;font-size:11px;font-weight:700;">{}</span>',
            color, obj.get_status_display()
        )
    status_badge.short_description = 'Status'

    def _change_status(self, request, queryset, new_status, label):
        updated = 0
        for order in queryset.exclude(status=new_status):
            order.status = new_status
            order.save(update_fields=['status', 'updated_at'])
            if new_status == Order.STATUS_DELIVERED:
                order.trigger_commission()
            updated += 1
        self.message_user(request, f"{updated} order(s) marked as {label}.")

    @admin.action(description='✅ Mark as Confirmed')
    def mark_confirmed(self, request, queryset):
        self._change_status(request, queryset, Order.STATUS_CONFIRMED, 'Confirmed')

    @admin.action(description='🚚 Mark as Shipped')
    def mark_shipped(self, request, queryset):
        self._change_status(request, queryset, Order.STATUS_SHIPPED, 'Shipped')

    @admin.action(description='📦 Mark as Delivered — triggers commission')
    def mark_delivered(self, request, queryset):
        self._change_status(request, queryset, Order.STATUS_DELIVERED, 'Delivered')

    @admin.action(description='❌ Mark as Cancelled')
    def mark_cancelled(self, request, queryset):
        self._change_status(request, queryset, Order.STATUS_CANCELLED, 'Cancelled')
