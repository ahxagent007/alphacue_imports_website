import json
from django.shortcuts import render, get_object_or_404
from django.db.models import Min, Prefetch

from .models import Category, Product, ProductVariant, ProductImage, SiteSettings

SORT_OPTIONS = [
    ('newest',     'Newest'),
    ('oldest',     'Oldest'),
    ('price_asc',  'Price ↑'),
    ('price_desc', 'Price ↓'),
]


def _base_context():
    return {
        'categories': Category.objects.filter(is_active=True).order_by('sort_order', 'name'),
        'site_settings': SiteSettings.get(),
        'sort_options': SORT_OPTIONS,
    }


def _product_qs():
    """
    Base queryset — uses 'price_sort' annotation to avoid
    conflicting with the Product.min_price @property.
    """
    return (
        Product.objects
        .filter(is_active=True)
        .prefetch_related(
            Prefetch('images', queryset=ProductImage.objects.filter(is_primary=True)),
            Prefetch('variants', queryset=ProductVariant.objects.filter(is_active=True)),
        )
        .annotate(price_sort=Min('variants__price'))
    )


def _sort_products(qs, sort):
    sort_map = {'newest': '-created_at', 'oldest': 'created_at'}
    if sort == 'price_asc':
        return qs.order_by('price_sort')
    elif sort == 'price_desc':
        return qs.order_by('-price_sort')
    return qs.order_by(sort_map.get(sort, '-created_at'))


def homepage(request):
    featured = _product_qs().filter(is_featured=True).order_by('-created_at')[:12]
    latest   = _product_qs().order_by('-created_at')[:16]
    ctx = _base_context()
    ctx.update({'featured_products': featured, 'latest_products': latest})
    return render(request, 'store/homepage.html', ctx)


def product_list(request):
    sort = request.GET.get('sort', 'newest')
    category_slug = request.GET.get('category', '')
    qs = _product_qs()
    if category_slug:
        qs = qs.filter(category__slug=category_slug)
    qs = _sort_products(qs, sort)
    selected_category = None
    if category_slug:
        selected_category = Category.objects.filter(slug=category_slug, is_active=True).first()
    ctx = _base_context()
    ctx.update({
        'products': qs,
        'current_sort': sort,
        'selected_category': selected_category,
        'selected_category_slug': category_slug,
        'total_count': qs.count(),
    })
    return render(request, 'store/product_list.html', ctx)


def category_detail(request, slug):
    category = get_object_or_404(Category, slug=slug, is_active=True)
    sort = request.GET.get('sort', 'newest')
    qs = _product_qs().filter(category=category)
    qs = _sort_products(qs, sort)
    ctx = _base_context()
    ctx.update({
        'category': category,
        'products': qs,
        'current_sort': sort,
        'total_count': qs.count(),
    })
    return render(request, 'store/category_detail.html', ctx)


def product_detail(request, slug):
    product = get_object_or_404(
        Product.objects.prefetch_related(
            'images',
            Prefetch(
                'variants',
                queryset=ProductVariant.objects.filter(is_active=True).prefetch_related('attributes'),
            ),
        ),
        slug=slug, is_active=True
    )

    variants = list(product.variants.filter(is_active=True).prefetch_related('attributes'))
    images   = list(product.images.all())

    variant_data = {}
    for v in variants:
        attrs = {a.key: a.value for a in v.attributes.all()}
        variant_data[v.pk] = {
            'id':            v.pk,
            'name':          v.name,
            'price':         float(v.price),
            'compare_price': float(v.compare_price) if v.compare_price else None,
            'stock':         v.stock,
            'available':     v.is_available,
            'sku':           v.sku,
            'discount':      v.discount_percentage,
            'attributes':    attrs,
        }

    seen_keys = set()
    all_attr_keys = []
    for v in variants:
        for a in v.attributes.all():
            if a.key not in seen_keys:
                all_attr_keys.append(a.key)
                seen_keys.add(a.key)

    related = (
        Product.objects
        .filter(category=product.category, is_active=True)
        .exclude(pk=product.pk)
        .prefetch_related(
            Prefetch('images', queryset=ProductImage.objects.filter(is_primary=True)),
            Prefetch('variants', queryset=ProductVariant.objects.filter(is_active=True)),
        )[:6]
    )

    ctx = _base_context()
    ctx.update({
        'product':          product,
        'variants':         variants,
        'images':           images,
        'variant_data_json': json.dumps(variant_data),
        'attr_keys_json':   json.dumps(all_attr_keys),
        'all_attr_keys':    all_attr_keys,
        'related_products': related,
        'default_variant':  variants[0] if variants else None,
    })
    return render(request, 'store/product_detail.html', ctx)

