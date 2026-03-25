from __future__ import annotations

from datetime import date, datetime, time
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from django.conf import settings
from django.core.cache import cache
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from core.models import AuditLog
from core.permissions import is_admin_role, is_receptionist, is_technician


def _normalize_value(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_normalize_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _normalize_value(item) for key, item in value.items()}
    return value


def snapshot_instance(instance, fields=None):
    if instance is None:
        return {}

    if fields is not None:
        data = {}
        for field_name in fields:
            data[str(field_name)] = _normalize_value(getattr(instance, field_name, None))
        return data

    data = {}
    for field in instance._meta.fields:
        key = field.attname if getattr(field, "is_relation", False) else field.name
        data[key] = _normalize_value(getattr(instance, key, None))
    return data


def actor_role_label(user) -> str:
    if not getattr(user, "is_authenticated", False):
        return "anonymous"
    if getattr(user, "is_superuser", False):
        return "superuser"
    if is_admin_role(user):
        return "admin"
    if is_receptionist(user):
        return "reception"
    if is_technician(user):
        return "professional"
    if getattr(user, "is_staff", False):
        return "staff"
    return "client"


def request_metadata(request) -> dict:
    if request is None:
        return {
            "ip_address": None,
            "user_agent": "",
            "request_path": "",
            "request_method": "",
        }
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    ip_address = forwarded.split(",")[0].strip() if forwarded else request.META.get("REMOTE_ADDR", "")
    return {
        "ip_address": ip_address or None,
        "user_agent": (request.META.get("HTTP_USER_AGENT", "") or "")[:1000],
        "request_path": (request.path or "")[:255],
        "request_method": (request.method or "")[:12],
    }


def cleanup_old_audit_logs_if_needed(*, force: bool = False) -> int:
    retention_days = int(getattr(settings, "AUDIT_LOG_RETENTION_DAYS", 365) or 0)
    if retention_days <= 0:
        return 0

    cache_key = "audit_log_cleanup_last_run"
    if not force and not cache.add(cache_key, "1", timeout=60 * 60 * 24):
        return 0

    cutoff = timezone.now() - timedelta(days=retention_days)
    deleted_count, _ = AuditLog.objects.filter(created_at__lt=cutoff).delete()
    return deleted_count


def log_audit_event(
    *,
    category: str,
    action: str,
    request=None,
    actor=None,
    instance=None,
    source: str = "",
    message: str = "",
    before=None,
    after=None,
    metadata=None,
):
    actor = actor or getattr(request, "user", None)
    content_type = None
    object_id = None
    object_repr = ""
    if instance is not None and getattr(instance, "pk", None):
        content_type = ContentType.objects.get_for_model(instance, for_concrete_model=False)
        object_id = instance.pk
        object_repr = str(instance)[:255]

    actor_display = ""
    actor_email = ""
    actor_role = "anonymous"
    if getattr(actor, "is_authenticated", False):
        actor_display = (actor.get_full_name() or actor.get_username() or str(actor))[:255]
        actor_email = (getattr(actor, "email", "") or "")[:254]
        actor_role = actor_role_label(actor)

    request_info = request_metadata(request)

    log = AuditLog.objects.create(
        category=(category or "")[:64],
        action=(action or "")[:64],
        source=(source or "")[:64],
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        actor_display=actor_display,
        actor_email=actor_email,
        actor_role=actor_role,
        content_type=content_type,
        object_id=object_id,
        object_repr=object_repr,
        message=(message or "")[:255],
        before=_normalize_value(before or {}),
        after=_normalize_value(after or {}),
        metadata=_normalize_value(metadata or {}),
        **request_info,
    )
    cleanup_old_audit_logs_if_needed()
    return log
