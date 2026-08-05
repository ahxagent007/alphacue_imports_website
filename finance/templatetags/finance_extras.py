"""
finance/templatetags/finance_extras.py
──────────────────────────────────────
Small helpers for the finance templates.

The ageing report keys its totals by bucket label, and Django templates cannot
subscript a dict by a variable — hence `get_item`.
"""

from decimal import Decimal

from django import template

register = template.Library()

ZERO = Decimal('0.00')


@register.filter
def get_item(mapping, key):
    """Look up a dict entry by a variable key: {{ totals|get_item:bucket }}"""
    if mapping is None:
        return None
    try:
        return mapping.get(key)
    except AttributeError:
        return None


@register.filter
def taka(value):
    """Format a number the way the rest of the panel shows money."""
    if value is None:
        return '৳0.00'
    try:
        return f'৳{Decimal(value):,.2f}'
    except (TypeError, ValueError, ArithmeticError):
        return value


@register.filter
def subtract(value, arg):
    """value − arg, for running comparisons in templates."""
    try:
        return Decimal(value) - Decimal(arg)
    except (TypeError, ValueError, ArithmeticError):
        return ZERO


@register.filter
def percentage_of(value, total):
    """What share `value` is of `total`, as a plain number for bar widths."""
    try:
        value, total = Decimal(value), Decimal(total)
        if total == ZERO:
            return 0
        return float(value / total * 100)
    except (TypeError, ValueError, ArithmeticError):
        return 0
