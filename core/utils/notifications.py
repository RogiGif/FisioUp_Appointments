from __future__ import annotations

import re
from typing import List, Optional

from django.conf import settings
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives, send_mail
from django.template.loader import render_to_string

from core.models import ClinicSettings, EmailLog


def get_clinic_settings() -> Optional[ClinicSettings]:
    return ClinicSettings.objects.first()


def parse_emails(text: str) -> List[str]:
    if not text:
        return []
    parts = re.split(r"[\n,;]+", text)
    cleaned = []
    for p in parts:
        email = (p or "").strip()
        if not email:
            continue
        try:
            validate_email(email)
        except ValidationError:
            continue
        if email not in cleaned:
            cleaned.append(email)
    return cleaned


def get_clinic_notification_emails() -> List[str]:
    settings_obj = get_clinic_settings()
    if settings_obj:
        emails = parse_emails(settings_obj.notification_emails)
        if emails:
            return emails

    fallback = getattr(settings, "CLINIC_NOTIFICATION_EMAILS", None)
    if fallback:
        return list(fallback)

    default_from = getattr(settings, "DEFAULT_FROM_EMAIL", "")
    return [default_from] if default_from else []


def get_from_email() -> str:
    settings_obj = get_clinic_settings()
    if settings_obj and settings_obj.from_email:
        return settings_obj.from_email
    return getattr(settings, "DEFAULT_FROM_EMAIL", "")


def get_clinic_name() -> str:
    settings_obj = get_clinic_settings()
    return settings_obj.clinic_name if settings_obj and settings_obj.clinic_name else "FisioUp"


def _render_html(subject: str, heading: str, message: str) -> str:
    return render_to_string(
        "core/emails/base.html",
        {
            "subject": subject,
            "clinic_name": get_clinic_name(),
            "heading": heading,
            "message": message,
        },
    )


def _send_email(event: str, subject: str, message: str, recipients: List[str]) -> int:
    if not recipients:
        return 0
    from_email = get_from_email()
    html_body = _render_html(subject, subject, message)
    email = EmailMultiAlternatives(subject, message, from_email, recipients)
    email.attach_alternative(html_body, "text/html")
    status = "sent"
    error = ""
    try:
        sent = email.send(fail_silently=False)
    except Exception as exc:
        sent = 0
        status = "failed"
        error = str(exc)
    EmailLog.objects.create(
        event=event,
        to=",".join(recipients),
        subject=subject,
        body_text=message,
        body_html=html_body,
        status=status,
        error=error,
    )
    return sent


def _setting_enabled(name: str) -> bool:
    settings_obj = get_clinic_settings()
    if not settings_obj:
        return True
    return bool(getattr(settings_obj, name, True))


def notify_clinic(subject: str, message: str) -> int:
    if not _setting_enabled("notify_clinic_on_new_booking"):
        return 0
    recipients = get_clinic_notification_emails()
    return _send_email("new_booking", subject, message, recipients)


def notify_clinic_custom(toggle_name: str, subject: str, message: str) -> int:
    if not _setting_enabled(toggle_name):
        return 0
    recipients = get_clinic_notification_emails()
    event = "generic"
    if toggle_name == "notify_admin_on_pending_registration":
        event = "pending_registration"
    elif toggle_name == "notify_clinic_on_client_reschedule":
        event = "reschedule_client"
    elif toggle_name == "notify_clinic_on_client_cancel":
        event = "cancel_client"
    return _send_email(event, subject, message, recipients)


def notify_professional(professional, subject: str, message: str) -> int:
    if not _setting_enabled("notify_professional_on_new_booking"):
        return 0
    email = getattr(getattr(professional, "user", None), "email", "") or ""
    if not email:
        return 0
    return _send_email("new_booking", subject, message, [email])


def notify_client(user, subject: str, message: str, event: str = "reschedule_clinic") -> int:
    if not _setting_enabled("notify_client_on_clinic_changes"):
        return 0
    email = getattr(user, "email", "") or ""
    if not email:
        return 0
    return _send_email(event, subject, message, [email])
