from django.contrib.auth.models import Group


def is_receptionist(user, *, ensure_group=False):
    if not user.is_authenticated:
        return False
    if ensure_group:
        Group.objects.get_or_create(name="RECEPTION")
    return user.groups.filter(name="RECEPTION").exists()


def is_technician(user):
    if not user.is_authenticated:
        return False
    return hasattr(user, "professional") and user.professional is not None


def _in_group(user, name):
    if not user.is_authenticated:
        return False
    return user.groups.filter(name=name).exists()


def can_view_all_calendar(user):
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return user.has_perm("core.can_view_all_calendar")


def can_book_for_any_professional(user):
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return user.has_perm("core.can_book_for_any_professional")


def can_access_backoffice(user):
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return user.has_perm("core.can_access_backoffice")


def is_admin_role(user):
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return _in_group(user, "ADMIN")
