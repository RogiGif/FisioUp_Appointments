from __future__ import annotations

from typing import Any, Dict, Optional

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

from core.models import ClinicSettings, EmailLog


def clinic_settings() -> ClinicSettings:
    return ClinicSettings.get_solo()


def clinic_email() -> str:
    settings_obj = clinic_settings()
    if settings_obj.clinic_email:
        return settings_obj.clinic_email
    return getattr(settings, "DEFAULT_FROM_EMAIL", "")


def send_templated_email(
    to_email: str,
    subject: str,
    template_html: str,
    template_txt: str,
    context: Dict[str, Any],
    *,
    reply_to: Optional[str] = None,
    event: str = "generic",
) -> int:
    settings_obj = clinic_settings()
    ctx = {
        **context,
        "clinic_settings": settings_obj,
        "clinic_name": settings_obj.clinic_name,
        "footer_text": settings_obj.footer_text,
        "signature_text": settings_obj.signature_text,
        "logo_url": settings_obj.logo.url if settings_obj.logo else "",
        "subject": subject,
    }

    html_body = render_to_string(template_html, ctx)
    txt_body = render_to_string(template_txt, ctx)

    from_email = settings_obj.from_email or getattr(settings, "DEFAULT_FROM_EMAIL", "")
    final_reply_to = reply_to or settings_obj.reply_to_email or None

    email = EmailMultiAlternatives(
        subject=subject,
        body=txt_body,
        from_email=from_email,
        to=[to_email],
        reply_to=[final_reply_to] if final_reply_to else None,
    )
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
        to=to_email,
        subject=subject,
        body_text=txt_body,
        body_html=html_body,
        status=status,
        error=error,
    )

    return sent


def log_email_skip(event: str, subject: str, reason: str, to_email: str = "") -> None:
    EmailLog.objects.create(
        event=event,
        to=to_email,
        subject=subject,
        body_text="",
        body_html="",
        status="skipped",
        error=reason,
    )
