from django.contrib import admin
from django.utils.html import format_html, mark_safe
from django.db.models import Sum
from django import forms
try:
    from ckeditor.widgets import CKEditorWidget
    CKEDITOR_AVAILABLE = True
except ImportError:
    CKEDITOR_AVAILABLE = False

from .models import (
    SiteSettings, Category, Product,
    ProductVariant, VariantAttribute, ProductImage
)
from affiliate.models import CommissionSetting


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
        return obj.products.filter(is_active=True).count()
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


# ─── Commission Inline (inside Product admin) ─────────────────────────────────

# ─── Commission inline helper — shown inside Product admin ───────────────────
# CommissionSetting has no FK to Product (uses product_id integer field),
# so we build a standalone form rendered via fieldsets rather than an inline.
# The commission form is injected into ProductAdmin.get_form() instead.

class ProductCommissionForm(forms.ModelForm):
    commission_type = forms.ChoiceField(
        choices=CommissionSetting.COMMISSION_TYPE_CHOICES,
        widget=forms.Select(attrs={'style': 'width:220px'}),
        label='Commission Type',
    )
    commission_value = forms.DecimalField(
        max_digits=6, decimal_places=2,
        min_value=0.01,
        widget=forms.NumberInput(attrs={
            'style': 'width:140px', 'step': '0.01',
            'min': '0.01', 'placeholder': 'e.g. 10',
        }),
        label='Commission Value',
        help_text='Percentage (e.g. 10 = 10%) or flat BDT amount.',
    )

    class Meta:
        model = CommissionSetting
        fields = ['commission_type', 'commission_value']


# ─── Product Admin Form ───────────────────────────────────────────────────────

class ProductAdminForm(forms.ModelForm):
    if CKEDITOR_AVAILABLE:
        description = forms.CharField(
            widget=CKEditorWidget(config_name='default'),
            required=False,
        )

    # Commission fields — not on the Product model, handled in save_model
    _commission_type = forms.ChoiceField(
        choices=[('', '--- Select ---')] + list(CommissionSetting.COMMISSION_TYPE_CHOICES),
        required=False,
        label='Commission Type',
        widget=forms.Select(attrs={'style': 'width:220px'}),
    )
    _commission_value = forms.DecimalField(
        max_digits=6, decimal_places=2,
        required=False,
        label='Commission Value',
        help_text='e.g. 10 for 10% or 150 for ৳150 flat. Leave blank to skip.',
        widget=forms.NumberInput(attrs={
            'style': 'width:160px', 'step': '0.01', 'min': '0.01',
            'placeholder': 'e.g. 10',
        }),
    )

    class Meta:
        model = Product
        fields = '__all__'


# ─── Product Admin ────────────────────────────────────────────────────────────

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    form = ProductAdminForm
    list_display = (
        'primary_image_thumb', 'name', 'category',
        'price_range_display', 'stock_display',
        'commission_rate_display',
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

    # Base fieldsets — commission is added dynamically via get_fieldsets()
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

    def get_fieldsets(self, request, obj=None):
        """Dynamically append the Commission Rate section."""
        base = list(self.fieldsets)
        commission_fieldset = (
            '💰 Commission Rate',
            {
                'fields': ('_commission_type', '_commission_value'),
                'description': (
                    'Set the affiliate commission for this product. '
                    'Affiliates will NOT earn unless a rate is set here. '
                    'This overrides the global default commission rate.'
                ),
            }
        )
        # Insert before Timestamps
        base.insert(-1, commission_fieldset)
        return base

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
                color, f"{total} units"
            )
        except Exception:
            return '—'
    stock_display.short_description = 'Total Stock'

    def commission_rate_display(self, obj):
        try:
            setting = CommissionSetting.objects.filter(
                product_id=obj.pk, is_active=True
            ).first()
            if setting:
                symbol = '%' if setting.commission_type == 'percentage' else '৳'
                return format_html(
                    '<span style="background:#D4EDDA;color:#155724;padding:2px 8px;'
                    'border-radius:10px;font-size:11px;font-weight:700;">'
                    '{}{}</span>',
                    setting.commission_value, symbol
                )
            return format_html(
                '<span style="background:#F8D7DA;color:#721C24;padding:2px 8px;'
                'border-radius:10px;font-size:11px;font-weight:700;">'
                '⚠ Not Set</span>'
            )
        except Exception:
            return mark_safe('<span style="color:#666;">—</span>')
    commission_rate_display.short_description = 'Commission'

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        # Pre-fill commission fields when editing an existing product
        if obj:
            existing = CommissionSetting.objects.filter(
                product_id=obj.pk, is_active=True
            ).first()
            if existing:
                form.base_fields['_commission_type'].initial = existing.commission_type
                form.base_fields['_commission_value'].initial = existing.commission_value
        return form

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # Save commission setting from injected fields
        comm_type  = form.cleaned_data.get('_commission_type', '').strip()
        comm_value = form.cleaned_data.get('_commission_value')
        if comm_type and comm_value:
            existing = CommissionSetting.objects.filter(product_id=obj.pk).first()
            if existing:
                existing.commission_type  = comm_type
                existing.commission_value = comm_value
                existing.is_active        = True
                existing.save(update_fields=['commission_type', 'commission_value', 'is_active'])
            else:
                CommissionSetting.objects.create(
                    name=f"{obj.name} — Commission Rate",
                    product_id=obj.pk,
                    commission_type=comm_type,
                    commission_value=comm_value,
                    is_active=True,
                    is_default=False,
                )

    def response_add(self, request, obj, post_url_continue=None):
        response = super().response_add(request, obj, post_url_continue)
        if not CommissionSetting.objects.filter(product_id=obj.pk, is_active=True).exists():
            self.message_user(
                request,
                (
                    f"⚠️ Product '{obj.name}' was saved but has NO commission rate set. "
                    "Edit the product and fill in the '💰 Commission Rate' section — "
                    "affiliates will NOT earn commission on this product until you do."
                ),
                level='warning',
            )
        return response

    def response_change(self, request, obj):
        response = super().response_change(request, obj)
        if not CommissionSetting.objects.filter(product_id=obj.pk, is_active=True).exists():
            self.message_user(
                request,
                (
                    f"⚠️ '{obj.name}' still has no commission rate. "
                    "Affiliates will NOT earn on this product."
                ),
                level='warning',
            )
        return response

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
    model = OrderItem
    fields = ('product_name', 'variant_name', 'sku', 'unit_price', 'quantity', 'line_total_display')
    readonly_fields = ('product_name', 'variant_name', 'sku', 'unit_price', 'quantity', 'line_total_display')
    extra = 0
    can_delete = False

    def line_total_display(self, obj):
        return f"৳{obj.line_total:,.0f}"
    line_total_display.short_description = 'Line Total'


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        'order_number', 'customer_name', 'customer_phone',
        'grand_total_display', 'status_badge',
        'delivery_zone', 'affiliate_code', 'created_at'
    )
    list_filter = ('status', 'delivery_zone', 'created_at')
    search_fields = ('order_number', 'customer_name', 'customer_phone', 'affiliate_code')
    readonly_fields = (
        'order_number', 'subtotal', 'delivery_fee', 'grand_total',
        'affiliate_code', 'referral_click', 'created_at', 'updated_at',
    )
    date_hierarchy = 'created_at'
    inlines = [OrderItemInline]
    actions = ['mark_confirmed', 'mark_shipped', 'mark_delivered', 'mark_cancelled']
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
        ('💰 Commission Rate', {
            'fields': ('_commission_type', '_commission_value'),
            'description': (
                'Set the affiliate commission for this product. '
                'Affiliates will NOT earn unless a rate is set here. '
                'This overrides the global default commission rate.'
            ),
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

    def save_model(self, request, obj, form, change):
        if change:
            try:
                old_obj = Order.objects.get(pk=obj.pk)
                was_delivered = old_obj.status == Order.STATUS_DELIVERED
            except Order.DoesNotExist:
                was_delivered = False
            super().save_model(request, obj, form, change)
            if obj.status == Order.STATUS_DELIVERED and not was_delivered:
                obj.trigger_commission()
        else:
            super().save_model(request, obj, form, change)

    def _change_status(self, request, queryset, new_status, label):
        from django.contrib import messages as dj_messages

        if new_status == Order.STATUS_DELIVERED:
            if not CommissionSetting.get_default():
                self.message_user(
                    request,
                    "⚠️ WARNING: No default CommissionSetting found. "
                    "Affiliate commissions will NOT be created. "
                    "Go to Admin > Commission Settings and create one with is_default=True.",
                    level=dj_messages.WARNING,
                )

        updated = 0
        comm_created = 0
        affiliate_orders = 0

        for order in queryset.exclude(status=new_status):
            order.status = new_status
            order.updated_at = timezone.now()
            order.save(update_fields=['status', 'updated_at'])

            if new_status == Order.STATUS_DELIVERED:
                if order.affiliate_code:
                    affiliate_orders += 1
                    commission = order.trigger_commission()
                    if commission:
                        comm_created += 1
            updated += 1

        msg = f"{updated} order(s) marked as {label}."
        if new_status == Order.STATUS_DELIVERED:
            if affiliate_orders > 0:
                msg += f" {affiliate_orders} had affiliate code(s)."
            if comm_created > 0:
                msg += f" ✅ {comm_created} commission(s) created."
            elif affiliate_orders > 0:
                msg += " ⚠️ No commissions created — check CommissionSetting configuration."
        self.message_user(request, msg)

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