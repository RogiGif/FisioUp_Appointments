from datetime import datetime, timedelta
from django.db.models import Q
from django.utils import timezone
from django.utils.timesince import timesince
from urllib.parse import urlencode
from core.session_timeout import get_session_timeout_config
from .models import Professional, Appointment, ClientProfile, AppointmentLog, EmailLog
from .permissions import is_receptionist, can_access_backoffice, is_admin_role
from .emails import send_templated_email, clinic_settings

def role_flags(request):
    is_professional = False
    is_reception = False
    can_backoffice = False
    is_admin = False
    user_avatar_url = ""
    if request.user.is_authenticated:
        prof = Professional.objects.filter(user=request.user).first()
        is_professional = bool(prof)
        is_reception = is_receptionist(request.user)
        can_backoffice = can_access_backoffice(request.user)
        is_admin = is_admin_role(request.user)
        if prof and prof.profile_photo:
            user_avatar_url = prof.profile_photo.url
        else:
            try:
                client_profile = request.user.client_profile
            except ClientProfile.DoesNotExist:
                client_profile = None
            if client_profile and client_profile.profile_photo:
                user_avatar_url = client_profile.profile_photo.url
    return {
        "is_professional": is_professional,
        "is_receptionist": is_reception,
        "can_access_backoffice": can_backoffice,
        "is_admin_role": is_admin,
        "user_avatar_url": user_avatar_url,
        "session_timeout_config": get_session_timeout_config(request.user),
    }


def appointment_notifications(request):
    if not request.user.is_authenticated:
        return {
            "appointment_notifications": [],
            "appointment_notifications_count": 0,
        }

    last_read_raw = request.session.get("notifications_last_read") or ""
    last_read_dt = None
    if last_read_raw:
        try:
            last_read_dt = datetime.fromisoformat(last_read_raw)
            if timezone.is_naive(last_read_dt):
                last_read_dt = timezone.make_aware(last_read_dt, timezone.get_current_timezone())
        except ValueError:
            last_read_dt = None

    now = timezone.localtime()
    today = now.date()
    now_t = now.time()

    def _initials_from_name(name):
        if not name:
            return ""
        parts = [p for p in name.strip().split() if p]
        if not parts:
            return ""
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()

    def _client_avatar_url(user):
        try:
            profile = user.client_profile
        except ClientProfile.DoesNotExist:
            return ""
        if profile and profile.profile_photo:
            return profile.profile_photo.url
        return ""

    def _client_avatar_initials(user):
        try:
            profile = user.client_profile
        except ClientProfile.DoesNotExist:
            profile = None
        name = ""
        if profile and profile.full_name:
            name = profile.full_name
        else:
            name = user.get_full_name() or getattr(user, "username", "")
        return _initials_from_name(name)

    prof = Professional.objects.select_related("user").filter(user=request.user).first()
    if not prof:
        if is_admin_role(request.user):
            base_qs = Appointment.objects.exclude(status=Appointment.STATUS_CANCELLED)
            needs_confirm = base_qs.filter(
                status=Appointment.STATUS_PENDING,
            ).filter(
                Q(date__gt=today) | Q(date=today, time__gte=now_t)
            )
            needs_review = base_qs.exclude(status=Appointment.STATUS_IN_DEBT).filter(
                Q(date__lt=today) | Q(date=today, time__lt=now_t)
            ).filter(
                status__in=[
                    Appointment.STATUS_PENDING,
                    Appointment.STATUS_SCHEDULED,
                    Appointment.STATUS_AWAITING_VALIDATION,
                ]
            )

            if last_read_dt:
                appointment_field_names = {f.name for f in Appointment._meta.fields}
                ts_field = "updated_at" if "updated_at" in appointment_field_names else "created_at"
                needs_confirm = needs_confirm.filter(**{f"{ts_field}__gt": last_read_dt})
                needs_review = needs_review.filter(**{f"{ts_field}__gt": last_read_dt})

            confirm_count = needs_confirm.count()
            review_count = needs_review.count()
            total = confirm_count + review_count
            confirm_items = list(
                needs_confirm
                .select_related("client", "service", "professional", "professional__user")
                .order_by("date", "time")[:3]
            )
            review_items = list(
                needs_review
                .select_related("client", "service", "professional", "professional__user")
                .order_by("-date", "-time")[:2]
            )

            notifications = []
            for appt in confirm_items:
                client_name = appt.client.get_full_name() or appt.client.username
                service_name = appt.service.name if appt.service else "Serviço"
                prof_name = (
                    appt.professional.user.get_full_name() or appt.professional.user.username
                    if appt.professional and appt.professional.user
                    else "Profissional"
                )
                notifications.append(
                    {
                        "title": "Confirmar marcação",
                        "detail": f"{service_name} · {client_name} · {prof_name}",
                        "date_label": f"{appt.date:%d/%m/%Y} {appt.time:%H:%M}",
                        "url": f"/prof/calendario/marcacao/{appt.id}/",
                        "avatar_url": _client_avatar_url(appt.client),
                        "avatar_initials": _client_avatar_initials(appt.client),
                    }
                )

            for appt in review_items:
                client_name = appt.client.get_full_name() or appt.client.username
                service_name = appt.service.name if appt.service else "Serviço"
                prof_name = (
                    appt.professional.user.get_full_name() or appt.professional.user.username
                    if appt.professional and appt.professional.user
                    else "Profissional"
                )
                appt_dt = datetime.combine(appt.date, appt.time)
                appt_dt = timezone.make_aware(appt_dt, timezone.get_current_timezone())
                when_label = timesince(appt_dt, now)
                notifications.append(
                    {
                        "title": "Rever marcação",
                        "detail": f"{service_name} · {client_name} · {prof_name}",
                        "date_label": f"há {when_label}",
                        "url": f"/prof/calendario/marcacao/{appt.id}/",
                        "avatar_url": _client_avatar_url(appt.client),
                        "avatar_initials": _client_avatar_initials(appt.client),
                    }
                )

            return {
                "appointment_notifications": notifications,
                "appointment_notifications_count": total,
            }

        try:
            client_profile = request.user.client_profile
        except ClientProfile.DoesNotExist:
            return {
                "appointment_notifications": [],
                "appointment_notifications_count": 0,
            }

        logs_qs = AppointmentLog.objects.filter(
            appointment__client=request.user,
            action__in=[
                AppointmentLog.ACTION_RESCHEDULED,
                AppointmentLog.ACTION_CANCELLED,
                AppointmentLog.ACTION_STATUS_UPDATED,
            ],
        ).select_related("appointment", "appointment__service", "appointment__professional", "appointment__professional__user")

        logs_qs = logs_qs.filter(
            Q(appointment__date__gt=today)
            | Q(appointment__date=today, appointment__time__gte=now_t)
        )

        if last_read_dt:
            logs_qs = logs_qs.filter(created_at__gt=last_read_dt)

        total = logs_qs.count()
        items = logs_qs.order_by("-created_at")[:5]

        notifications = []
        for log in items:
            appt = log.appointment
            service_name = appt.service.name if appt.service else "Serviço"
            prof_name = (
                appt.professional.user.get_full_name() or appt.professional.user.username
                if appt.professional and appt.professional.user
                else "Profissional"
            )
            if log.action == AppointmentLog.ACTION_RESCHEDULED:
                title = "Marcação reagendada"
                if log.new_date and log.new_time:
                    detail = f"{service_name} · {log.new_date:%d/%m/%Y} {log.new_time:%H:%M} · {prof_name}"
                else:
                    detail = f"{service_name} · {prof_name}"
            elif log.action == AppointmentLog.ACTION_STATUS_UPDATED:
                status_label = dict(Appointment.STATUS_CHOICES).get(log.new_status, log.new_status or "")
                if log.new_status == Appointment.STATUS_SCHEDULED:
                    title = "Marcação confirmada"
                elif log.new_status == Appointment.STATUS_CANCELLED:
                    title = "Marcação cancelada"
                elif log.new_status == Appointment.STATUS_AWAITING_VALIDATION:
                    title = "Marcação a validar"
                elif log.new_status == Appointment.STATUS_NO_SHOW:
                    title = "Falta registada"
                elif log.new_status == Appointment.STATUS_COMPLETED:
                    title = "Marcação concluída"
                else:
                    title = "Estado atualizado"
                detail = f"{service_name} · {appt.date:%d/%m/%Y} {appt.time:%H:%M} · {prof_name}"
                if status_label:
                    detail = f"{detail} · {status_label}"
            else:
                title = "Marcação cancelada"
                detail = f"{service_name} · {appt.date:%d/%m/%Y} {appt.time:%H:%M} · {prof_name}"
            when_label = timesince(log.created_at, now)
            notifications.append(
                {
                    "title": title,
                    "detail": detail,
                    "date_label": f"há {when_label}",
                    "url": "/minhas-marcacoes/",
                    "avatar_url": _client_avatar_url(appt.client),
                    "avatar_initials": _client_avatar_initials(appt.client),
                }
            )

        return {
            "appointment_notifications": notifications,
            "appointment_notifications_count": total,
        }

    base_qs = Appointment.objects.filter(professional=prof).exclude(status=Appointment.STATUS_CANCELLED)

    needs_confirm = base_qs.filter(
        status=Appointment.STATUS_PENDING,
    ).filter(
        Q(date__gt=today) | Q(date=today, time__gte=now_t)
    )

    needs_review = base_qs.exclude(status=Appointment.STATUS_IN_DEBT).filter(
        Q(date__lt=today) | Q(date=today, time__lt=now_t)
    ).filter(
        status__in=[
            Appointment.STATUS_PENDING,
            Appointment.STATUS_SCHEDULED,
            Appointment.STATUS_AWAITING_VALIDATION,
        ]
    )

    if last_read_dt:
        # Appointment não tem updated_at em todos os ambientes/migrações.
        # Usa created_at como fallback para manter "marcar como lidas" estável.
        appointment_field_names = {f.name for f in Appointment._meta.fields}
        ts_field = "updated_at" if "updated_at" in appointment_field_names else "created_at"
        needs_confirm = needs_confirm.filter(**{f"{ts_field}__gt": last_read_dt})
        needs_review = needs_review.filter(**{f"{ts_field}__gt": last_read_dt})

    confirm_count = needs_confirm.count()
    review_count = needs_review.count()
    total = confirm_count + review_count
    confirm_items = list(needs_confirm.select_related("client", "service").order_by("date", "time")[:3])
    review_items = list(needs_review.select_related("client", "service").order_by("-date", "-time")[:2])

    notifications = []
    for appt in confirm_items:
        client_name = appt.client.get_full_name() or appt.client.username
        service_name = appt.service.name
        notifications.append(
            {
                "title": "Confirmar marcação",
                "detail": f"{service_name} · {client_name}",
                "date_label": f"{appt.date:%d/%m/%Y} {appt.time:%H:%M}",
                "url": f"/prof/calendario/marcacao/{appt.id}/",
                "avatar_url": _client_avatar_url(appt.client),
                "avatar_initials": _client_avatar_initials(appt.client),
            }
        )

    for appt in review_items:
        client_name = appt.client.get_full_name() or appt.client.username
        service_name = appt.service.name
        appt_dt = datetime.combine(appt.date, appt.time)
        appt_dt = timezone.make_aware(appt_dt, timezone.get_current_timezone())
        when_label = timesince(appt_dt, now)
        notifications.append(
            {
                "title": "Rever marcação",
                "detail": f"{service_name} · {client_name}",
                "date_label": f"há {when_label}",
                "url": f"/prof/calendario/marcacao/{appt.id}/",
                "avatar_url": _client_avatar_url(appt.client),
                "avatar_initials": _client_avatar_initials(appt.client),
            }
        )

    # Reminder email (throttled): only for technicians/professionals without backoffice access.
    if prof and not can_access_backoffice(request.user) and total > 0:
        settings_obj = clinic_settings()
        prof_email = (getattr(prof.user, "email", "") or "").strip()
        if settings_obj.notify_professional_on_new_booking and prof_email:
            throttle_from = now - timedelta(hours=2)
            already_sent = EmailLog.objects.filter(
                event="professional_action_reminder",
                to=prof_email,
                status="sent",
                created_at__gte=throttle_from,
            ).exists()
            if not already_sent:
                send_templated_email(
                    prof_email,
                    f"Ações pendentes na agenda — {confirm_count} para confirmar / {review_count} para rever",
                    "emails/professional_action_reminder.html",
                    "emails/professional_action_reminder.txt",
                    {
                        "professional_name": prof.user.get_full_name() or prof.user.username,
                        "confirm_count": confirm_count,
                        "review_count": review_count,
                        "calendar_url": request.build_absolute_uri("/prof/marcacoes/?tab=pending"),
                    },
                    event="professional_action_reminder",
                )

    return {
        "appointment_notifications": notifications,
        "appointment_notifications_count": total,
    }


def upcoming_appointments(request):
    if not request.user.is_authenticated:
        return {
            "upcoming_appointments": [],
            "upcoming_today_count": 0,
            "upcoming_appointments_url": "",
            "upcoming_show_empty": True,
        }

    prof = Professional.objects.filter(user=request.user).first()
    if not prof:
        try:
            client_profile = request.user.client_profile
        except ClientProfile.DoesNotExist:
            return {
                "upcoming_appointments": [],
                "upcoming_today_count": 0,
                "upcoming_appointments_url": "",
                "upcoming_show_empty": True,
            }

        now = timezone.localtime()
        today = now.date()
        now_t = now.time()

        base_qs = Appointment.objects.filter(client=request.user).exclude(
            status__in=[
                Appointment.STATUS_CANCELLED,
                Appointment.STATUS_COMPLETED,
                Appointment.STATUS_IN_DEBT,
                Appointment.STATUS_NO_SHOW,
            ]
        )

        upcoming_qs = base_qs.filter(
            Q(date__gt=today) | Q(date=today, time__gte=now_t)
        ).select_related("professional", "professional__user", "service").order_by("date", "time")

        today_count = base_qs.filter(date=today, time__gte=now_t).count()
        upcoming_items = upcoming_qs[:3]

        items = []
        for appt in upcoming_items:
            professional_name = (
                appt.professional.user.get_full_name() or appt.professional.user.username
                if appt.professional and appt.professional.user
                else "Profissional"
            )
            service_name = appt.service.name if appt.service else "Serviço"
            items.append(
                {
                    "id": appt.id,
                    "date": appt.date,
                    "time": appt.time,
                    "time_label": appt.time.strftime("%H:%M") if appt.time else "—",
                    "client_name": professional_name,
                    "service_name": service_name,
                    "url": "/minhas-marcacoes/",
                }
            )

        return {
            "upcoming_appointments": items,
            "upcoming_today_count": today_count,
            "upcoming_appointments_url": "/minhas-marcacoes/",
            "upcoming_show_empty": False,
        }

    now = timezone.localtime()
    today = now.date()
    now_t = now.time()

    base_qs = Appointment.objects.filter(
        professional=prof,
        status=Appointment.STATUS_SCHEDULED,
    )

    upcoming_qs = base_qs.filter(
        Q(date__gt=today) | Q(date=today, time__gte=now_t)
    ).select_related("client", "service").order_by("date", "time")

    today_count = base_qs.filter(date=today, time__gte=now_t).count()
    upcoming_items = upcoming_qs[:3]

    items = []
    for appt in upcoming_items:
        client_name = appt.client.get_full_name() or appt.client.username
        service_name = appt.service.name if appt.service else "Serviço"
        items.append(
            {
                "id": appt.id,
                "date": appt.date,
                "time": appt.time,
                "time_label": appt.time.strftime("%H:%M") if appt.time else "—",
                "client_name": client_name,
                "service_name": service_name,
                "url": f"/prof/calendario/marcacao/{appt.id}/",
            }
        )

    params = {
        "date": today.strftime("%Y-%m-%d"),
        "view_mode": "day",
        "type": "all",
        "professional_id": str(prof.id),
        "status": Appointment.STATUS_SCHEDULED,
    }
    upcoming_url = f"/prof/marcacoes/?{urlencode(params)}"

    return {
        "upcoming_appointments": items,
        "upcoming_today_count": today_count,
        "upcoming_appointments_url": upcoming_url,
        "upcoming_show_empty": True,
    }
