import os
from datetime import date, timedelta

import pymysql

pymysql.install_as_MySQLdb()

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from core.models import Appointment


DAYS_TO_SHOW = 2
EXCLUDED_STATUSES = {Appointment.STATUS_CANCELLED}


def user_name(user):
    if not user:
        return ""
    name = user.get_full_name().strip()
    if name:
        return name
    try:
        profile_name = (user.client_profile.full_name or "").strip()
    except Exception:
        profile_name = ""
    return profile_name or user.username or user.email or f"User #{user.pk}"


def user_phone(user):
    try:
        return (user.client_profile.phone or "").strip()
    except Exception:
        return ""


def professional_name(professional):
    if not professional:
        return ""
    user = getattr(professional, "user", None)
    if user:
        name = user.get_full_name().strip()
        if name:
            return name
        return user.username or user.email or str(professional)
    return str(professional)


start = date.today()
end = start + timedelta(days=DAYS_TO_SHOW - 1)

appointments = (
    Appointment.objects.select_related(
        "client",
        "client__client_profile",
        "professional",
        "professional__user",
        "service",
    )
    .filter(date__range=(start, end))
    .exclude(status__in=EXCLUDED_STATUSES)
    .order_by("date", "time", "professional__user__first_name", "client__first_name")
)

print(f"AGENDA {start:%Y-%m-%d} a {end:%Y-%m-%d}")
print("=" * 80)

current_day = None
count = 0
for appt in appointments:
    if appt.date != current_day:
        current_day = appt.date
        print(f"\n{current_day:%Y-%m-%d}")
        print("-" * 80)

    count += 1
    client = appt.client
    service = appt.service.name if appt.service_id else ""
    status = appt.get_status_display()
    line = (
        f"{appt.time:%H:%M} | "
        f"{user_name(client)} | "
        f"{user_phone(client)} | "
        f"{professional_name(appt.professional)} | "
        f"{service} | "
        f"{status}"
    )
    print(line)

print("\n" + "=" * 80)
print(f"Total: {count}")
