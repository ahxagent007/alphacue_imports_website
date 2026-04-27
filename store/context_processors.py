"""
store/context_processors.py
----------------------------
Injects the cart object into every template context automatically.
Register in settings.py TEMPLATES > OPTIONS > context_processors.
"""

from .cart import Cart


def cart(request):
    return {'cart': Cart(request)}


def google_analytics(request):
    from django.conf import settings
    return {
        'GOOGLE_ANALYTICS_ID': getattr(settings, 'GOOGLE_ANALYTICS_ID', ''),
    }
