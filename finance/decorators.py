"""
finance/decorators.py
─────────────────────
Access control for the finance panel.

Every finance view is wrapped in `finance_staff_required`. By default that is
staff-only, matching how store/order_views.py protects /manage/orders/.

Once more than one person has admin access, set FINANCE_REQUIRED_GROUP in .env
to the name of a Django group. Only members of that group (and superusers) will
then reach the finance screens — an order-packing assistant does not need to see
the bank balance or what each investor is owed.
"""

from functools import wraps

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import PermissionDenied


def user_can_access_finance(user):
    """Whether this user is allowed into the finance panel."""
    if not (user.is_authenticated and user.is_active and user.is_staff):
        return False

    required_group = getattr(settings, 'FINANCE_REQUIRED_GROUP', '')
    if not required_group:
        return True

    if user.is_superuser:
        return True

    return user.groups.filter(name=required_group).exists()


def finance_staff_required(view_func):
    """
    Allow only staff — and, when FINANCE_REQUIRED_GROUP is set, only members of
    that group — into the finance panel.

    Falls back to `staff_member_required` for the not-logged-in case so the
    redirect to the login page keeps working; the group check then runs on top
    and raises PermissionDenied rather than bouncing an already-signed-in user
    back to a login form they have already passed.
    """

    @wraps(view_func)
    @staff_member_required
    def _wrapped(request, *args, **kwargs):
        if not user_can_access_finance(request.user):
            raise PermissionDenied(
                'Your account does not have access to the finance panel.'
            )
        return view_func(request, *args, **kwargs)

    return _wrapped
