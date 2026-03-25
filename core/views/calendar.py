from datetime import datetime, timedelta, time as dtime
from decimal import Decimal
from collections import defaultdict
from dataclasses import dataclass
from uuid import uuid4
import json
import csv
import io
import unicodedata

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User, Group
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth import views as auth_views
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Q, Count, Sum, Max
from django.db.models.functions import Coalesce, TruncDate
from django.shortcuts import render, redirect, get_object_or_404
from django.conf import settings
from django.urls import reverse
from urllib.parse import urlencode
from django.utils.text import slugify
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.safestring import mark_safe
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.views.decorators.http import require_POST, require_http_methods, require_GET

from core.decorators import professional_required, backoffice_required
from core.permissions import (
    can_view_all_calendar,
    can_book_for_any_professional,
    can_access_backoffice,
    is_receptionist,
)
from core.ratelimit import check_rate_limit, rate_limited_response, is_json_request, rate_limit
from core.emails import send_templated_email, clinic_email, clinic_settings, log_email_skip
from core.services.audit import log_audit_event, snapshot_instance
from core.utils.holidays import iter_portuguese_holidays, is_portuguese_holiday
from core.forms import (
    RegisterForm,
    ClientProfileForm,
    ProfessionalProfileForm,
    StaffClientCreateForm,
    BackofficeServiceForm,
    BackofficeProfessionalForm,
    BackofficePartnerForm,
    BackofficeClientProfileForm,
)
from core.utils.pricing import compute_pricing
from core.utils.revenue import (
    get_revenue_queryset,
    compute_trend,
    month_range,
    week_range,
    day_range,
    month_start,
)
from core.models import (
    Professional,
    Appointment,
    Service,
    ClientProfile,
    ClinicalRecord,
    TreatmentRecord,
    AppointmentLog,
    BlockedSlot,
    GroupSession,
    GroupEnrollment,
    GroupSchedule,
    WeeklyWorkingBlock,
    Product,
    AppointmentConsumption,
    MoloniIntegration,
    ClientImportLog,
    ClientImportBatch,
    ClientImportRow,
    Partner,
    PartnerServicePrice,
    ContentPost,
)

from core.views.common import (
    log_appt,
    group_booked_statuses,
    apply_terms_filter,
    ensure_group_sessions_for_schedules,
    professional_weekdays_labels,
    can_modify_appointment,
    _get_professional_or_403,
    _monday_of_week,
    _safe_return_to,
    _get_slots,
    _has_availability_window,
    _is_slot_blocked,
    _is_slot_occupied,
    _occupied_intervals_for_professional_day,
)
from core.services.scheduling import (
    get_active_weekly_schedule,
    get_working_weekdays,
    get_last_end_time_for_date,
    build_slots,
)
from core.services.subcontracting import sync_subcontractor_payout
from core.utils.stock import (
    get_stock,
    get_existing_consumption_totals,
    reconcile_appointment_consumptions,
)


def _appointment_consumptions_snapshot(appointment):
    return [
        {
            "product_id": row.product_id,
            "product_name": row.product.name if row.product_id else "",
            "quantity_base": str(row.quantity_base or Decimal("0.00")),
        }
        for row in (
            AppointmentConsumption.objects
            .select_related("product")
            .filter(appointment=appointment)
            .order_by("product__name", "id")
        )
    ]


def _calendar_service_colors(request=None):
    services = list(Service.objects.order_by("name"))
    service_palette = [
        "#5485e4",
        "#25b865",
        "#d13b4c",
        "#17a2b8",
        "#e49e3d",
        "#5856d6",
        "#3dc7be",
        "#475e77",
    ]
    service_colors = {
        str(s.id): service_palette[i % len(service_palette)] for i, s in enumerate(services)
    }
    services_with_colors = [
        {"id": s.id, "name": s.name, "color": service_colors[str(s.id)]} for s in services
    ]
    return services_with_colors, service_colors


def _hex_to_rgb(value):
    text = str(value or "").strip()
    if not text.startswith("#"):
        return None
    hex_value = text[1:]
    if len(hex_value) == 3:
        hex_value = "".join(ch * 2 for ch in hex_value)
    if len(hex_value) != 6:
        return None
    try:
        return tuple(int(hex_value[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def _rgb_to_hex(rgb):
    r, g, b = rgb
    r = max(0, min(255, int(r)))
    g = max(0, min(255, int(g)))
    b = max(0, min(255, int(b)))
    return f"#{r:02x}{g:02x}{b:02x}"


def _shade_color(color, factor):
    rgb = _hex_to_rgb(color)
    if rgb is None:
        return color
    if factor >= 0:
        mixed = tuple(round(ch + ((255 - ch) * factor)) for ch in rgb)
    else:
        darken = abs(factor)
        mixed = tuple(round(ch * (1 - darken)) for ch in rgb)
    return _rgb_to_hex(mixed)


def _professional_service_color(base_color, professional_id):
    if not professional_id:
        return base_color
    try:
        pid = int(professional_id)
    except (TypeError, ValueError):
        return base_color

    # Mantém o mesmo matiz do serviço e apenas varia a percentagem de claro/escuro
    # de forma determinística por profissional.
    seed = (pid * 2654435761) & 0xFFFFFFFF
    unit = seed / 4294967295
    factor = (unit * 0.34) - 0.17
    if -0.05 < factor < 0.05:
        factor = 0.07 if factor >= 0 else -0.07
    return _shade_color(base_color, factor)


def _get_clinic_last_weekday_and_end(date_obj):
    blocks = WeeklyWorkingBlock.objects.filter(weekly_schedule__is_active=True)
    if not blocks.exists():
        return None, None
    last_weekday = blocks.aggregate(max_weekday=Max("weekday"))["max_weekday"]
    if last_weekday is None:
        return None, None
    last_end = None
    if date_obj.weekday() == last_weekday:
        last_end = (
            blocks.filter(weekday=last_weekday)
            .aggregate(max_end=Max("end_time"))
            .get("max_end")
        )
    return last_weekday, last_end


def _serialize_calendar_events(qs, service_colors):
    events = []
    for appt in qs:
        service = appt.service
        duration = getattr(service, "duration_minutes", None) or 60
        start_dt = datetime.combine(appt.date, appt.time or dtime.min)
        end_dt = start_dt + timedelta(minutes=duration)
        service_color = service_colors.get(str(service.id)) if service else "#5485e4"
        professional_service_color = _professional_service_color(service_color, appt.professional_id)
        if appt.status == Appointment.STATUS_CANCELLED:
            color = "#d13b4c"
        elif appt.status == Appointment.STATUS_PENDING:
            color = "#e49e3d"
        elif appt.status == Appointment.STATUS_AWAITING_VALIDATION:
            color = "#8b5cf6"
        elif appt.status == Appointment.STATUS_NO_SHOW:
            color = "#ef4444"
        elif appt.status == Appointment.STATUS_COMPLETED:
            color = "#25b865"
        elif appt.status == Appointment.STATUS_IN_DEBT:
            color = "#f97316"
        else:
            color = professional_service_color

        title_service = service.name if service else "Serviço"
        client_name = appt.client.get_full_name() or appt.client.username
        prof_name = appt.professional.user.get_full_name() or appt.professional.user.username
        status_label = appt.get_status_display()
        partner_name = appt.partner.name if getattr(appt, "partner", None) else ""
        final_price = None
        try:
            final_price = float(appt.final_price) if appt.final_price is not None else None
        except Exception:
            final_price = None
        events.append(
            {
                "id": str(appt.id),
                "calendarId": str(service.id) if service else "0",
                "title": f"{title_service} - {client_name}",
                "body": f"Técnico: {prof_name}<br>Estado: {status_label}",
                "category": "time",
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "color": "#ffffff",
                "bgColor": color,
                "dragBgColor": color,
                "borderColor": color,
                "raw": {
                    "service_id": service.id if service else None,
                    "service_name": title_service,
                    "professional_id": appt.professional_id,
                    "professional_name": prof_name,
                    "client_name": client_name,
                    "partner_name": partner_name,
                    "status": status_label,
                    "status_raw": appt.status,
                    "is_paid": bool(getattr(appt, "is_paid", False)),
                    "final_price": final_price,
                },
            }
        )
    return events


def _serialize_group_session_events(qs, service_colors):
    events = []
    for session in qs:
        service = session.service
        start_dt = datetime.combine(session.date, session.time or dtime.min)
        end_dt = start_dt + timedelta(minutes=session.duration_value)
        service_color = service_colors.get(str(service.id)) if service else "#00B6DF"
        professional_service_color = _professional_service_color(service_color, session.professional_id)
        if session.status == GroupSession.STATUS_CANCELLED:
            color = "#d13b4c"
        elif session.status == GroupSession.STATUS_COMPLETED:
            color = "#25b865"
        else:
            color = professional_service_color

        booked_count = session.enrolments.filter(status__in=group_booked_statuses()).count()
        title_service = session.name or (session.schedule.name if session.schedule else "") or (service.name if service else "Turma")
        prof_name = session.professional.user.get_full_name() if session.professional else ""
        events.append(
            {
                "id": f"group-{session.id}",
                "calendarId": str(service.id) if service else "group",
                "title": f"Turma: {title_service} ({booked_count}/{session.capacity_value})",
                "body": f"Técnico: {prof_name or '—'}<br>Estado: {session.get_status_display()}",
                "category": "time",
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "color": "#ffffff",
                "bgColor": color,
                "dragBgColor": color,
                "borderColor": color,
                "raw": {
                    "type": "group",
                    "session_id": session.id,
                    "service_id": service.id if service else None,
                    "professional_id": session.professional_id,
                    "status": session.status,
                },
            }
        )
    return events


def _serialize_blocked_slot_events(qs):
    events = []
    for slot in qs:
        start_dt = datetime.combine(slot.date, slot.time or dtime.min)
        end_dt = start_dt + timedelta(minutes=30)
        events.append(
            {
                "id": f"blocked-{slot.id}",
                "calendarId": "blocked",
                "title": "Bloqueado",
                "body": "Horário bloqueado",
                "category": "time",
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "color": "#1f2937",
                "bgColor": "#e5e7eb",
                "dragBgColor": "#e5e7eb",
                "borderColor": "#cbd5e1",
                "raw": {
                    "type": "blocked",
                    "blocked_id": slot.id,
                    "professional_id": slot.professional_id,
                },
            }
        )
    return events


def _serialize_holiday_events(start_date, end_date):
    events = []
    for day, holiday_name in iter_portuguese_holidays(start_date, end_date):
        start_dt = datetime.combine(day, dtime(hour=8, minute=0))
        end_dt = datetime.combine(day, dtime(hour=21, minute=0))
        events.append(
            {
                "id": f"holiday-{day.isoformat()}",
                "calendarId": "holiday",
                "title": f"Feriado nacional · {holiday_name}",
                "body": "Marcações indisponíveis neste dia.",
                "category": "time",
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "color": "#334155",
                "bgColor": "#e2e8f0",
                "dragBgColor": "#e2e8f0",
                "borderColor": "#94a3b8",
                "raw": {
                    "type": "holiday",
                    "holiday_name": holiday_name,
                },
            }
        )
    return events


def _redirect_to_calendar_route(request, route_name):
    target = reverse(route_name)
    query_string = request.GET.urlencode()
    if query_string:
        target = f"{target}?{query_string}"
    return redirect(target)


def professional_calendar_view(request):
    return _redirect_to_calendar_route(request, "professional_calendar")


@login_required
def client_calendar_view(request):
    return _redirect_to_calendar_route(request, "client_calendar")


def professional_calendar_fullcalendar_test_view(request):
    blocked, retry_after = check_rate_limit(
        request,
        name="calendar_ip_minute",
        limit=120,
        window=60,
        by_ip=True,
    )
    if blocked:
        if is_json_request(request):
            return rate_limited_response(
                request,
                "Demasiadas tentativas. Tenta novamente em alguns minutos.",
                retry_after,
            )
        messages.error(request, "Demasiadas tentativas. Tenta novamente em alguns minutos.")
        response = render(request, "backoffice/prof/calendar_fullcalendar_test.html", {"is_admin": False}, status=429)
        response["Retry-After"] = str(retry_after)
        return response

    is_admin = can_view_all_calendar(request.user)
    is_reception = is_receptionist(request.user)
    show_calendar_filters = bool(is_admin or is_reception)
    professional = None
    if not is_admin:
        professional = _get_professional_or_403(request.user)
        if professional is None:
            return HttpResponseForbidden("Acesso apenas para profissionais.")
    else:
        professional = Professional.objects.filter(user=request.user).first()

    if is_admin:
        ensure_group_sessions_for_schedules()
    elif professional:
        ensure_group_sessions_for_schedules(
            schedules=GroupSchedule.objects.filter(professional=professional, is_active=True)
            .select_related("service", "professional")
        )

    today = timezone.localdate()
    now_t = timezone.localtime().time()
    base_date = today
    week_param = (request.GET.get("week") or "").strip()
    session_week = (request.session.get("calendar_week") or "").strip()
    selected_week = week_param or session_week

    if selected_week:
        try:
            base_date = datetime.strptime(selected_week, "%Y-%m-%d").date()
        except ValueError:
            base_date = today
            request.session.pop("calendar_week", None)

    if week_param:
        request.session["calendar_week"] = base_date.strftime("%Y-%m-%d")

    if not week_param:
        current_week_start = _monday_of_week(today)
        base_week_start = _monday_of_week(base_date)
        is_current_week = base_week_start == current_week_start

        if is_current_week:
            last_weekday = None
            last_end = None
            if is_admin:
                last_weekday, last_end = _get_clinic_last_weekday_and_end(today)
            elif professional:
                weekdays = get_working_weekdays(professional)
                if weekdays:
                    last_weekday = max(weekdays)
                    if today.weekday() == last_weekday:
                        last_end = get_last_end_time_for_date(professional, today)

            if last_weekday is not None:
                if today.weekday() > last_weekday:
                    base_date = today + timedelta(days=7)
                    request.session["calendar_week"] = base_date.strftime("%Y-%m-%d")
                elif today.weekday() == last_weekday and last_end and now_t >= last_end:
                    base_date = today + timedelta(days=7)
                    request.session["calendar_week"] = base_date.strftime("%Y-%m-%d")

    past_qs = Appointment.objects.filter(
        Q(date__lt=today) | Q(date=today, time__lt=now_t),
        status__in=[Appointment.STATUS_SCHEDULED, Appointment.STATUS_PENDING],
    )
    if not is_admin and professional:
        past_qs = past_qs.filter(professional=professional)
    past_qs.update(status=Appointment.STATUS_AWAITING_VALIDATION)

    services_with_colors, service_colors = _calendar_service_colors()
    professionals = Professional.objects.select_related("user").order_by("user__username")

    week_start = _monday_of_week(base_date)
    week_end = week_start + timedelta(days=6)
    qs = Appointment.objects.select_related(
        "client", "service", "professional", "professional__user", "client__client_profile", "partner"
    ).filter(date__range=(week_start, week_end)).exclude(status=Appointment.STATUS_CANCELLED)
    if not is_admin and professional:
        qs = qs.filter(professional=professional)

    events = _serialize_calendar_events(qs, service_colors)

    group_qs = GroupSession.objects.select_related(
        "service", "professional", "professional__user", "schedule"
    ).filter(date__range=(week_start, week_end))
    if not is_admin and professional:
        group_qs = group_qs.filter(professional=professional)
    events += _serialize_group_session_events(group_qs, service_colors)

    blocked_qs = BlockedSlot.objects.filter(date__range=(week_start, week_end))
    if not is_admin and professional:
        blocked_qs = blocked_qs.filter(professional=professional)
    events += _serialize_blocked_slot_events(blocked_qs)

    schedule_tz = "Europe/Lisbon"
    if professional:
        schedule = get_active_weekly_schedule(professional)
        if schedule and schedule.timezone:
            schedule_tz = schedule.timezone

    calendar_data = {
        "defaultView": "week",
        "baseDate": base_date.strftime("%Y-%m-%d"),
        "services": services_with_colors,
        "professionals": [
            {
                "id": p.id,
                "name": p.user.get_full_name() or p.user.username,
            }
            for p in professionals
        ],
        "events": events,
        "eventsUrl": reverse("professional_calendar_events"),
        "availabilityEventsUrl": reverse("professional_calendar_availability_events") if (is_admin or professional) else "",
        "bookingUrl": reverse("professional_book"),
        "clientsSearchUrl": reverse("professional_clients_search"),
        "professionalsByServiceUrl": reverse("api_professionals_by_service"),
        "availabilityOptionsUrl": reverse("professional_calendar_availability_options"),
        "slotsApiUrl": reverse("slots_api"),
        "blockSlotUrl": reverse("toggle_blocked_slot"),
        "quickCreateUrl": reverse("professional_calendar_quick_create"),
        "rescheduleContextUrl": reverse("professional_reschedule_context"),
        "createClientUrl": reverse("prof_customer_create"),
        "currentProfessionalId": professional.id if professional else None,
        "currentProfessionalName": (professional.user.get_full_name() or professional.user.username) if professional else "",
        "timezone": schedule_tz,
        "appointmentDetailUrlBase": "/prof/calendario/marcacao/",
        "appointmentConfirmUrlBase": "/prof/calendario/marcacao/",
        "canConfirmAll": bool(is_admin or is_reception),
        "groupSessionDetailUrlBase": "/backoffice/turmas/" if is_admin else "/prof/turmas/",
        "filtersEnabled": show_calendar_filters,
    }

    ctx = {
        "professional": professional,
        "is_admin": is_admin,
        "is_receptionist": is_reception,
        "is_client_calendar": False,
        "show_availability_toggle": bool(is_admin or professional),
        "week_start": week_start,
        "show_calendar_filters": show_calendar_filters,
        "services": services_with_colors,
        "professionals": professionals,
        "calendar_data": calendar_data,
    }
    return render(request, "backoffice/prof/calendar_fullcalendar_test.html", ctx)


@login_required
def client_calendar_fullcalendar_test_view(request):
    if can_access_backoffice(request.user) or Professional.objects.filter(user=request.user).exists():
        return redirect("professional_calendar")

    try:
        client_profile = request.user.client_profile
    except ClientProfile.DoesNotExist:
        return redirect(f"/perfil/?next={request.path}")

    blocked, retry_after = check_rate_limit(
        request,
        name="calendar_ip_minute",
        limit=120,
        window=60,
        by_ip=True,
    )
    if blocked:
        if is_json_request(request):
            return rate_limited_response(
                request,
                "Demasiadas tentativas. Tenta novamente em alguns minutos.",
                retry_after,
            )
        messages.error(request, "Demasiadas tentativas. Tenta novamente em alguns minutos.")
        response = render(request, "calendar_fullcalendar_test.html", {"is_admin": False}, status=429)
        response["Retry-After"] = str(retry_after)
        return response

    today = timezone.localdate()
    now_t = timezone.localtime().time()
    base_date = today
    week_param = (request.GET.get("week") or "").strip()
    session_week = (request.session.get("client_calendar_week") or "").strip()
    selected_week = week_param or session_week

    if selected_week:
        try:
            base_date = datetime.strptime(selected_week, "%Y-%m-%d").date()
        except ValueError:
            base_date = today
            request.session.pop("client_calendar_week", None)

    if week_param:
        request.session["client_calendar_week"] = base_date.strftime("%Y-%m-%d")

    if not week_param:
        current_week_start = _monday_of_week(today)
        base_week_start = _monday_of_week(base_date)
        is_current_week = base_week_start == current_week_start

        if is_current_week:
            last_weekday, last_end = _get_clinic_last_weekday_and_end(today)
            if last_weekday is not None:
                if today.weekday() > last_weekday:
                    base_date = today + timedelta(days=7)
                    request.session["client_calendar_week"] = base_date.strftime("%Y-%m-%d")
                elif today.weekday() == last_weekday and last_end and now_t >= last_end:
                    base_date = today + timedelta(days=7)
                    request.session["client_calendar_week"] = base_date.strftime("%Y-%m-%d")

    services_with_colors, service_colors = _calendar_service_colors()
    professionals = Professional.objects.select_related("user").order_by("user__username")

    week_start = _monday_of_week(base_date)
    week_end = week_start + timedelta(days=6)
    qs = Appointment.objects.select_related(
        "client", "service", "professional", "professional__user", "client__client_profile", "partner"
    ).filter(client=request.user, date__range=(week_start, week_end)).exclude(status=Appointment.STATUS_CANCELLED)

    events = _serialize_calendar_events(qs, service_colors)

    calendar_data = {
        "defaultView": "week",
        "baseDate": base_date.strftime("%Y-%m-%d"),
        "services": services_with_colors,
        "professionals": [
            {
                "id": p.id,
                "name": p.user.get_full_name() or p.user.username,
            }
            for p in professionals
        ],
        "events": events,
        "eventsUrl": reverse("client_calendar_events"),
        "availabilityEventsUrl": reverse("client_calendar_availability_events"),
        "bookingUrl": reverse("book"),
        "clientsSearchUrl": "",
        "professionalsByServiceUrl": reverse("api_professionals_by_service"),
        "availabilityOptionsUrl": reverse("client_calendar_availability_options"),
        "slotsApiUrl": reverse("slots_api"),
        "blockSlotUrl": "",
        "quickCreateUrl": reverse("client_calendar_quick_create"),
        "rescheduleContextUrl": "",
        "createClientUrl": "",
        "currentProfessionalId": None,
        "timezone": "Europe/Lisbon",
        "canConfirmAll": False,
        "filtersEnabled": True,
        "clientProfileId": client_profile.id,
        "clientMode": True,
        "appointmentDetailUrlBase": "/minhas-marcacoes/",
    }

    ctx = {
        "professional": None,
        "is_admin": False,
        "is_client_calendar": True,
        "show_calendar_filters": True,
        "client_profile_id": client_profile.id,
        "week_start": week_start,
        "services": services_with_colors,
        "professionals": professionals,
        "calendar_data": calendar_data,
    }
    return render(request, "calendar_fullcalendar_test.html", ctx)


def professional_calendar_events_view(request):
    is_admin = can_view_all_calendar(request.user)
    professional = None
    if not is_admin:
        professional = _get_professional_or_403(request.user)
        if professional is None:
            return JsonResponse({"events": []}, status=403)
    else:
        professional = Professional.objects.filter(user=request.user).first()

    if is_admin:
        ensure_group_sessions_for_schedules()
    elif professional:
        ensure_group_sessions_for_schedules(
            schedules=GroupSchedule.objects.filter(professional=professional, is_active=True)
            .select_related("service", "professional")
        )

    def _parse_date(value):
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except Exception:
            return None

    start = _parse_date((request.GET.get("start") or "").strip())
    end = _parse_date((request.GET.get("end") or "").strip())
    if not start or not end:
        return JsonResponse({"events": []})

    _, service_colors = _calendar_service_colors()

    qs = Appointment.objects.select_related(
        "client", "service", "professional", "professional__user", "client__client_profile", "partner"
    ).filter(date__range=(start, end)).exclude(status=Appointment.STATUS_CANCELLED)
    if not is_admin and professional:
        qs = qs.filter(professional=professional)

    service_ids = request.GET.getlist("service_id")
    if not service_ids:
        service_ids = [s for s in (request.GET.get("service_ids") or "").split(",") if s]
    if service_ids:
        qs = qs.filter(service_id__in=service_ids)

    selected_professional_ids = [p for p in request.GET.getlist("professional_id") if p]
    if not selected_professional_ids:
        selected_professional_id = (request.GET.get("professional_id") or "").strip()
        if selected_professional_id:
            selected_professional_ids = [selected_professional_id]
    if not selected_professional_ids:
        selected_professional_ids = [p for p in (request.GET.get("professional_ids") or "").split(",") if p]
    view_all = (request.GET.get("view_all") or "").strip()
    if is_admin and selected_professional_ids and not view_all:
        qs = qs.filter(professional_id__in=selected_professional_ids)

    selected_status = (request.GET.get("status") or "").strip()
    if selected_status:
        qs = qs.filter(status=selected_status)

    q = (request.GET.get("q") or "").strip()
    if q:
        qs = apply_terms_filter(
            qs,
            q,
            [
                "client__username__icontains",
                "client__client_profile__full_name__icontains",
                "client__client_profile__phone__icontains",
            ],
        )

    group_qs = GroupSession.objects.select_related(
        "service", "professional", "professional__user", "schedule"
    ).filter(date__range=(start, end))
    if not is_admin and professional:
        group_qs = group_qs.filter(professional=professional)
    if service_ids:
        group_qs = group_qs.filter(service_id__in=service_ids)
    if is_admin and selected_professional_ids and not view_all:
        group_qs = group_qs.filter(professional_id__in=selected_professional_ids)
    if selected_status:
        group_qs = group_qs.filter(status=selected_status)
    if q:
        group_qs = apply_terms_filter(
            group_qs,
            q,
            [
                "service__name__icontains",
                "professional__user__first_name__icontains",
                "professional__user__last_name__icontains",
            ],
        )

    events = _serialize_calendar_events(qs.order_by("date", "time", "id"), service_colors)
    events += _serialize_group_session_events(group_qs.order_by("date", "time", "id"), service_colors)
    blocked_qs = BlockedSlot.objects.filter(date__range=(start, end))
    if not is_admin and professional:
        blocked_qs = blocked_qs.filter(professional=professional)
    if is_admin and selected_professional_ids and not view_all:
        blocked_qs = blocked_qs.filter(professional_id__in=selected_professional_ids)
    events += _serialize_blocked_slot_events(blocked_qs.order_by("date", "time", "id"))
    events += _serialize_holiday_events(start, end)
    return JsonResponse({"events": events})


@login_required
def client_calendar_events_view(request):
    try:
        request.user.client_profile
    except ClientProfile.DoesNotExist:
        return JsonResponse({"events": []}, status=403)

    start_str = (request.GET.get("start") or "").strip()
    end_str = (request.GET.get("end") or "").strip()
    service_ids = request.GET.getlist("service_id")
    professional_id = (request.GET.get("professional_id") or "").strip()

    qs = Appointment.objects.select_related(
        "client", "service", "professional", "professional__user", "client__client_profile", "partner"
    ).filter(client=request.user).exclude(status=Appointment.STATUS_CANCELLED)

    holiday_events = []
    if start_str and end_str:
        try:
            start_date = datetime.strptime(start_str, "%Y-%m-%d").date()
            end_date = datetime.strptime(end_str, "%Y-%m-%d").date()
            qs = qs.filter(date__range=(start_date, end_date))
            holiday_events = _serialize_holiday_events(start_date, end_date)
        except ValueError:
            pass

    if service_ids:
        qs = qs.filter(service_id__in=service_ids)
    if professional_id:
        qs = qs.filter(professional_id=professional_id)

    _, service_colors = _calendar_service_colors()
    events = _serialize_calendar_events(qs.order_by("date", "time", "id"), service_colors)
    events += holiday_events
    return JsonResponse({"events": events})


@login_required
def professional_calendar_availability_events_view(request):
    is_admin = can_view_all_calendar(request.user)
    professional = (
        Professional.objects
        .select_related("user")
        .prefetch_related("services")
        .filter(user=request.user)
        .first()
    )
    if not is_admin and not professional:
        return JsonResponse({"events": []}, status=403)

    def _parse_date(value):
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except Exception:
            return None

    start = _parse_date((request.GET.get("start") or "").strip())
    end = _parse_date((request.GET.get("end") or "").strip())
    if not start or not end:
        return JsonResponse({"events": []})

    service_ids = request.GET.getlist("service_id")
    if not service_ids:
        service_ids = [s for s in (request.GET.get("service_ids") or "").split(",") if s]
    professional_ids = request.GET.getlist("professional_id")
    if not professional_ids:
        professional_ids = [s for s in (request.GET.get("professional_ids") or "").split(",") if s]

    if is_admin:
        professionals_qs = (
            Professional.objects
            .select_related("user")
            .prefetch_related("services")
            .order_by("user__username")
        )
        if professional_ids:
            professionals_qs = professionals_qs.filter(id__in=professional_ids)
        professionals = list(professionals_qs)
        services_qs = Service.objects.exclude(service_type="group").order_by("name")
    else:
        professionals = [professional]
        services_qs = Service.objects.exclude(service_type="group").filter(
            id__in=professional.services.values_list("id", flat=True)
        ).order_by("name")

    if service_ids:
        services_qs = services_qs.filter(id__in=service_ids)
    services = list(services_qs)
    if not services or not professionals:
        return JsonResponse({"events": []})

    _, service_colors = _calendar_service_colors()
    appointments_qs = (
        Appointment.objects
        .filter(professional__in=professionals, date__range=(start, end))
        .exclude(status=Appointment.STATUS_CANCELLED)
        .select_related("service")
    )
    group_sessions_qs = (
        GroupSession.objects
        .filter(professional__in=professionals, date__range=(start, end), status=GroupSession.STATUS_SCHEDULED)
        .select_related("service")
    )
    blocked_qs = (
        BlockedSlot.objects
        .filter(professional__in=professionals, date__range=(start, end))
        .values_list("professional_id", "date", "time")
    )
    occupied_by_prof_day = defaultdict(list)
    blocked_by_prof_day = defaultdict(set)
    for appointment in appointments_qs:
        duration = getattr(appointment.service, "duration_minutes", None) or 30
        end_dt = datetime.combine(appointment.date, appointment.time) + timedelta(minutes=duration)
        occupied_by_prof_day[(appointment.professional_id, appointment.date)].append(
            (appointment.time, end_dt.time())
        )
    for session in group_sessions_qs:
        duration = session.duration_minutes or getattr(session.service, "duration_minutes", None) or 60
        end_dt = datetime.combine(session.date, session.time) + timedelta(minutes=duration)
        occupied_by_prof_day[(session.professional_id, session.date)].append(
            (session.time, end_dt.time())
        )
    for prof_id, day, tm in blocked_qs:
        blocked_by_prof_day[(prof_id, day)].add(tm)

    def _daterange(start_date, end_date):
        current = start_date
        while current <= end_date:
            yield current
            current += timedelta(days=1)

    events = []
    for prof in professionals:
        prof_name = prof.user.get_full_name() or prof.user.username
        prof_service_ids = set(prof.services.values_list("id", flat=True))
        relevant_services = [s for s in services if s.id in prof_service_ids]
        if not relevant_services:
            continue
        for day in _daterange(start, end):
            occupied = occupied_by_prof_day.get((prof.id, day), [])
            blocked = blocked_by_prof_day.get((prof.id, day), set())
            for service in relevant_services:
                slots = build_slots(
                    prof,
                    day,
                    service_duration_minutes=service.duration_minutes,
                    blocked_slots=blocked,
                    occupied_intervals=occupied,
                )
                for slot in slots:
                    start_dt = datetime.combine(day, datetime.strptime(slot, "%H:%M").time())
                    end_dt = start_dt + timedelta(minutes=service.duration_minutes)
                    base_color = service_colors.get(str(service.id), "#5485e4")
                    color = _professional_service_color(base_color, prof.id)
                    events.append(
                        {
                            "id": f"prof-avail-{prof.id}-{day.isoformat()}-{slot}-{service.id}",
                            "calendarId": str(service.id),
                            "title": f"{service.name} · {prof_name}",
                            "category": "time",
                            "start": start_dt.isoformat(),
                            "end": end_dt.isoformat(),
                            "color": "#ffffff",
                            "bgColor": color,
                            "dragBgColor": color,
                            "borderColor": color,
                            "raw": {
                                "type": "availability",
                                "service_id": service.id,
                                "professional_ids": [prof.id],
                                "professional_id": prof.id,
                            },
                        }
                    )

    events += _serialize_holiday_events(start, end)
    return JsonResponse({"events": events})


@login_required
def client_calendar_availability_events_view(request):
    try:
        request.user.client_profile
    except ClientProfile.DoesNotExist:
        return JsonResponse({"events": []}, status=403)

    def _parse_date(value):
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except Exception:
            return None

    start = _parse_date((request.GET.get("start") or "").strip())
    end = _parse_date((request.GET.get("end") or "").strip())
    if not start or not end:
        return JsonResponse({"events": []})

    service_ids = request.GET.getlist("service_id")
    if not service_ids:
        service_ids = [s for s in (request.GET.get("service_ids") or "").split(",") if s]

    services_qs = Service.objects.exclude(service_type="group").order_by("name")
    if service_ids:
        services_qs = services_qs.filter(id__in=service_ids)
    services = list(services_qs)
    if not services:
        return JsonResponse({"events": []})

    service_map = {s.id: s for s in services}
    _, service_colors = _calendar_service_colors()

    professionals = (
        Professional.objects
        .select_related("user")
        .prefetch_related("services")
        .order_by("user__username")
    )

    def _daterange(start_date, end_date):
        current = start_date
        while current <= end_date:
            yield current
            current += timedelta(days=1)

    availability_map = {}
    for day in _daterange(start, end):
        for prof in professionals:
            prof_service_ids = set(prof.services.values_list("id", flat=True))
            relevant_services = [s for s in services if s.id in prof_service_ids]
            if not relevant_services:
                continue
            blocked = BlockedSlot.objects.filter(
                professional=prof,
                date=day,
            ).values_list("time", flat=True)
            occupied = _occupied_intervals_for_professional_day(prof, day)

            for service in relevant_services:
                slots = build_slots(
                    prof,
                    day,
                    service_duration_minutes=service.duration_minutes,
                    blocked_slots=blocked,
                    occupied_intervals=occupied,
                )
                for slot in slots:
                    key = (day, slot, service.id)
                    entry = availability_map.setdefault(
                        key,
                        {"professionals": set(), "professional_ids": set()},
                    )
                    entry["professionals"].add(prof.user.get_full_name() or prof.user.username)
                    entry["professional_ids"].add(prof.id)

    events = []
    for (day, slot, service_id), info in availability_map.items():
        service = service_map.get(service_id)
        if not service:
            continue
        start_dt = datetime.combine(day, datetime.strptime(slot, "%H:%M").time())
        end_dt = start_dt + timedelta(minutes=service.duration_minutes)
        prof_names = sorted(info["professionals"])
        title = service.name
        if prof_names:
            title = f"{service.name} · {', '.join(prof_names)}"
        color = service_colors.get(str(service_id), "#5485e4")
        events.append(
            {
                "id": f"avail-{day.isoformat()}-{slot}-{service_id}",
                "calendarId": str(service_id),
                "title": title,
                "category": "time",
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "color": "#ffffff",
                "bgColor": color,
                "dragBgColor": color,
                "borderColor": color,
                "raw": {
                    "type": "availability",
                    "service_id": service_id,
                    "professional_ids": list(info["professional_ids"]),
                },
            }
        )

    events += _serialize_holiday_events(start, end)
    return JsonResponse({"events": events})


def professional_clients_search_view(request):
    if not (can_view_all_calendar(request.user) or Professional.objects.filter(user=request.user).exists()):
        return HttpResponseForbidden("Acesso negado.")
    q = (request.GET.get("q") or "").strip()
    if len(q) < 2:
        return JsonResponse({"results": []})

    qs = ClientProfile.objects.select_related("user")
    qs = apply_terms_filter(
        qs,
        q,
        [
            "full_name__icontains",
            "nif__icontains",
            "phone__icontains",
            "user__username__icontains",
            "user__first_name__icontains",
            "user__last_name__icontains",
            "user__email__icontains",
        ],
    ).order_by("full_name")[:10]
    results = []
    for c in qs:
        user = c.user
        label = c.full_name or (user.get_full_name() if user else "") or (user.username if user else "—")
        results.append(
            {
                "id": c.id,
                "label": label,
                "nif": c.nif or "",
                "phone": c.phone or "",
                "has_user": bool(user),
                "user_id": user.id if user else None,
            }
        )
    return JsonResponse({"results": results})


def _ensure_client_user(client_profile):
    client_user = client_profile.user
    if client_user:
        return client_user

    base_username = "".join(ch for ch in (client_profile.nif or "") if ch.isdigit()) or f"cliente{client_profile.id}"
    username = base_username
    suffix = 1
    while User.objects.filter(username=username).exists():
        suffix += 1
        username = f"{base_username}{suffix}"
    client_user = User.objects.create_user(username=username, email="", password=None)
    client_user.set_unusable_password()
    client_user.is_active = False
    client_user.first_name = client_profile.full_name or ""
    client_user.save()
    group, _ = Group.objects.get_or_create(name="Cliente")
    client_user.groups.add(group)
    client_profile.user = client_user
    client_profile.save(update_fields=["user"])
    return client_user


@require_POST
def professional_calendar_quick_create_view(request):
    is_admin = can_view_all_calendar(request.user)
    prof = Professional.objects.filter(user=request.user).first()
    if not (is_admin or prof):
        return JsonResponse({"ok": False, "message": "Acesso restrito a profissionais."}, status=403)

    client_profile_id = (request.POST.get("client_profile_id") or "").strip()
    service_id = (request.POST.get("service_id") or "").strip()
    professional_id = (request.POST.get("professional_id") or "").strip()
    reschedule_id = (request.POST.get("reschedule_id") or "").strip()
    date_str = (request.POST.get("date") or "").strip()
    time_str = (request.POST.get("time") or "").strip()
    send_client_email_raw = (request.POST.get("send_client_email") or "1").strip().lower()
    send_client_email = send_client_email_raw in {"1", "true", "on", "yes"}

    if not (date_str and time_str):
        return JsonResponse({"ok": False, "message": "Dados incompletos."}, status=400)

    is_reschedule = bool(reschedule_id)
    appt_to_reschedule = None
    client_profile = None
    client_user = None
    service = None
    selected_prof = None

    if is_reschedule:
        appt_to_reschedule = get_object_or_404(
            Appointment.objects.select_related(
                "client",
                "client__client_profile",
                "service",
                "professional",
                "professional__user",
            ),
            id=reschedule_id,
        )
        if not can_modify_appointment(request.user, appt_to_reschedule):
            return JsonResponse({"ok": False, "message": "Não podes reagendar esta marcação."}, status=403)
        if appt_to_reschedule.status in {
            Appointment.STATUS_COMPLETED,
            Appointment.STATUS_IN_DEBT,
            Appointment.STATUS_CANCELLED,
            Appointment.STATUS_NO_SHOW,
        }:
            return JsonResponse(
                {"ok": False, "message": "Não podes reagendar uma marcação concluída, em dívida, cancelada ou em falta."},
                status=400,
            )
        if not appt_to_reschedule.service or not appt_to_reschedule.professional:
            return JsonResponse({"ok": False, "message": "Marcação inválida para reagendar."}, status=400)
        client_user = appt_to_reschedule.client
        client_profile = getattr(client_user, "client_profile", None)
        service = appt_to_reschedule.service
        selected_prof = appt_to_reschedule.professional
        requested_professional_id = professional_id or str(appt_to_reschedule.professional_id or "")
        if requested_professional_id:
            selected_prof = Professional.objects.filter(id=requested_professional_id).first()
        if not selected_prof:
            return JsonResponse({"ok": False, "message": "Profissional inválido."}, status=400)
        if not selected_prof.services.filter(id=service.id).exists():
            return JsonResponse({"ok": False, "message": "Este profissional não realiza este serviço."}, status=400)
    else:
        if not (client_profile_id and service_id):
            return JsonResponse({"ok": False, "message": "Dados incompletos."}, status=400)
        client_profile = get_object_or_404(ClientProfile, id=client_profile_id)
        service = get_object_or_404(Service, id=service_id)
        if is_admin:
            if not professional_id:
                return JsonResponse({"ok": False, "message": "Profissional obrigatório."}, status=400)
            selected_prof = get_object_or_404(Professional, id=professional_id)
        else:
            selected_prof = prof
        if not selected_prof.services.filter(id=service.id).exists():
            return JsonResponse({"ok": False, "message": "Este profissional não realiza este serviço."}, status=400)
        client_user = _ensure_client_user(client_profile)

    if service.service_type == "group":
        return JsonResponse({"ok": False, "message": "Serviço de turma não usa horários individuais."}, status=400)

    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
        time_obj = datetime.strptime(time_str, "%H:%M").time()
    except ValueError:
        return JsonResponse({"ok": False, "message": "Data ou hora inválida."}, status=400)

    if is_portuguese_holiday(date_obj):
        return JsonResponse({"ok": False, "message": "Não é possível marcar em feriado nacional."}, status=400)

    today = timezone.localdate()
    now_t = timezone.localtime().time()
    if date_obj < today:
        return JsonResponse({"ok": False, "message": "Não podes marcar no passado."}, status=400)
    if date_obj == today and time_obj <= now_t:
        return JsonResponse({"ok": False, "message": "Este horário já passou."}, status=400)

    is_current_reschedule_slot_context = bool(
        is_reschedule
        and appt_to_reschedule
        and appt_to_reschedule.professional_id == selected_prof.id
        and appt_to_reschedule.date == date_obj
        and appt_to_reschedule.time
    )
    is_current_reschedule_slot_selected = bool(
        is_current_reschedule_slot_context
        and appt_to_reschedule.time.strftime("%H:%M") == time_str
    )

    if _is_slot_blocked(selected_prof, date_obj, time_obj) and not is_current_reschedule_slot_selected:
        return JsonResponse({"ok": False, "message": "Este horário está indisponível."}, status=400)

    if not _has_availability_window(selected_prof, date_obj, time_obj) and not is_current_reschedule_slot_selected:
        return JsonResponse({"ok": False, "message": "Profissional não atende neste horário."}, status=400)

    valid_slots = _get_slots(selected_prof, date_obj, step_minutes=service.duration_minutes)
    if is_current_reschedule_slot_context:
        current_time = appt_to_reschedule.time.strftime("%H:%M")
        if current_time not in valid_slots:
            valid_slots = sorted(valid_slots + [current_time])

    if time_str not in valid_slots:
        return JsonResponse({"ok": False, "message": "Horário inválido para este serviço."}, status=400)

    occupied_qs = Appointment.objects.filter(
        professional=selected_prof,
        date=date_obj,
        time=time_obj,
    ).exclude(status=Appointment.STATUS_CANCELLED)
    if is_reschedule and appt_to_reschedule:
        occupied_qs = occupied_qs.exclude(id=appt_to_reschedule.id)
    if occupied_qs.exists():
        return JsonResponse({"ok": False, "message": "Este horário já está ocupado."}, status=400)

    settings_obj = clinic_settings()
    client_name_for_mail = (
        (getattr(client_profile, "full_name", "") or "").strip()
        or (client_user.get_full_name() or "").strip()
        or (client_user.username or "").strip()
    )
    client_phone_for_mail = (getattr(client_profile, "phone", "") or "").strip()

    if is_reschedule and appt_to_reschedule:
        old_date = appt_to_reschedule.date
        old_time = appt_to_reschedule.time
        old_status = appt_to_reschedule.status
        old_professional_id = appt_to_reschedule.professional_id
        appt_to_reschedule.date = date_obj
        appt_to_reschedule.time = time_obj
        appt_to_reschedule.professional = selected_prof
        appt_to_reschedule.status = Appointment.STATUS_SCHEDULED
        update_fields = ["date", "time", "status"]
        if old_professional_id != selected_prof.id:
            update_fields.append("professional")
        appt_to_reschedule.save(update_fields=update_fields)

        log_appt(
            AppointmentLog.ACTION_RESCHEDULED,
            appt_to_reschedule,
            request.user,
            old_date=old_date,
            old_time=old_time,
            new_date=appt_to_reschedule.date,
            new_time=appt_to_reschedule.time,
            old_status=old_status,
            new_status=appt_to_reschedule.status,
            request=request,
        )

        if settings_obj.notify_client_on_clinic_changes and send_client_email:
            client_email = (getattr(client_user, "email", "") or "").strip()
            if client_email:
                send_templated_email(
                    client_email,
                    f"Marcação reagendada — {settings_obj.clinic_name}",
                    "emails/appointment_changed_by_clinic.html",
                    "emails/appointment_changed_by_clinic.txt",
                    {
                        "client_name": client_name_for_mail,
                        "change_type": "rescheduled",
                        "old_date": old_date,
                        "old_time": old_time,
                        "new_date": appt_to_reschedule.date,
                        "new_time": appt_to_reschedule.time,
                        "service_name": service.name if service else "-",
                        "professional_name": selected_prof.user.get_full_name() or selected_prof.user.username,
                        "reason": "",
                        "manage_url": request.build_absolute_uri("/marcacoes/"),
                    },
                    event="reschedule_clinic",
                )
            else:
                log_email_skip("reschedule_clinic", "Marcação reagendada", "Cliente sem email.", "")

        return JsonResponse({"ok": True, "appointment_id": appt_to_reschedule.id, "rescheduled": True})

    pricing = compute_pricing(service, client_profile)
    appt = Appointment.objects.create(
        client=client_user,
        professional=selected_prof,
        service=service,
        date=date_obj,
        time=time_obj,
        symptomatology="",
        base_price=pricing["base_price_applied"],
        partner=pricing["partner"],
        partner_price=pricing["partner_price_applied"],
        discount_type=pricing["discount_type"],
        discount_value=pricing["discount_value"],
        final_price=pricing["final_price"],
        session_index=pricing["session_index"],
        pricing_tier=pricing["pricing_tier"],
        base_price_applied=pricing["base_price_applied"],
        partner_price_applied=pricing["partner_price_applied"],
        discount_applied=pricing["discount_applied"],
    )

    log_appt(
        AppointmentLog.ACTION_CREATED,
        appt,
        request.user,
        new_date=appt.date,
        new_time=appt.time,
        new_status=getattr(appt, "status", None),
        request=request,
    )

    clinic_to = clinic_email()
    if settings_obj.notify_clinic_on_new_booking and clinic_to:
        send_templated_email(
            clinic_to,
            f"Nova marcação — {service.name} — {appt.date} {appt.time}",
            "emails/clinic_appointment_event.html",
            "emails/clinic_appointment_event.txt",
            {
                "appointment": appt,
                "service_name": service.name,
                "client_name": client_name_for_mail,
                "professional_name": selected_prof.user.get_full_name() or selected_prof.user.username,
                "appointment_date": appt.date,
                "appointment_time": appt.time,
                "admin_url": request.build_absolute_uri("/prof/calendario/"),
            },
            event="new_booking",
        )

    if (
        settings_obj.notify_professional_on_new_booking
        and selected_prof.user_id != request.user.id
    ):
        prof_email = (getattr(selected_prof.user, "email", "") or "").strip()
        if prof_email:
            send_templated_email(
                prof_email,
                f"Nova marcação — {service.name} — {appt.date} {appt.time}",
                "emails/clinic_appointment_event.html",
                "emails/clinic_appointment_event.txt",
                {
                    "event_type": "new_booking",
                    "event_title": "Nova marcação",
                    "client_name": client_name_for_mail,
                    "client_phone": client_phone_for_mail,
                    "service_name": service.name,
                    "professional_name": selected_prof.user.get_full_name() or selected_prof.user.username,
                    "old_date": "",
                    "old_time": "",
                    "new_date": appt.date,
                    "new_time": appt.time,
                    "cancelled_at": "",
                    "actor": request.user.get_full_name() or request.user.username,
                    "admin_url": request.build_absolute_uri("/prof/calendario/"),
                },
                event="new_booking",
            )
        else:
            log_email_skip("new_booking", "Nova marcação", "Profissional sem email", "")

    if settings_obj.notify_client_on_new_booking and send_client_email:
        client_email = (getattr(client_user, "email", "") or "").strip()
        if client_email:
            send_templated_email(
                client_email,
                f"Marcação confirmada — {service.name} em {appt.date} {appt.time}",
                "emails/appointment_confirmed.html",
                "emails/appointment_confirmed.txt",
                {
                    "client_name": client_name_for_mail,
                    "service_name": service.name,
                    "professional_name": selected_prof.user.get_full_name() or selected_prof.user.username,
                    "date": appt.date,
                    "time": appt.time,
                    "symptomatology": appt.symptomatology,
                    "manage_url": request.build_absolute_uri(reverse("my_appointments")),
                },
                event="new_booking",
            )
        else:
            log_email_skip("new_booking", "Marcação confirmada", "Cliente sem email", "")

    return JsonResponse({"ok": True, "appointment_id": appt.id})


@require_GET
@login_required
def professional_reschedule_context_view(request):
    reschedule_id = (request.GET.get("reschedule_id") or "").strip()
    if not reschedule_id:
        return JsonResponse({"ok": False, "message": "Reagendamento inválido."}, status=400)

    appt = Appointment.objects.select_related(
        "client",
        "client__client_profile",
        "service",
        "professional",
        "professional__user",
    ).filter(id=reschedule_id).first()

    if not appt:
        return JsonResponse({"ok": False, "message": "Marcação não encontrada."}, status=404)
    if not can_modify_appointment(request.user, appt):
        return JsonResponse({"ok": False, "message": "Não tens permissão para reagendar esta marcação."}, status=403)
    if appt.status in {
        Appointment.STATUS_COMPLETED,
        Appointment.STATUS_IN_DEBT,
        Appointment.STATUS_CANCELLED,
        Appointment.STATUS_NO_SHOW,
    }:
        return JsonResponse({"ok": False, "message": "Não podes reagendar esta marcação."}, status=400)
    if not appt.service_id or not appt.professional_id:
        return JsonResponse({"ok": False, "message": "Marcação inválida para reagendar."}, status=400)

    client_profile = getattr(appt.client, "client_profile", None)
    client_label = (
        (getattr(client_profile, "full_name", "") or "").strip()
        or (appt.client.get_full_name() or "").strip()
        or (appt.client.username or "").strip()
    )
    professional_name = (appt.professional.user.get_full_name() or appt.professional.user.username or "").strip()

    return JsonResponse(
        {
            "ok": True,
            "appointment": {
                "id": appt.id,
                "client_profile_id": client_profile.id if client_profile else None,
                "client_user_id": appt.client_id,
                "client_label": client_label,
                "service_id": appt.service_id,
                "service_name": (appt.service.name or "").strip(),
                "professional_id": appt.professional_id,
                "professional_name": professional_name,
                "date": appt.date.strftime("%Y-%m-%d") if appt.date else "",
                "time": appt.time.strftime("%H:%M") if appt.time else "",
            },
        }
    )


@require_POST
@login_required
def client_calendar_quick_create_view(request):
    try:
        client_profile = request.user.client_profile
    except ClientProfile.DoesNotExist:
        return JsonResponse({"ok": False, "message": "Perfil de cliente em falta."}, status=400)

    service_id = (request.POST.get("service_id") or "").strip()
    professional_id = (request.POST.get("professional_id") or "").strip()
    date_str = (request.POST.get("date") or "").strip()
    time_str = (request.POST.get("time") or "").strip()

    if not (service_id and professional_id and date_str and time_str):
        return JsonResponse({"ok": False, "message": "Dados incompletos."}, status=400)

    service = get_object_or_404(Service, id=service_id)
    if service.service_type == "group":
        return JsonResponse({"ok": False, "message": "Serviço de turma não usa horários individuais."}, status=400)

    selected_prof = get_object_or_404(Professional, id=professional_id)
    if not selected_prof.services.filter(id=service.id).exists():
        return JsonResponse({"ok": False, "message": "Este profissional não realiza este serviço."}, status=400)

    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
        time_obj = datetime.strptime(time_str, "%H:%M").time()
    except ValueError:
        return JsonResponse({"ok": False, "message": "Data ou hora inválida."}, status=400)

    if is_portuguese_holiday(date_obj):
        return JsonResponse({"ok": False, "message": "Não é possível marcar em feriado nacional."}, status=400)

    today = timezone.localdate()
    now_t = timezone.localtime().time()
    if date_obj < today:
        return JsonResponse({"ok": False, "message": "Não podes marcar no passado."}, status=400)
    if date_obj == today and time_obj <= now_t:
        return JsonResponse({"ok": False, "message": "Este horário já passou."}, status=400)

    if _is_slot_blocked(selected_prof, date_obj, time_obj):
        return JsonResponse({"ok": False, "message": "Este horário está indisponível."}, status=400)

    if not _has_availability_window(selected_prof, date_obj, time_obj):
        return JsonResponse({"ok": False, "message": "Profissional não atende neste horário."}, status=400)

    valid_slots = _get_slots(selected_prof, date_obj, step_minutes=service.duration_minutes)
    if time_str not in valid_slots:
        return JsonResponse({"ok": False, "message": "Horário inválido para este serviço."}, status=400)

    if _is_slot_occupied(selected_prof, date_obj, time_obj):
        return JsonResponse({"ok": False, "message": "Este horário já está ocupado."}, status=400)

    pricing = compute_pricing(service, client_profile)
    appt = Appointment.objects.create(
        client=request.user,
        professional=selected_prof,
        service=service,
        date=date_obj,
        time=time_obj,
        symptomatology="",
        status=Appointment.STATUS_PENDING,
        base_price=pricing["base_price_applied"],
        partner=pricing["partner"],
        partner_price=pricing["partner_price_applied"],
        discount_type=pricing["discount_type"],
        discount_value=pricing["discount_value"],
        final_price=pricing["final_price"],
        session_index=pricing["session_index"],
        pricing_tier=pricing["pricing_tier"],
        base_price_applied=pricing["base_price_applied"],
        partner_price_applied=pricing["partner_price_applied"],
        discount_applied=pricing["discount_applied"],
    )

    log_appt(
        AppointmentLog.ACTION_CREATED,
        appt,
        request.user,
        new_date=appt.date,
        new_time=appt.time,
        new_status=getattr(appt, "status", None),
        request=request,
    )

    settings_obj = clinic_settings()
    clinic_to = clinic_email()
    if settings_obj.notify_clinic_on_new_booking and clinic_to:
        send_templated_email(
            clinic_to,
            f"Marcação em confirmação — {service.name} — {appt.date} {appt.time}",
            "emails/clinic_appointment_event.html",
            "emails/clinic_appointment_event.txt",
            {
                "event_type": "pending_confirmation",
                "event_title": "Marcação em confirmação",
                "client_name": request.user.get_full_name() or request.user.username,
                "client_phone": getattr(getattr(request.user, "client_profile", None), "phone", ""),
                "service_name": service.name,
                "professional_name": selected_prof.user.get_full_name() or selected_prof.user.username,
                "old_date": "",
                "old_time": "",
                "new_date": appt.date,
                "new_time": appt.time,
                "cancelled_at": "",
                "actor": "Cliente",
                "admin_url": request.build_absolute_uri("/prof/calendario/"),
            },
            event="new_booking",
        )

    if settings_obj.notify_professional_on_new_booking:
        prof_email = getattr(selected_prof.user, "email", "")
        if prof_email:
            send_templated_email(
                prof_email,
                f"Marcação em confirmação — {service.name} — {appt.date} {appt.time}",
                "emails/clinic_appointment_event.html",
                "emails/clinic_appointment_event.txt",
                {
                    "event_type": "pending_confirmation",
                    "event_title": "Marcação em confirmação",
                    "client_name": request.user.get_full_name() or request.user.username,
                    "client_phone": getattr(getattr(request.user, "client_profile", None), "phone", ""),
                    "service_name": service.name,
                    "professional_name": selected_prof.user.get_full_name() or selected_prof.user.username,
                    "old_date": "",
                    "old_time": "",
                    "new_date": appt.date,
                    "new_time": appt.time,
                    "cancelled_at": "",
                    "actor": "Cliente",
                    "admin_url": "",
                },
                event="new_booking",
            )
        else:
            log_email_skip("new_booking", "Marcação em confirmação", "Profissional sem email", "")

    client_email = request.user.email or ""
    if client_email:
        send_templated_email(
            client_email,
            f"Pedido de marcação recebido — {service.name} em {appt.date} {appt.time}",
            "emails/appointment_pending_confirmation.html",
            "emails/appointment_pending_confirmation.txt",
            {
                "client_name": request.user.get_full_name() or request.user.username,
                "service_name": service.name,
                "professional_name": selected_prof.user.get_full_name() or selected_prof.user.username,
                "date": appt.date,
                "time": appt.time,
                "symptomatology": "",
                "is_reschedule": False,
                "manage_url": request.build_absolute_uri(reverse("my_appointments")),
            },
            event="new_booking",
        )
    else:
        log_email_skip("new_booking", "Pedido de marcação em confirmação", "Cliente sem email", "")

    return JsonResponse({"ok": True, "appointment_id": appt.id})


def _parse_window_minutes(raw_value):
    try:
        window = int(raw_value)
    except (TypeError, ValueError):
        window = 60
    return max(15, min(window, 180))


def _filter_slots_in_window(slots, start_time, window_minutes):
    start_minutes = start_time.hour * 60 + start_time.minute
    end_minutes = start_minutes + window_minutes
    filtered = []
    for slot in slots:
        try:
            slot_h, slot_m = (slot or "").split(":")
            slot_minutes = int(slot_h) * 60 + int(slot_m)
        except Exception:
            continue
        if start_minutes <= slot_minutes < end_minutes:
            filtered.append(slot)
    return filtered


def _calendar_availability_options_payload(professionals, services, date_obj, time_obj, service_id, window_minutes):
    if is_portuguese_holiday(date_obj):
        return {
            "services": [],
            "professionals": [],
            "selected_service_id": str(service_id or ""),
            "message": "Feriado nacional: marcações indisponíveis.",
        }

    available_services = []
    professionals_payload = []
    slots_cache = {}

    for service in services:
        has_service_slots = False
        for prof in professionals:
            if service.id not in prof._service_ids:
                continue
            cache_key = (prof.id, service.id)
            if cache_key not in slots_cache:
                all_slots = _get_slots(prof, date_obj, step_minutes=service.duration_minutes)
                slots_cache[cache_key] = _filter_slots_in_window(all_slots, time_obj, window_minutes)
            if slots_cache[cache_key]:
                has_service_slots = True
                break
        if has_service_slots:
            available_services.append(service)

    selected_service = next((svc for svc in services if str(svc.id) == service_id), None)
    if selected_service:
        for prof in professionals:
            if selected_service.id not in prof._service_ids:
                continue
            cache_key = (prof.id, selected_service.id)
            if cache_key not in slots_cache:
                all_slots = _get_slots(prof, date_obj, step_minutes=selected_service.duration_minutes)
                slots_cache[cache_key] = _filter_slots_in_window(all_slots, time_obj, window_minutes)
            if not slots_cache[cache_key]:
                continue
            professionals_payload.append(
                {
                    "id": prof.id,
                    "label": prof.user.get_full_name() or prof.user.username,
                }
            )

    if not available_services:
        message = "Não há serviços disponíveis para este período."
    elif selected_service and not professionals_payload:
        message = "Não há profissionais disponíveis para este horário."
    else:
        message = ""

    return {
        "ok": True,
        "message": message,
        "services": [{"id": s.id, "name": s.name} for s in available_services],
        "professionals": professionals_payload,
    }


@login_required
def professional_calendar_availability_options_view(request):
    date_str = (request.GET.get("date") or "").strip()
    time_str = (request.GET.get("time") or "").strip()
    service_id = (request.GET.get("service_id") or "").strip()
    window_minutes = _parse_window_minutes((request.GET.get("window_minutes") or "").strip())

    if not (date_str and time_str):
        return JsonResponse({"ok": False, "message": "Dados incompletos.", "services": [], "professionals": []})

    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
        time_obj = datetime.strptime(time_str, "%H:%M").time()
    except ValueError:
        return JsonResponse({"ok": False, "message": "Data ou hora inválida.", "services": [], "professionals": []})

    is_admin = can_view_all_calendar(request.user)
    prof = Professional.objects.filter(user=request.user).first()
    if not (is_admin or prof):
        return JsonResponse({"ok": False, "message": "Acesso restrito.", "services": [], "professionals": []}, status=403)

    professionals_qs = Professional.objects.select_related("user").prefetch_related("services")
    if not is_admin:
        professionals_qs = professionals_qs.filter(id=prof.id)

    professionals = list(professionals_qs)
    for professional in professionals:
        professional._service_ids = {s.id for s in professional.services.all()}

    services = list(Service.objects.exclude(service_type="group").order_by("name"))
    payload = _calendar_availability_options_payload(
        professionals=professionals,
        services=services,
        date_obj=date_obj,
        time_obj=time_obj,
        service_id=service_id,
        window_minutes=window_minutes,
    )
    return JsonResponse(payload)


@login_required
def client_calendar_availability_options_view(request):
    date_str = (request.GET.get("date") or "").strip()
    time_str = (request.GET.get("time") or "").strip()
    service_id = (request.GET.get("service_id") or "").strip()
    window_minutes = _parse_window_minutes((request.GET.get("window_minutes") or "").strip())

    if not (date_str and time_str):
        return JsonResponse({"ok": False, "message": "Dados incompletos.", "services": [], "professionals": []})

    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
        time_obj = datetime.strptime(time_str, "%H:%M").time()
    except ValueError:
        return JsonResponse({"ok": False, "message": "Data ou hora inválida.", "services": [], "professionals": []})

    professionals = list(Professional.objects.select_related("user").prefetch_related("services"))
    for professional in professionals:
        professional._service_ids = {s.id for s in professional.services.all()}

    services = list(Service.objects.exclude(service_type="group").order_by("name"))
    payload = _calendar_availability_options_payload(
        professionals=professionals,
        services=services,
        date_obj=date_obj,
        time_obj=time_obj,
        service_id=service_id,
        window_minutes=window_minutes,
    )
    return JsonResponse(payload)


def toggle_blocked_slot_view(request):
    prof = _get_professional_or_403(request.user)
    is_staff = request.user.is_staff
    can_view_all = can_view_all_calendar(request.user)
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    professional_id = (request.POST.get("professional_id") or "").strip()
    date_str = (request.POST.get("date") or "").strip()
    time_str = (request.POST.get("time") or "").strip()
    week = (request.POST.get("week") or "").strip()

    def _respond(ok, message, *, blocked=None, status=200, action="", slot=None):
        if is_ajax:
            payload = {
                "ok": bool(ok),
                "message": message,
                "blocked": blocked,
                "action": action,
                "professional_id": slot.professional_id if slot else (target_prof.id if "target_prof" in locals() else None),
                "date": date_obj.isoformat() if "date_obj" in locals() else date_str,
                "time": time_obj.strftime("%H:%M") if "time_obj" in locals() else time_str,
                "blocked_id": slot.id if slot else None,
            }
            return JsonResponse(payload, status=status)
        if ok:
            messages.success(request, message)
        else:
            messages.error(request, message)
        if week:
            return redirect(f"/prof/calendario/?week={week}")
        return redirect("professional_calendar")

    if not date_str or not time_str:
        return _respond(False, "Dados inválidos.", status=400)

    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
        time_obj = datetime.strptime(time_str, "%H:%M").time()
    except ValueError:
        return _respond(False, "Dados inválidos.", status=400)

    if can_view_all:
        if not professional_id:
            return _respond(False, "Profissional obrigatório.", status=400)
        target_prof = get_object_or_404(Professional, id=professional_id)
    else:
        if prof is None:
            return _respond(False, "Acesso restrito a profissionais.", status=403)
        target_prof = prof

    existing = BlockedSlot.objects.filter(
        professional=target_prof,
        date=date_obj,
        time=time_obj,
    ).first()

    if existing:
        if not (is_staff or existing.created_by_id == request.user.id):
            log_audit_event(
                category="blocked_slot",
                action="remove_denied",
                request=request,
                actor=request.user,
                instance=existing,
                source="calendar_block",
                message="Tentativa sem permissão para remover bloqueio.",
                metadata={
                    "professional_id": target_prof.id,
                    "date": date_obj.isoformat(),
                    "time": time_obj.strftime("%H:%M"),
                },
            )
            return _respond(False, "Não tens permissão para remover este bloqueio.", blocked=True, status=403, action="remove_denied", slot=existing)
        existing_snapshot = {
            "professional_id": existing.professional_id,
            "date": existing.date.isoformat() if existing.date else "",
            "time": existing.time.strftime("%H:%M") if existing.time else "",
            "created_by_id": existing.created_by_id,
        }
        blocked_id = existing.id
        existing.delete()
        still_exists = BlockedSlot.objects.filter(id=blocked_id).exists()
        if still_exists:
            log_audit_event(
                category="blocked_slot",
                action="remove_failed",
                request=request,
                actor=request.user,
                source="calendar_block",
                message="Falha ao remover bloqueio.",
                metadata=existing_snapshot,
            )
            return _respond(False, "O bloqueio não foi removido corretamente.", blocked=True, status=500, action="remove_failed")
        log_audit_event(
            category="blocked_slot",
            action="remove",
            request=request,
            actor=request.user,
            source="calendar_block",
            message="Bloqueio removido.",
            before=existing_snapshot,
            metadata=existing_snapshot,
        )
        return _respond(True, "Bloqueio removido com sucesso.", blocked=False, action="removed")
    else:
        if not (is_staff or target_prof.user_id == request.user.id):
            log_audit_event(
                category="blocked_slot",
                action="create_denied",
                request=request,
                actor=request.user,
                source="calendar_block",
                message="Tentativa sem permissão para criar bloqueio.",
                metadata={
                    "professional_id": target_prof.id,
                    "date": date_obj.isoformat(),
                    "time": time_obj.strftime("%H:%M"),
                },
            )
            return _respond(False, "Não tens permissão para criar bloqueios neste Profissional.", blocked=False, status=403, action="create_denied")
        conflicting_appointment = (
            Appointment.objects.filter(
                professional=target_prof,
                date=date_obj,
                time=time_obj,
            )
            .exclude(status=Appointment.STATUS_CANCELLED)
            .order_by("id")
            .first()
        )
        conflicting_group_session = (
            GroupSession.objects.filter(
                professional=target_prof,
                date=date_obj,
                time=time_obj,
                status=GroupSession.STATUS_SCHEDULED,
            )
            .order_by("id")
            .first()
        )
        if conflicting_appointment or conflicting_group_session:
            conflict_metadata = {
                "professional_id": target_prof.id,
                "date": date_obj.isoformat(),
                "time": time_obj.strftime("%H:%M"),
            }
            if conflicting_appointment:
                conflict_metadata.update(
                    {
                        "conflict_type": "appointment",
                        "appointment_id": conflicting_appointment.id,
                        "appointment_status": conflicting_appointment.status,
                    }
                )
            elif conflicting_group_session:
                conflict_metadata.update(
                    {
                        "conflict_type": "group_session",
                        "group_session_id": conflicting_group_session.id,
                        "group_session_status": conflicting_group_session.status,
                    }
                )
            log_audit_event(
                category="blocked_slot",
                action="create_conflict",
                request=request,
                actor=request.user,
                source="calendar_block",
                message="Não foi possível bloquear: já existe uma marcação nesse horário.",
                metadata=conflict_metadata,
            )
            return _respond(False, "Já existe uma marcação nesse horário.", blocked=False, status=409, action="create_conflict")
        slot = BlockedSlot.objects.create(
            professional=target_prof,
            date=date_obj,
            time=time_obj,
            created_by=request.user,
        )
        persisted = BlockedSlot.objects.filter(id=slot.id).exists()
        if not persisted:
            log_audit_event(
                category="blocked_slot",
                action="create_failed",
                request=request,
                actor=request.user,
                source="calendar_block",
                message="Falha ao persistir bloqueio.",
                metadata={
                    "professional_id": target_prof.id,
                    "date": date_obj.isoformat(),
                    "time": time_obj.strftime("%H:%M"),
                },
            )
            return _respond(False, "O horário não ficou bloqueado no sistema.", blocked=False, status=500, action="create_failed")
        log_audit_event(
            category="blocked_slot",
            action="create",
            request=request,
            actor=request.user,
            instance=slot,
            source="calendar_block",
            message="Horário bloqueado.",
            after={
                "professional_id": slot.professional_id,
                "date": slot.date.isoformat() if slot.date else "",
                "time": slot.time.strftime("%H:%M") if slot.time else "",
                "created_by_id": slot.created_by_id,
            },
            metadata={
                "professional_id": slot.professional_id,
                "date": slot.date.isoformat() if slot.date else "",
                "time": slot.time.strftime("%H:%M") if slot.time else "",
            },
        )
        return _respond(True, "Horário bloqueado com sucesso.", blocked=True, action="created", slot=slot)


def professional_appointment_detail_view(request, appointment_id):
    appt = get_object_or_404(
        Appointment.objects.select_related(
            "client", "service", "professional", "professional__user"
        ),
        id=appointment_id,
    )

    if not can_modify_appointment(request.user, appt):
        return HttpResponseForbidden("Não tens acesso a esta marcação.")

    week = (request.GET.get("week") or "").strip()
    return_to = _safe_return_to(request, request.POST.get("return_to") or request.GET.get("return_to"))

    status_choices = list(Appointment.STATUS_CHOICES)

    if request.method == "POST":
        detail_before = snapshot_instance(
            appt,
            fields=[
                "status",
                "is_paid",
                "paid_at",
                "summary",
                "treatment_done",
                "final_price",
            ],
        )
        consumptions_before = _appointment_consumptions_snapshot(appt)
        status = (request.POST.get("status") or "").strip()
        is_paid = request.POST.get("is_paid") == "on"
        summary = (request.POST.get("summary") or "").strip()
        treatment_done = (request.POST.get("treatment_done") or "").strip()
        confirm_debt = request.POST.get("confirm_debt") == "1"

        product_ids = request.POST.getlist("consumable_product")
        qty_values = request.POST.getlist("consumable_quantity")
        consumption_errors = []
        consumption_items = []

        existing_totals = get_existing_consumption_totals(appt)

        max_len = max(len(product_ids), len(qty_values))
        for idx in range(max_len):
            pid = (product_ids[idx].strip() if idx < len(product_ids) else "")
            qty_raw = (qty_values[idx].strip() if idx < len(qty_values) else "")
            if not pid and not qty_raw:
                continue
            if not pid:
                consumption_errors.append("Indica o produto em todas as linhas de consumo.")
                continue
            product = Product.objects.filter(id=pid, is_active=True).first()
            if not product:
                consumption_errors.append("Produto inválido.")
                continue
            try:
                qty = Decimal(qty_raw.replace(",", "."))
            except Exception:
                consumption_errors.append("Quantidade inválida.")
                continue
            if qty <= 0:
                consumption_errors.append("A quantidade deve ser positiva.")
                continue

            available = get_stock(product) + (existing_totals.get(product.id) or Decimal("0.00"))
            if qty > available and not can_access_backoffice(request.user):
                consumption_errors.append(
                    f"Sem stock suficiente para {product.name}."
                )
                continue

            consumption_items.append((product, qty))

        valid_statuses = {c[0] for c in status_choices}
        if status and status not in valid_statuses:
            messages.error(request, "Estado inválido.")
        else:
            new_status = status if status else appt.status
            now_local = timezone.localtime()
            is_past_appointment = (
                appt.date < now_local.date()
                or (appt.date == now_local.date() and appt.time < now_local.time())
            )
            if new_status == Appointment.STATUS_IN_DEBT and not is_past_appointment:
                messages.error(request, "Só podes marcar uma consulta como 'Em dívida' após a hora da marcação.")
                params = {}
                if week:
                    params["week"] = week
                if return_to:
                    params["return_to"] = return_to
                target = reverse("professional_appointment_detail", args=[appt.id])
                if params:
                    return redirect(f"{target}?{urlencode(params)}")
                return redirect(target)
            if new_status in {Appointment.STATUS_AWAITING_VALIDATION, Appointment.STATUS_NO_SHOW} and not is_past_appointment:
                messages.error(request, "Só podes usar este estado após a hora da marcação.")
                params = {}
                if week:
                    params["week"] = week
                if return_to:
                    params["return_to"] = return_to
                target = reverse("professional_appointment_detail", args=[appt.id])
                if params:
                    return redirect(f"{target}?{urlencode(params)}")
                return redirect(target)
            if (
                is_past_appointment
                and new_status not in {
                    Appointment.STATUS_CANCELLED,
                    Appointment.STATUS_AWAITING_VALIDATION,
                    Appointment.STATUS_NO_SHOW,
                }
                and not is_paid
            ):
                if not confirm_debt:
                    messages.error(
                        request,
                        "Para guardar uma consulta sem pagamento, confirma primeiro a marcação como 'Em dívida'.",
                    )
                    params = {}
                    if week:
                        params["week"] = week
                    if return_to:
                        params["return_to"] = return_to
                    target = reverse("professional_appointment_detail", args=[appt.id])
                    if params:
                        return redirect(f"{target}?{urlencode(params)}")
                    return redirect(target)
                new_status = Appointment.STATUS_IN_DEBT

            if consumption_items or existing_totals:
                if new_status not in {Appointment.STATUS_COMPLETED, Appointment.STATUS_IN_DEBT}:
                    consumption_errors.append(
                        "Só podes registar consumos quando a marcação estiver concluída ou em dívida."
                    )

            if consumption_errors:
                for err in consumption_errors:
                    messages.error(request, err)
                params = {}
                if week:
                    params["week"] = week
                if return_to:
                    params["return_to"] = return_to
                target = reverse("professional_appointment_detail", args=[appt.id])
                if params:
                    return redirect(f"{target}?{urlencode(params)}")
                return redirect(target)

            updated_fields = []
            old_status = appt.status
            status_changed = False
            if appt.status != new_status:
                appt.status = new_status
                status_changed = True
                updated_fields.append("status")
                if appt.status in {Appointment.STATUS_COMPLETED, Appointment.STATUS_IN_DEBT}:
                    appt.completed_at = timezone.now()
                    appt.completed_by = request.user
                    updated_fields.extend(["completed_at", "completed_by"])
                elif appt.completed_at or appt.completed_by:
                    appt.completed_at = None
                    appt.completed_by = None
                    updated_fields.extend(["completed_at", "completed_by"])

            if appt.is_paid != is_paid:
                appt.is_paid = is_paid
                appt.paid_at = timezone.now() if is_paid else None
                updated_fields.extend(["is_paid", "paid_at"])

            if appt.summary != summary:
                appt.summary = summary
                updated_fields.append("summary")

            if appt.treatment_done != treatment_done:
                appt.treatment_done = treatment_done
                updated_fields.append("treatment_done")

            if updated_fields:
                appt.save(update_fields=updated_fields)
                if status_changed:
                    sync_subcontractor_payout(appt, actor=request.user)
                    log_appt(
                        AppointmentLog.ACTION_STATUS_UPDATED,
                        appt,
                        request.user,
                        old_status=old_status,
                        new_status=appt.status,
                        request=request,
                    )
                    settings_obj = clinic_settings()
                    if settings_obj.notify_client_on_clinic_changes:
                        client_email = (appt.client.email or "").strip()
                        if client_email:
                            if appt.status == Appointment.STATUS_SCHEDULED:
                                send_templated_email(
                                    client_email,
                                    f"Marcação confirmada — {appt.service.name} em {appt.date} {appt.time}",
                                    "emails/appointment_confirmed.html",
                                    "emails/appointment_confirmed.txt",
                                    {
                                        "client_name": appt.client.get_full_name() or appt.client.username,
                                        "service_name": appt.service.name if appt.service else "-",
                                        "professional_name": appt.professional.user.get_full_name() or appt.professional.user.username,
                                        "date": appt.date,
                                        "time": appt.time,
                                        "symptomatology": appt.symptomatology,
                                        "manage_url": request.build_absolute_uri(reverse("my_appointments")),
                                    },
                                    event="status_update",
                                )
                            elif appt.status == Appointment.STATUS_CANCELLED:
                                send_templated_email(
                                    client_email,
                                    f"Marcação cancelada — {settings_obj.clinic_name}",
                                    "emails/appointment_changed_by_clinic.html",
                                    "emails/appointment_changed_by_clinic.txt",
                                    {
                                        "client_name": appt.client.get_full_name() or appt.client.username,
                                        "change_type": "cancelled",
                                        "old_date": appt.date,
                                        "old_time": appt.time,
                                        "new_date": "",
                                        "new_time": "",
                                        "service_name": appt.service.name if appt.service else "-",
                                        "professional_name": appt.professional.user.get_full_name() or appt.professional.user.username,
                                        "reason": "",
                                        "manage_url": request.build_absolute_uri(reverse("my_appointments")),
                                    },
                                    event="status_update",
                                )
                        else:
                            log_email_skip("status_update", "Estado atualizado", "Cliente sem email", "")
                non_status_fields = {"is_paid", "paid_at", "summary", "treatment_done"}
                if any(field in non_status_fields for field in updated_fields):
                    log_audit_event(
                        category="appointment_detail",
                        action="update",
                        request=request,
                        instance=appt,
                        source="professional_appointment_detail",
                        message="Detalhes da marcação atualizados.",
                        before=detail_before,
                        after=snapshot_instance(
                            appt,
                            fields=[
                                "status",
                                "is_paid",
                                "paid_at",
                                "summary",
                                "treatment_done",
                                "final_price",
                            ],
                        ),
                    )
                messages.success(request, "Marcação atualizada.")

            # reconciliar consumos (se aplicável)
            if new_status in {Appointment.STATUS_COMPLETED, Appointment.STATUS_IN_DEBT}:
                normalized = {}
                product_map = {}
                for product, qty in consumption_items:
                    normalized[product.id] = normalized.get(product.id, Decimal("0.00")) + qty
                    product_map[product.id] = product
                if normalized != existing_totals:
                    reconcile_appointment_consumptions(
                        appt,
                        [(product_map[pid], qty) for pid, qty in normalized.items()],
                        user=request.user,
                    )
                    log_audit_event(
                        category="appointment_consumption",
                        action="reconcile",
                        request=request,
                        instance=appt,
                        source="professional_appointment_detail",
                        message="Consumos da marcação atualizados.",
                        before={"consumptions": consumptions_before},
                        after={"consumptions": _appointment_consumptions_snapshot(appt)},
                        metadata={"status": new_status},
                    )

        if return_to:
            return redirect(return_to)
        return redirect(
            f"{reverse('professional_appointment_detail', args=[appt.id])}{'?week=' + week if week else ''}"
        )

    can_confirm = (
        can_view_all_calendar(request.user)
        or (appt.professional and appt.professional.user_id == request.user.id)
    )
    now_local = timezone.localtime()
    requires_review_completion = (
        appt.date < now_local.date()
        or (appt.date == now_local.date() and appt.time < now_local.time())
    )

    consumptions = (
        AppointmentConsumption.objects
        .filter(appointment=appt)
        .select_related("product")
        .order_by("id")
    )
    products = Product.objects.filter(is_active=True).order_by("name")
    latest_log = (
        AppointmentLog.objects
        .filter(appointment=appt)
        .select_related("actor")
        .order_by("-created_at")
        .first()
    )
    created_log = (
        AppointmentLog.objects
        .filter(appointment=appt, action=AppointmentLog.ACTION_CREATED)
        .select_related("actor")
        .order_by("created_at")
        .first()
    )
    if not created_log:
        created_log = (
            AppointmentLog.objects
            .filter(appointment=appt)
            .select_related("actor")
            .order_by("created_at")
            .first()
        )

    recent_logs = list(
        AppointmentLog.objects
        .filter(appointment=appt)
        .select_related("actor")
        .order_by("-created_at")[:5]
    )

    def _actor_label(user_obj):
        if not user_obj:
            return "—"
        return user_obj.get_full_name() or user_obj.username

    audit_data = {
        "reference": f"MC-{appt.id:06d}",
        "created_at": appt.created_at,
        "created_by": _actor_label(created_log.actor if created_log else None),
        "last_changed_at": latest_log.created_at if latest_log else None,
        "last_changed_by": _actor_label(latest_log.actor if latest_log else None),
        "last_action": latest_log.get_action_display() if latest_log else "—",
        "recent_logs": recent_logs,
    }

    return render(
        request,
        "core/professional_appointment_detail.html",
        {
            "appointment": appt,
            "week": week,
            "status_choices": status_choices,
            "can_confirm_appointment": can_confirm,
            "can_view_professional_link": can_access_backoffice(request.user),
            "return_to": return_to,
            "requires_review_completion": requires_review_completion,
            "consumptions": consumptions,
            "products": products,
            "audit_data": audit_data,
        },
    )


@require_POST
def professional_confirm_appointment_view(request, appointment_id):
    appt = get_object_or_404(
        Appointment.objects.select_related(
            "client", "service", "professional", "professional__user"
        ),
        id=appointment_id,
    )

    can_confirm = (
        can_view_all_calendar(request.user)
        or (appt.professional and appt.professional.user_id == request.user.id)
    )
    if not can_confirm:
        return HttpResponseForbidden("Não tens acesso a esta marcação.")

    if appt.status in [
        Appointment.STATUS_CANCELLED,
        Appointment.STATUS_COMPLETED,
        Appointment.STATUS_IN_DEBT,
        Appointment.STATUS_NO_SHOW,
    ]:
        return HttpResponseForbidden("Não podes confirmar uma marcação cancelada, concluída, em dívida ou em falta.")

    old_status = appt.status
    if appt.status != Appointment.STATUS_SCHEDULED:
        appt.status = Appointment.STATUS_SCHEDULED
        appt.save(update_fields=["status"])
        log_appt(
            AppointmentLog.ACTION_STATUS_UPDATED,
            appt,
            request.user,
            old_status=old_status,
            new_status=appt.status,
            request=request,
        )
        settings_obj = clinic_settings()
        if settings_obj.notify_client_on_clinic_changes:
            client_email = (appt.client.email or "").strip()
            if client_email:
                send_templated_email(
                    client_email,
                    f"Marcação confirmada — {appt.service.name} em {appt.date} {appt.time}",
                    "emails/appointment_confirmed.html",
                    "emails/appointment_confirmed.txt",
                    {
                        "client_name": appt.client.get_full_name() or appt.client.username,
                        "service_name": appt.service.name if appt.service else "-",
                        "professional_name": appt.professional.user.get_full_name() or appt.professional.user.username,
                        "date": appt.date,
                        "time": appt.time,
                        "symptomatology": appt.symptomatology,
                        "manage_url": request.build_absolute_uri(reverse("my_appointments")),
                    },
                    event="status_update",
                )
            else:
                log_email_skip("status_update", "Marcação confirmada", "Cliente sem email", "")

    return_to = _safe_return_to(request, request.POST.get("return_to") or request.GET.get("return_to"))
    week = (request.GET.get("week") or "").strip()
    if return_to:
        return redirect(return_to)
    if week:
        return redirect(f"/prof/calendario/marcacao/{appt.id}/?week={week}")
    return redirect("professional_appointment_detail", appointment_id=appt.id)

def professional_cancel_appointment_view(request, appointment_id):
    if not can_view_all_calendar(request.user) and not Professional.objects.filter(user=request.user).exists():
        return HttpResponseForbidden("Acesso apenas para profissionais.")

    if can_view_all_calendar(request.user):
        appt = get_object_or_404(Appointment, id=appointment_id)
    else:
        professional = get_object_or_404(Professional, user=request.user)
        appt = get_object_or_404(Appointment, id=appointment_id, professional=professional)

    if appt.status in {Appointment.STATUS_COMPLETED, Appointment.STATUS_IN_DEBT, Appointment.STATUS_NO_SHOW}:
        return HttpResponseForbidden("Não podes cancelar uma marcação concluída, em dívida ou em falta.")

    # se houver choice "cancelled", usa; senão apaga (fallback)
    status_field = Appointment._meta.get_field("status")
    choices = [c[0] for c in (status_field.choices or [])]
    old_status = appt.status
    if "cancelled" in choices:
        appt.status = "cancelled"
        appt.save(update_fields=["status"])
    else:
        appt.delete()

    if old_status != getattr(appt, "status", old_status):
        log_appt(
            AppointmentLog.ACTION_CANCELLED,
            appt,
            request.user,
            old_status=old_status,
            new_status=getattr(appt, "status", None),
            request=request,
        )
    messages.success(request, "Marcação cancelada.")
    settings_obj = clinic_settings()
    if settings_obj.notify_client_on_clinic_changes:
        client_email = (appt.client.email or "").strip()
        if client_email:
            send_templated_email(
                client_email,
                f"Marcação cancelada — {settings_obj.clinic_name}",
                "emails/appointment_changed_by_clinic.html",
                "emails/appointment_changed_by_clinic.txt",
                {
                    "client_name": appt.client.get_full_name() or appt.client.username,
                    "change_type": "cancelled",
                    "old_date": appt.date,
                    "old_time": appt.time,
                    "new_date": "",
                    "new_time": "",
                    "service_name": appt.service.name if appt.service else "-",
                    "professional_name": appt.professional.user.get_full_name() or appt.professional.user.username,
                    "reason": "",
                    "manage_url": request.build_absolute_uri("/marcacoes/"),
                },
                event="cancel_clinic",
            )
        else:
            log_email_skip(
                "cancel_clinic",
                "Marcação cancelada",
                "Cliente sem email.",
            )
    if settings_obj.notify_clinic_on_client_cancel:
        clinic_to = clinic_email()
        if clinic_to:
            send_templated_email(
                clinic_to,
                f"Marcação cancelada — {appt.service.name if appt.service else '-'} — {appt.date} {appt.time}",
                "emails/clinic_appointment_event.html",
                "emails/clinic_appointment_event.txt",
                {
                    "event_type": "cancelled",
                    "event_title": "Marcação cancelada",
                    "client_name": appt.client.get_full_name() or appt.client.username,
                    "client_phone": getattr(getattr(appt.client, "client_profile", None), "phone", ""),
                    "service_name": appt.service.name if appt.service else "-",
                    "professional_name": appt.professional.user.get_full_name() or appt.professional.user.username,
                    "old_date": appt.date,
                    "old_time": appt.time,
                    "new_date": "",
                    "new_time": "",
                    "cancelled_at": timezone.localtime().strftime("%Y-%m-%d %H:%M"),
                    "actor": request.user.get_full_name() or request.user.username,
                    "admin_url": request.build_absolute_uri("/prof/calendario/"),
                },
                event="cancel_clinic",
            )
        else:
            log_email_skip(
                "cancel_clinic",
                "Marcação cancelada",
                "Email da clínica vazio.",
            )
    return_to = _safe_return_to(request, request.POST.get("return_to") or request.GET.get("return_to"))
    week = request.GET.get("week", "")
    if return_to:
        return redirect(return_to)
    if week:
        return redirect(f"/prof/calendario/?week={week}")
    return redirect("professional_calendar")


def professional_complete_appointment_view(request, appointment_id):
    if not can_view_all_calendar(request.user) and not Professional.objects.filter(user=request.user).exists():
        return HttpResponseForbidden("Acesso apenas para profissionais.")

    if can_view_all_calendar(request.user):
        appt = get_object_or_404(Appointment, id=appointment_id)
    else:
        professional = get_object_or_404(Professional, user=request.user)
        appt = get_object_or_404(Appointment, id=appointment_id, professional=professional)

    week = (request.GET.get("week") or "").strip()
    return_to = _safe_return_to(request, request.GET.get("return_to"))
    params = {}
    if week:
        params["week"] = week
    if return_to:
        params["return_to"] = return_to
    target = reverse("professional_appointment_detail", args=[appt.id])
    if params:
        return redirect(f"{target}?{urlencode(params)}")
    return redirect(target)


def professional_dashboard_view(request):
    """
    Dashboard fora do admin (profissional/staff).
    - lista agenda do dia
    - atalhos rápidos
    """
    today = timezone.localdate()

    # Se existir Professional ligado ao user, filtra por ele. Se for staff sem Professional, mostra todos.
    prof = None
    try:
        prof = Professional.objects.get(user=request.user)
    except Professional.DoesNotExist:
        prof = None

    appts = (
        Appointment.objects
        .filter(date=today)
        .select_related("client", "professional", "professional__user", "service")
        .order_by("time", "id")
    )
    if prof:
        appts = appts.filter(professional=prof)

    total = appts.count()
    scheduled = appts.filter(status="scheduled").count()
    completed = appts.filter(status="completed").count()

    return render(
        request,
        "core/professional_dashboard.html",
        {
            "today": today,
            "appointments": appts,
            "total": total,
            "scheduled": scheduled,
            "completed": completed,
            "prof": prof,
        },
    )
