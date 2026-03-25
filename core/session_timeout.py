from django.conf import settings
from django.urls import reverse

from core.permissions import can_access_backoffice, is_technician


def is_internal_session_user(user):
    if not user or not user.is_authenticated:
        return False
    return can_access_backoffice(user) or is_technician(user)


def get_session_timeout_config(user):
    if not user or not user.is_authenticated:
        return {
            "enabled": False,
            "timeout_seconds": 0,
            "warning_seconds": 0,
            "keepalive_interval_seconds": 0,
            "keepalive_url": "",
            "login_url": reverse("login"),
            "logout_url": reverse("logout"),
            "is_internal": False,
        }

    is_internal = is_internal_session_user(user)
    timeout_seconds = (
        settings.INTERNAL_SESSION_TIMEOUT_SECONDS
        if is_internal
        else settings.CLIENT_SESSION_TIMEOUT_SECONDS
    )
    warning_seconds = (
        settings.INTERNAL_SESSION_WARNING_SECONDS
        if is_internal
        else settings.CLIENT_SESSION_WARNING_SECONDS
    )

    return {
        "enabled": True,
        "timeout_seconds": int(timeout_seconds),
        "warning_seconds": int(min(warning_seconds, timeout_seconds)),
        "keepalive_interval_seconds": int(settings.SESSION_KEEPALIVE_INTERVAL_SECONDS),
        "keepalive_url": reverse("session_keepalive"),
        "login_url": reverse("login"),
        "logout_url": reverse("logout"),
        "is_internal": bool(is_internal),
    }
