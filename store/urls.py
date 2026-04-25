from django.urls import path
from . import views, cart_views, checkout_views, order_views

app_name = 'store'

urlpatterns = [
    # ── Catalog ──────────────────────────────────────────────────────────
    path('', views.homepage, name='homepage'),
    path('shop/', views.product_list, name='product_list'),
    path('shop/<slug:slug>/', views.category_detail, name='category_detail'),
    path('product/<slug:slug>/', views.product_detail, name='product_detail'),

    # ── Search ───────────────────────────────────────────────────────────
    path('search/', views.search, name='search'),

    # ── Cart ─────────────────────────────────────────────────────────────
    path('cart/', cart_views.cart_detail, name='cart'),
    path('cart/add/', cart_views.cart_add, name='cart_add'),
    path('cart/update/', cart_views.cart_update, name='cart_update'),
    path('cart/remove/', cart_views.cart_remove, name='cart_remove'),
    path('cart/delivery-zone/', cart_views.set_delivery_zone, name='cart_delivery_zone'),

    # ── Checkout ─────────────────────────────────────────────────────────
    path('checkout/', checkout_views.checkout, name='checkout'),
    path('order/confirmation/<str:order_number>/', checkout_views.order_confirmation, name='order_confirmation'),

    # ── Customer order tracking ───────────────────────────────────────────
    path('orders/track/', order_views.order_track, name='order_track'),
    path('orders/<str:order_number>/', order_views.order_detail_customer, name='order_detail_customer'),

    # ── Staff order management ────────────────────────────────────────────
    path('manage/orders/', order_views.manage_order_list, name='manage_order_list'),
    path('manage/orders/<str:order_number>/', order_views.manage_order_detail, name='manage_order_detail'),
    path('manage/orders/<str:order_number>/status/', order_views.manage_order_status_ajax, name='manage_order_status_ajax'),
]