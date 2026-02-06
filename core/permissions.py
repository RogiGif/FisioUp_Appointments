from django.contrib.auth.models import Group


def is_receptionist(user, *, ensure_group=False):
    if not user.is_authenticated:
        return False
    if ensure_group:
        Group.objects.get_or_create(name="receptionist")
    return user.groups.filter(name="receptionist").exists()


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
    if user.is_staff:
        return True
    if user.has_perm("core.can_view_all_calendar"):
        return True
    if is_receptionist(user, ensure_group=True):
        return True
    return _in_group(user, "ADMIN") or _in_group(user, "RECEPTION")


def can_book_for_any_professional(user):
    if not user.is_authenticated:
        return False
    if user.is_staff:
        return True
    if user.has_perm("core.can_book_for_any_professional"):
        return True
    if is_receptionist(user, ensure_group=True):
        return True
    return _in_group(user, "ADMIN") or _in_group(user, "RECEPTION")


def can_access_backoffice(user):
    if not user.is_authenticated:
        return False
    if user.is_staff:
        return True
    if user.has_perm("core.can_access_backoffice"):
        return True
    if is_receptionist(user, ensure_group=True):
        return True
    return _in_group(user, "ADMIN") or _in_group(user, "RECEPTION")
