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
from django.db.models import Q, Count, Sum
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
    is_admin_role,
)
from core.ratelimit import check_rate_limit, rate_limited_response, is_json_request, rate_limit
from core.emails import send_templated_email, clinic_email, clinic_settings, log_email_skip
from core.services.audit import log_audit_event, snapshot_instance
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
    GroupMembership,
    GroupMonthlyCharge,
    MoloniIntegration,
    ClientImportLog,
    ClientImportBatch,
    ClientImportRow,
    Partner,
    PartnerServicePrice,
    ContentPost,
)

from core.views.common import (
    update_group_sessions_statuses,
    group_booked_statuses,
    promote_group_waitlist,
    can_cancel_group_enrollment,
    ensure_group_sessions_for_schedules,
    ensure_group_monthly_charges,
    group_schedule_family_key,
    _get_professional_or_403,
    _safe_return_to,
)


GROUP_SCHEDULE_AUDIT_FIELDS = [
    "id",
    "service_id",
    "professional_id",
    "weekday",
    "time",
    "start_date",
    "capacity",
    "duration_minutes",
    "name",
    "notes",
    "is_active",
]

GROUP_SESSION_AUDIT_FIELDS = [
    "id",
    "service_id",
    "professional_id",
    "schedule_id",
    "date",
    "time",
    "capacity",
    "duration_minutes",
    "name",
    "notes",
    "status",
]

GROUP_ENROLLMENT_AUDIT_FIELDS = [
    "id",
    "session_id",
    "client_id",
    "status",
]

GROUP_MEMBERSHIP_AUDIT_FIELDS = [
    "id",
    "client_id",
    "service_id",
    "professional_id",
    "schedule_id",
    "family_key",
    "class_name",
    "weekdays",
    "monthly_price_override",
    "is_active",
]

GROUP_MONTHLY_CHARGE_AUDIT_FIELDS = [
    "id",
    "client_id",
    "service_id",
    "professional_id",
    "schedule_id",
    "family_key",
    "month",
    "base_price",
    "partner_id",
    "partner_price",
    "discount_type",
    "discount_value",
    "discount_applied",
    "final_price",
    "status",
    "paid_at",
]


def _schedule_snapshot(schedule):
    return snapshot_instance(schedule, GROUP_SCHEDULE_AUDIT_FIELDS)


def _session_snapshot(session):
    return snapshot_instance(session, GROUP_SESSION_AUDIT_FIELDS)


def _enrolment_snapshot(enrolment):
    return snapshot_instance(enrolment, GROUP_ENROLLMENT_AUDIT_FIELDS)


def _membership_snapshot(membership):
    return snapshot_instance(membership, GROUP_MEMBERSHIP_AUDIT_FIELDS)


def _monthly_charge_snapshot(charge):
    return snapshot_instance(charge, GROUP_MONTHLY_CHARGE_AUDIT_FIELDS)


def _ensure_group_client_user(client_profile):
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


def _family_schedule_snapshots(schedule_ids):
    return [
        _schedule_snapshot(item)
        for item in GroupSchedule.objects.filter(id__in=list(schedule_ids)).order_by("weekday", "id")
    ]


@login_required
def group_services_list_view(request):
    services = Service.objects.filter(service_type="group").order_by("name")
    return render(
        request,
        "core/group_services_list.html",
        {"services": services},
    )


@login_required
def group_sessions_list_view(request, service_id):
    service = get_object_or_404(Service, id=service_id, service_type="group")

    update_group_sessions_statuses()
    ensure_group_sessions_for_schedules(
        schedules=GroupSchedule.objects.filter(service=service, is_active=True)
        .select_related("service", "professional")
    )

    today = timezone.localdate()
    now_t = timezone.localtime().time()
    month_filter = (request.GET.get("month") or "").strip()
    only_available = (request.GET.get("only_available") or "").strip().lower() in {"1", "true", "on", "yes"}
    month_year = None
    month_value = None
    if month_filter:
        try:
            month_year = datetime.strptime(month_filter, "%Y-%m")
            month_value = month_year.strftime("%Y-%m")
        except Exception:
            month_year = None

    sessions = (
        GroupSession.objects
        .select_related("service", "professional", "professional__user")
        .filter(service=service, status=GroupSession.STATUS_SCHEDULED)
        .order_by("date", "time")
    )

    sessions = [s for s in sessions if (s.date > today) or (s.date == today and s.time > now_t)]
    if month_year:
        sessions = [
            s for s in sessions
            if s.date.year == month_year.year and s.date.month == month_year.month
        ]
    available_sessions = [s for s in sessions if s.spots_left > 0]
    has_full_sessions = any(s.spots_left <= 0 for s in sessions)
    if only_available:
        sessions = available_sessions

    enrolments = (
        GroupEnrollment.objects
        .filter(client=request.user, session__service=service)
        .exclude(status=GroupEnrollment.STATUS_CANCELLED)
        .values_list("session_id", "status")
    )
    enrolled_status = {session_id: status for session_id, status in enrolments}
    for s in sessions:
        s.user_status = enrolled_status.get(s.id)

    return render(
        request,
        "core/group_sessions_list.html",
        {
            "service": service,
            "sessions": sessions,
            "available_sessions_count": len(available_sessions),
            "has_full_sessions": has_full_sessions,
            "only_available": only_available,
            "available_months": (
                GroupSession.objects
                .filter(service=service, status=GroupSession.STATUS_SCHEDULED, date__gte=today)
                .dates("date", "month", order="ASC")
            ),
            "selected_month": month_value,
        },
    )


@login_required
@require_POST
def enroll_group_session_view(request, session_id):
    session = get_object_or_404(GroupSession.objects.select_related("service", "schedule"), id=session_id)
    if session.service.service_type != "group":
        return redirect("book")

    today = timezone.localdate()
    now_t = timezone.localtime().time()

    if session.status != GroupSession.STATUS_SCHEDULED:
        messages.error(request, "Esta turma já não está disponível.")
        return redirect("group_sessions_list", service_id=session.service_id)

    if session.date < today or (session.date == today and session.time <= now_t):
        messages.error(request, "Não podes inscrever-te em sessões no passado.")
        return redirect("group_sessions_list", service_id=session.service_id)

    with transaction.atomic():
        session = GroupSession.objects.select_related("schedule").select_for_update().get(id=session_id)
        target_sessions = list(_future_class_sessions_for_session(session, for_update=True))
        if not target_sessions:
            messages.error(request, "Não existem sessões futuras disponíveis nesta turma.")
            return redirect("group_sessions_list", service_id=session.service_id)

        existing_qs = (
            GroupEnrollment.objects
            .select_for_update()
            .filter(session__in=target_sessions, client=request.user)
        )
        existing_map = {enrol.session_id: enrol for enrol in existing_qs}
        active_statuses = {
            GroupEnrollment.STATUS_BOOKED,
            GroupEnrollment.STATUS_WAITLIST,
        }
        if all(
            existing_map.get(s.id) and existing_map[s.id].status in active_statuses
            for s in target_sessions
        ):
            messages.info(request, "Já estás inscrito nesta turma.")
            return redirect("group_sessions_list", service_id=session.service_id)

        can_book_all = True
        for target in target_sessions:
            existing = existing_map.get(target.id)
            if existing and existing.status in active_statuses:
                continue
            if target.spots_left <= 0:
                can_book_all = False
                break

        if can_book_all:
            new_status = GroupEnrollment.STATUS_BOOKED
        elif session.service.allow_waitlist:
            new_status = GroupEnrollment.STATUS_WAITLIST
        else:
            messages.error(request, "Esta turma já está cheia.")
            return redirect("group_sessions_list", service_id=session.service_id)

        for target in target_sessions:
            existing = existing_map.get(target.id)
            if existing:
                if existing.status != new_status:
                    existing.status = new_status
                    existing.save(update_fields=["status", "updated_at"])
            else:
                GroupEnrollment.objects.create(session=target, client=request.user, status=new_status)

    log_audit_event(
        category="group_enrollment",
        action="client_enroll",
        request=request,
        instance=session,
        source="group_sessions",
        message="Cliente inscrito em turma.",
        after=_session_snapshot(session),
        metadata={
            "client_id": request.user.id,
            "status": new_status,
            "session_ids": [item.id for item in target_sessions],
            "session_count": len(target_sessions),
        },
    )

    if new_status == GroupEnrollment.STATUS_WAITLIST:
        messages.info(request, "Entraste na lista de espera da turma.")
    elif len(target_sessions) > 1:
        messages.success(request, "Inscrição realizada em todas as sessões futuras da turma.")
    else:
        messages.success(request, "Inscrição realizada com sucesso.")
    return redirect("group_sessions_list", service_id=session.service_id)


@login_required
@require_POST
def cancel_group_session_enrollment_view(request, session_id):
    enrolment = get_object_or_404(
        GroupEnrollment.objects.select_related("session", "session__service"),
        session_id=session_id,
        client=request.user,
    )
    session = enrolment.session
    if enrolment.status == GroupEnrollment.STATUS_CANCELLED:
        messages.info(request, "A inscrição já foi cancelada.")
        return redirect("my_group_sessions")

    if session.status != GroupSession.STATUS_SCHEDULED:
        messages.error(request, "Esta sessão já não pode ser alterada.")
        return redirect("my_group_sessions")

    if not can_cancel_group_enrollment(request.user, session):
        messages.error(request, "Já não é possível cancelar esta inscrição.")
        return redirect("my_group_sessions")

    with transaction.atomic():
        session = GroupSession.objects.select_related("schedule").select_for_update().get(id=session.id)
        target_sessions = list(_future_class_sessions_for_session(session, for_update=True))
        target_enrolments = (
            GroupEnrollment.objects
            .select_for_update()
            .filter(
                session__in=target_sessions,
                client=request.user,
                status__in=[GroupEnrollment.STATUS_BOOKED, GroupEnrollment.STATUS_WAITLIST],
            )
        )
        updated = 0
        for item in target_enrolments:
            item.status = GroupEnrollment.STATUS_CANCELLED
            item.save(update_fields=["status", "updated_at"])
            updated += 1
        for target in target_sessions:
            promote_group_waitlist(target)

    log_audit_event(
        category="group_enrollment",
        action="client_cancel",
        request=request,
        instance=session,
        source="group_sessions",
        message="Cliente cancelou inscrição em turma.",
        before=_session_snapshot(session),
        metadata={
            "client_id": request.user.id,
            "session_ids": [item.id for item in target_sessions],
            "cancelled_count": updated,
        },
    )

    if updated > 1:
        messages.success(request, "Inscrição cancelada em todas as sessões futuras da turma.")
    else:
        messages.success(request, "Inscrição cancelada.")
    return redirect("my_group_sessions")


@login_required
def my_group_sessions_view(request):
    update_group_sessions_statuses()

    today = timezone.localdate()
    now_t = timezone.localtime().time()

    enrolments = (
        GroupEnrollment.objects
        .select_related("session", "session__service", "session__professional", "session__professional__user")
        .filter(client=request.user)
        .exclude(status=GroupEnrollment.STATUS_CANCELLED)
        .order_by("session__date", "session__time")
    )

    upcoming = []
    past = []

    month_filter = (request.GET.get("month") or "").strip()
    month_year = None
    month_value = None
    if month_filter:
        try:
            month_year = datetime.strptime(month_filter, "%Y-%m")
            month_value = month_year.strftime("%Y-%m")
        except Exception:
            month_year = None
    for e in enrolments:
        s = e.session
        is_future = (s.date > today) or (s.date == today and s.time and s.time > now_t)
        e.status_label = dict(GroupEnrollment.STATUS_CHOICES).get(e.status, e.status)
        e.can_cancel = is_future and can_cancel_group_enrollment(request.user, s)
        if is_future:
            if month_year:
                if s.date.year == month_year.year and s.date.month == month_year.month:
                    upcoming.append(e)
            else:
                upcoming.append(e)
        else:
            past.append(e)

    upcoming_page = Paginator(upcoming, 5).get_page(request.GET.get("page") or 1)
    past_page = Paginator(past, 6).get_page(request.GET.get("past_page") or 1)
    upcoming_months = (
        GroupSession.objects
        .filter(enrolments__client=request.user)
        .exclude(enrolments__status=GroupEnrollment.STATUS_CANCELLED)
        .dates("date", "month", order="ASC")
    )

    return render(
        request,
        "core/my_group_sessions.html",
        {
            "upcoming": upcoming_page,
            "past": past_page,
            "upcoming_months": upcoming_months,
            "selected_month": month_value,
        },
    )


def _schedule_family_ids(schedule):
    if not schedule:
        return []
    return list(
        GroupSchedule.objects.filter(
            service_id=schedule.service_id,
            professional_id=schedule.professional_id,
            name=schedule.name,
            time=schedule.time,
            start_date=schedule.start_date,
        ).values_list("id", flat=True)
    )


def _future_class_sessions_for_session(session, *, for_update=False):
    today = timezone.localdate()
    now_t = timezone.localtime().time()
    upcoming_q = Q(date__gt=today) | Q(date=today, time__gt=now_t)

    if session.schedule_id:
        schedule = getattr(session, "schedule", None) or GroupSchedule.objects.filter(id=session.schedule_id).first()
        schedule_ids = _schedule_family_ids(schedule) if schedule else [session.schedule_id]
        qs = GroupSession.objects.filter(
            schedule_id__in=schedule_ids,
            status=GroupSession.STATUS_SCHEDULED,
        )
    else:
        qs = GroupSession.objects.filter(
            id=session.id,
            status=GroupSession.STATUS_SCHEDULED,
        )
    qs = qs.filter(upcoming_q).order_by("date", "time")
    if for_update:
        qs = qs.select_for_update()
    return qs


def _future_class_sessions_for_schedule(schedule):
    today = timezone.localdate()
    now_t = timezone.localtime().time()
    upcoming_q = Q(date__gt=today) | Q(date=today, time__gt=now_t)
    schedule_ids = _schedule_family_ids(schedule) or [schedule.id]
    return (
        GroupSession.objects
        .filter(
            schedule_id__in=schedule_ids,
            status=GroupSession.STATUS_SCHEDULED,
        )
        .filter(upcoming_q)
        .order_by("date", "time")
    )


def group_sessions_admin_list_view(request):
    if not can_access_backoffice(request.user):
        return HttpResponseForbidden("Acesso apenas para backoffice.")

    update_group_sessions_statuses()
    ensure_group_sessions_for_schedules()

    prof = Professional.objects.filter(user=request.user).first()

    qs = GroupSchedule.objects.select_related(
        "service", "professional", "professional__user"
    ).order_by("name", "weekday", "time")

    if not can_view_all_calendar(request.user) and prof:
        qs = qs.filter(professional=prof)

    q = (request.GET.get("q") or "").strip()
    service_id = (request.GET.get("service_id") or "").strip()
    professional_id = (request.GET.get("professional_id") or "").strip()
    status = (request.GET.get("status") or "").strip()
    date_from = (request.GET.get("date_from") or "").strip()
    date_to = (request.GET.get("date_to") or "").strip()

    if service_id:
        qs = qs.filter(service_id=service_id)
    if professional_id:
        qs = qs.filter(professional_id=professional_id)
    if status:
        if status == "active":
            qs = qs.filter(is_active=True)
        elif status == "inactive":
            qs = qs.filter(is_active=False)
    if date_from:
        qs = qs.filter(start_date__gte=date_from)
    if date_to:
        qs = qs.filter(start_date__lte=date_to)
    if q:
        qs = apply_terms_filter(
            qs,
            q,
            [
                "name__icontains",
                "service__name__icontains",
                "professional__user__first_name__icontains",
                "professional__user__last_name__icontains",
                "professional__user__username__icontains",
            ],
        )

    weekday_map = dict(GroupSchedule.WEEKDAY_CHOICES)
    grouped = {}
    for schedule in qs:
        family_key = (
            schedule.service_id,
            schedule.professional_id,
            schedule.name,
            schedule.time,
            schedule.start_date,
        )
        item = grouped.get(family_key)
        if item is None:
            item = {
                "primary_schedule": schedule,
                "schedules": [],
                "weekdays": [],
            }
            grouped[family_key] = item
        item["schedules"].append(schedule)
        item["weekdays"].append(schedule.weekday)

    grouped_rows = []
    for item in grouped.values():
        primary = item["primary_schedule"]
        family_schedules = item["schedules"]
        unique_weekdays = sorted(set(item["weekdays"]))
        class_sessions_qs = _future_class_sessions_for_schedule(primary)
        next_session = class_sessions_qs.first()
        booked_count = (
            GroupEnrollment.objects
            .filter(
                session__in=class_sessions_qs,
                status__in=group_booked_statuses(),
            )
            .values("client_id")
            .distinct()
            .count()
        )
        waitlist_count = (
            GroupEnrollment.objects
            .filter(
                session__in=class_sessions_qs,
                status=GroupEnrollment.STATUS_WAITLIST,
            )
            .values("client_id")
            .distinct()
            .count()
        )
        active_count = sum(1 for schedule in family_schedules if schedule.is_active)
        is_active = active_count == len(family_schedules)
        is_inactive = active_count == 0
        grouped_rows.append(
            {
                "primary_schedule": primary,
                "primary_schedule_id": primary.id,
                "name": primary.name or primary.service.name,
                "service": primary.service,
                "professional": primary.professional,
                "time": primary.time,
                "start_date": primary.start_date,
                "next_session": next_session,
                "booked_count": booked_count,
                "waitlist_count": waitlist_count,
                "weekday_labels": [weekday_map.get(day, str(day)) for day in unique_weekdays],
                "repeat_count": len(unique_weekdays),
                "is_active": is_active,
                "is_inactive": is_inactive,
                "is_mixed": not is_active and not is_inactive,
            }
        )

    page = Paginator(grouped_rows, 15).get_page(request.GET.get("page") or 1)

    services = Service.objects.filter(service_type="group").order_by("name")
    professionals = Professional.objects.select_related("user").order_by("user__first_name", "user__last_name")
    if not can_view_all_calendar(request.user) and prof:
        professionals = professionals.filter(id=prof.id)

    params = request.GET.copy()
    if "page" in params:
        params.pop("page")
    pagination_qs = params.urlencode()

    return render(
        request,
        "core/group_schedules_admin_list.html",
        {
            "schedule_groups": page,
            "return_to": request.get_full_path(),
            "services": services,
            "professionals": professionals,
            "filters": {
                "q": q,
                "service_id": service_id,
                "professional_id": professional_id,
                "status": status,
                "date_from": date_from,
                "date_to": date_to,
            },
            "status_choices": (
                ("active", "Ativa"),
                ("inactive", "Inativa"),
            ),
            "pagination_qs": pagination_qs,
            "is_professional_view": False,
        },
    )


def group_schedule_edit_view(request, schedule_id):
    if not can_access_backoffice(request.user):
        return HttpResponseForbidden("Acesso apenas para backoffice.")

    schedule = get_object_or_404(
        GroupSchedule.objects.select_related("service", "professional", "professional__user"),
        id=schedule_id,
    )
    if not can_view_all_calendar(request.user):
        prof = Professional.objects.filter(user=request.user).first()
        if schedule.professional_id and prof and schedule.professional_id != prof.id:
            return HttpResponseForbidden("Sem acesso a esta turma.")

    services = Service.objects.filter(service_type="group").order_by("name")
    professionals = Professional.objects.select_related("user").prefetch_related("services").order_by("user__username")
    if not can_view_all_calendar(request.user):
        current_prof = Professional.objects.filter(user=request.user).first()
        if current_prof:
            professionals = professionals.filter(id=current_prof.id)
    business_weekday_choices = [choice for choice in GroupSchedule.WEEKDAY_CHOICES if choice[0] <= 4]
    allowed_weekdays = {value for value, _label in business_weekday_choices}
    valid_tabs = {"edit", "enrollments", "monthly"}
    requested_tab = (request.POST.get("tab") or request.GET.get("tab") or "edit").strip().lower()
    active_tab = requested_tab if requested_tab in valid_tabs else "edit"
    errors = []
    return_to = _safe_return_to(request, request.POST.get("return_to") or request.GET.get("return_to"))
    family_ids = _schedule_family_ids(schedule) or [schedule.id]
    family_before = _family_schedule_snapshots(family_ids)
    selected_weekdays = list(
        GroupSchedule.objects
        .filter(id__in=family_ids)
        .order_by("weekday")
        .values_list("weekday", flat=True)
    )
    month_raw = (request.POST.get("billing_month") or request.GET.get("billing_month") or "").strip()
    try:
        billing_month = datetime.strptime(month_raw, "%Y-%m").date().replace(day=1)
    except Exception:
        billing_month = timezone.localdate().replace(day=1)
    billing_month_value = billing_month.strftime("%Y-%m")
    family_key = group_schedule_family_key(schedule)
    if family_key:
        ensure_group_monthly_charges(
            start_date=billing_month,
            end_date=billing_month,
            family_keys=[family_key],
        )

    update_group_sessions_statuses()
    ensure_group_sessions_for_schedules(
        schedules=GroupSchedule.objects.filter(id__in=family_ids, is_active=True).select_related("service", "professional")
    )

    def _resolve_management_session():
        next_session = (
            GroupSession.objects
            .select_related("service", "professional", "professional__user", "schedule")
            .filter(schedule_id__in=family_ids, status=GroupSession.STATUS_SCHEDULED)
            .order_by("date", "time")
            .first()
        )
        if next_session:
            return next_session
        return (
            GroupSession.objects
            .select_related("service", "professional", "professional__user", "schedule")
            .filter(schedule_id__in=family_ids)
            .order_by("-date", "-time")
            .first()
        )

    management_session = _resolve_management_session()

    if request.method == "POST":
        action = (request.POST.get("action") or "save_edit").strip()
        if action == "save_edit":
            active_tab = "edit"
            name = (request.POST.get("name") or "").strip()
            service_id = (request.POST.get("service_id") or "").strip()
            professional_id = (request.POST.get("professional_id") or "").strip()
            weekday_list = request.POST.getlist("weekdays")
            start_date_str = (request.POST.get("start_date") or "").strip()
            time_str = (request.POST.get("time") or "").strip()
            capacity_str = (request.POST.get("capacity") or "").strip()
            duration_str = (request.POST.get("duration_minutes") or "").strip()
            notes = (request.POST.get("notes") or "").strip()
            is_active = (request.POST.get("is_active") or "").strip().lower() in {"1", "true", "on", "yes"}

            if not (name and service_id and professional_id and start_date_str and time_str and weekday_list):
                errors.append("Preenche nome, serviço, profissional, data de início, dias da semana e hora.")
            else:
                service = Service.objects.filter(id=service_id, service_type="group").first()
                if not service:
                    errors.append("Seleciona um serviço válido.")

                professional = Professional.objects.filter(id=professional_id).first()
                if not professional:
                    errors.append("Seleciona um profissional válido.")
                elif service and not professional.services.filter(id=service.id).exists():
                    errors.append("Profissional inválido para este serviço.")

                weekday_values = []
                for w in weekday_list:
                    try:
                        w_int = int(w)
                    except Exception:
                        errors.append("Dia da semana inválido.")
                        weekday_values = []
                        break
                    if w_int not in allowed_weekdays:
                        errors.append("Só são permitidos dias úteis (segunda a sexta).")
                        weekday_values = []
                        break
                    weekday_values.append(w_int)
                if weekday_values:
                    selected_weekdays = sorted(set(weekday_values))

                try:
                    start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
                    time_obj = datetime.strptime(time_str, "%H:%M").time()
                except Exception:
                    errors.append("Data ou hora inválidos.")
                    start_date = time_obj = None

                if not errors and professional and time_obj and selected_weekdays:
                    conflicts = []
                    for weekday in selected_weekdays:
                        conflict_exists = GroupSchedule.objects.filter(
                            professional=professional,
                            weekday=weekday,
                            time=time_obj,
                        ).exclude(id__in=family_ids).exists()
                        if conflict_exists:
                            conflicts.append(weekday)
                    if conflicts:
                        labels = [dict(GroupSchedule.WEEKDAY_CHOICES).get(w, str(w)) for w in conflicts]
                        errors.append(
                            "Já existe uma turma recorrente neste(s) dia(s)/hora para este profissional: "
                            + ", ".join(labels)
                            + "."
                        )

                if not errors:
                    capacity = int(capacity_str) if capacity_str.isdigit() else None
                    duration_minutes = int(duration_str) if duration_str.isdigit() else None

                    today = timezone.localdate()
                    now_t = timezone.localtime().time()
                    upcoming_q = Q(date__gt=today) | Q(date=today, time__gt=now_t)
                    locked_sessions_kept = False
                    with transaction.atomic():
                        family_schedules = list(
                            GroupSchedule.objects
                            .select_for_update()
                            .filter(id__in=family_ids)
                            .order_by("weekday", "id")
                        )
                        existing_by_weekday = {item.weekday: item for item in family_schedules}
                        desired_weekdays = set(selected_weekdays)

                        for weekday, schedule_item in existing_by_weekday.items():
                            if weekday in desired_weekdays:
                                continue
                            has_future_enrolments = GroupEnrollment.objects.filter(
                                session__schedule=schedule_item,
                                session__status=GroupSession.STATUS_SCHEDULED,
                                status__in=group_booked_statuses() + [GroupEnrollment.STATUS_WAITLIST],
                            ).filter(upcoming_q).exists()
                            if has_future_enrolments:
                                weekday_label = dict(GroupSchedule.WEEKDAY_CHOICES).get(weekday, str(weekday))
                                errors.append(
                                    f"Não é possível remover {weekday_label}: existem inscrições futuras nesse dia."
                                )

                        if not errors:
                            removed_weekdays = [w for w in existing_by_weekday if w not in desired_weekdays]
                            for weekday in removed_weekdays:
                                schedule_item = existing_by_weekday[weekday]
                                GroupSession.objects.filter(
                                    schedule=schedule_item,
                                    status=GroupSession.STATUS_SCHEDULED,
                                ).filter(upcoming_q).delete()
                                schedule_item.delete()

                            kept_schedules = list(
                                GroupSchedule.objects
                                .select_for_update()
                                .filter(id__in=family_ids, weekday__in=desired_weekdays)
                                .order_by("weekday", "id")
                            )
                            for schedule_item in kept_schedules:
                                structure_changed = any(
                                    [
                                        schedule_item.service_id != service.id,
                                        schedule_item.professional_id != professional.id,
                                        schedule_item.time != time_obj,
                                        schedule_item.start_date != start_date,
                                        schedule_item.is_active != is_active,
                                    ]
                                )

                                schedule_item.service = service
                                schedule_item.name = name
                                schedule_item.professional = professional
                                schedule_item.time = time_obj
                                schedule_item.start_date = start_date
                                schedule_item.capacity = capacity
                                schedule_item.duration_minutes = duration_minutes
                                schedule_item.notes = notes
                                schedule_item.is_active = is_active
                                schedule_item.save()

                                future_sessions = GroupSession.objects.filter(
                                    schedule=schedule_item,
                                    status=GroupSession.STATUS_SCHEDULED,
                                ).filter(upcoming_q)

                                if structure_changed:
                                    locked_ids = list(
                                        future_sessions
                                        .annotate(total_enrolments=Count("enrolments"))
                                        .filter(total_enrolments__gt=0)
                                        .values_list("id", flat=True)
                                    )
                                    if locked_ids:
                                        locked_sessions_kept = True
                                        GroupSession.objects.filter(id__in=locked_ids).update(
                                            name=name,
                                            capacity=capacity,
                                            duration_minutes=duration_minutes,
                                            notes=notes,
                                        )
                                    future_sessions.exclude(id__in=locked_ids).delete()
                                    if schedule_item.is_active:
                                        ensure_group_sessions_for_schedules(
                                            schedules=GroupSchedule.objects.filter(id=schedule_item.id).select_related(
                                                "service", "professional"
                                            )
                                        )
                                else:
                                    future_sessions.update(
                                        service=schedule_item.service,
                                        name=schedule_item.name,
                                        professional=schedule_item.professional,
                                        capacity=schedule_item.capacity,
                                        duration_minutes=schedule_item.duration_minutes,
                                        notes=schedule_item.notes,
                                    )

                            kept_weekdays = set(
                                GroupSchedule.objects
                                .filter(id__in=family_ids)
                                .values_list("weekday", flat=True)
                            )
                            weekdays_to_create = sorted(desired_weekdays - kept_weekdays)
                            created_ids = []
                            for weekday in weekdays_to_create:
                                created = GroupSchedule.objects.create(
                                    service=service,
                                    name=name,
                                    professional=professional,
                                    weekday=weekday,
                                    time=time_obj,
                                    start_date=start_date,
                                    capacity=capacity,
                                    duration_minutes=duration_minutes,
                                    notes=notes,
                                    is_active=is_active,
                                )
                                created_ids.append(created.id)

                            if is_active and created_ids:
                                ensure_group_sessions_for_schedules(
                                    schedules=GroupSchedule.objects.filter(id__in=created_ids).select_related(
                                        "service", "professional"
                                    )
                                )

                    if not errors:
                        family_after_ids = list(
                            GroupSchedule.objects.filter(
                                service=service,
                                professional=professional,
                                name=name,
                                time=time_obj,
                                start_date=start_date,
                            ).values_list("id", flat=True)
                        )
                        family_after = _family_schedule_snapshots(family_after_ids)
                        log_audit_event(
                            category="group_schedule",
                            action="update",
                            request=request,
                            instance=schedule,
                            source="group_schedule_edit",
                            message="Turma recorrente atualizada.",
                            before={"schedules": family_before},
                            after={"schedules": family_after},
                            metadata={
                                "family_schedule_ids": family_after_ids,
                                "selected_weekdays": selected_weekdays,
                                "locked_sessions_kept": locked_sessions_kept,
                            },
                        )
                        if locked_sessions_kept:
                            messages.info(
                                request,
                                "A turma foi atualizada. Mantivemos sessões futuras com inscrições para não perder reservas.",
                            )
                        messages.success(request, "Turma atualizada com sucesso.")
                        if return_to:
                            return redirect(return_to)
                        return redirect("group_sessions_admin_list")
        else:
            if action in {"mark_monthly_paid", "mark_monthly_unpaid"}:
                active_tab = "monthly"
                if not family_key:
                    messages.error(request, "Esta turma não pertence a uma recorrência válida.")
                else:
                    charge_id = (request.POST.get("charge_id") or "").strip()
                    if not charge_id.isdigit():
                        messages.error(request, "Mensalidade inválida.")
                    else:
                        charge = GroupMonthlyCharge.objects.filter(
                            id=int(charge_id),
                            family_key=family_key,
                            month=billing_month,
                        ).first()
                        if not charge:
                            messages.error(request, "Mensalidade não encontrada.")
                        elif action == "mark_monthly_paid":
                            before_charge = _monthly_charge_snapshot(charge)
                            charge.status = GroupMonthlyCharge.STATUS_PAID
                            charge.paid_at = timezone.now()
                            charge.save(update_fields=["status", "paid_at", "updated_at"])
                            log_audit_event(
                                category="group_monthly_charge",
                                action="mark_paid",
                                request=request,
                                instance=charge,
                                source="group_schedule_edit",
                                message="Mensalidade de turma marcada como paga.",
                                before=before_charge,
                                after=_monthly_charge_snapshot(charge),
                            )
                            messages.success(request, "Mensalidade marcada como paga.")
                        else:
                            before_charge = _monthly_charge_snapshot(charge)
                            charge.status = GroupMonthlyCharge.STATUS_UNPAID
                            charge.paid_at = None
                            charge.save(update_fields=["status", "paid_at", "updated_at"])
                            log_audit_event(
                                category="group_monthly_charge",
                                action="mark_unpaid",
                                request=request,
                                instance=charge,
                                source="group_schedule_edit",
                                message="Mensalidade de turma marcada como em dívida.",
                                before=before_charge,
                                after=_monthly_charge_snapshot(charge),
                            )
                            messages.success(request, "Mensalidade marcada como em dívida.")
            else:
                active_tab = "enrollments"
                if action == "cancel_schedule":
                    before_cancel = _family_schedule_snapshots(family_ids)
                    any_updated = False
                    for family_schedule in GroupSchedule.objects.filter(id__in=family_ids):
                        if family_schedule.is_active:
                            family_schedule.is_active = False
                            family_schedule.save(update_fields=["is_active", "updated_at"])
                            any_updated = True
                    today = timezone.localdate()
                    cancelled_sessions = list(
                        GroupSession.objects.filter(
                            schedule_id__in=family_ids,
                            date__gte=today,
                            status=GroupSession.STATUS_SCHEDULED,
                        ).values_list("id", flat=True)
                    )
                    GroupSession.objects.filter(
                        schedule_id__in=family_ids,
                        date__gte=today,
                        status=GroupSession.STATUS_SCHEDULED,
                    ).update(status=GroupSession.STATUS_CANCELLED)
                    if any_updated:
                        log_audit_event(
                            category="group_schedule",
                            action="cancel",
                            request=request,
                            instance=schedule,
                            source="group_schedule_edit",
                            message="Recorrência de turma cancelada.",
                            before={"schedules": before_cancel},
                            after={"schedules": _family_schedule_snapshots(family_ids)},
                            metadata={
                                "family_schedule_ids": family_ids,
                                "cancelled_session_ids": cancelled_sessions,
                            },
                        )
                    if any_updated:
                        messages.success(request, "Recorrência cancelada e sessões futuras anuladas.")
                    else:
                        messages.info(request, "A recorrência já estava inativa.")
                elif not management_session:
                    messages.error(request, "Não existe sessão associada para gerir inscritos.")
                elif action == "cancel_session":
                    management_session.status = GroupSession.STATUS_CANCELLED
                    management_session.save(update_fields=["status", "updated_at"])
                    GroupEnrollment.objects.filter(
                        session=management_session,
                        status__in=[GroupEnrollment.STATUS_BOOKED, GroupEnrollment.STATUS_WAITLIST],
                    ).update(status=GroupEnrollment.STATUS_CANCELLED)
                    messages.success(request, "Sessão cancelada.")
                elif action == "complete_session":
                    management_session.status = GroupSession.STATUS_COMPLETED
                    management_session.save(update_fields=["status", "updated_at"])
                    messages.success(request, "Sessão concluída.")
                elif action == "add_enrolment":
                    client_id = (request.POST.get("client_id") or "").strip()
                    client_profile_id = (request.POST.get("client_profile_id") or "").strip()
                    posted_weekdays = request.POST.getlist("enrolment_weekdays")
                    monthly_price_override_raw = (request.POST.get("monthly_price_override") or "").strip()
                    if not client_id and not client_profile_id:
                        messages.error(request, "Seleciona um cliente.")
                    else:
                        if client_profile_id:
                            client_profile = ClientProfile.objects.select_related("user").filter(id=client_profile_id).first()
                        else:
                            client = User.objects.filter(id=client_id).first()
                            client_profile = (
                                ClientProfile.objects.select_related("user").filter(user=client).first()
                                if client else None
                            )
                        if not client_profile:
                            messages.error(request, "Cliente inválido.")
                        else:
                            client = _ensure_group_client_user(client_profile)
                            membership_before = None
                            existing_membership = GroupMembership.objects.filter(
                                client=client,
                                family_key=family_key,
                            ).first()
                            if existing_membership:
                                membership_before = _membership_snapshot(existing_membership)
                            family_weekday_values = set(
                                GroupSchedule.objects
                                .filter(id__in=family_ids)
                                .values_list("weekday", flat=True)
                            )
                            selected_enrolment_weekdays = set()
                            for item in posted_weekdays:
                                try:
                                    weekday_value = int(item)
                                except Exception:
                                    continue
                                if weekday_value in family_weekday_values:
                                    selected_enrolment_weekdays.add(weekday_value)
                            if not selected_enrolment_weekdays:
                                selected_enrolment_weekdays = set(family_weekday_values)
                            if not selected_enrolment_weekdays:
                                messages.error(request, "Esta turma não tem dias de repetição válidos.")
                                query = {"tab": active_tab, "billing_month": billing_month_value}
                                if return_to:
                                    query["return_to"] = return_to
                                return redirect(f"{request.path}?{urlencode(query)}")

                            monthly_price_override = None
                            if monthly_price_override_raw:
                                normalized_override = monthly_price_override_raw.replace(",", ".")
                                try:
                                    monthly_price_override = Decimal(normalized_override).quantize(Decimal("0.01"))
                                except Exception:
                                    messages.error(request, "Preço mensal personalizado inválido.")
                                    query = {"tab": active_tab, "billing_month": billing_month_value}
                                    if return_to:
                                        query["return_to"] = return_to
                                    return redirect(f"{request.path}?{urlencode(query)}")
                                if monthly_price_override < 0:
                                    messages.error(request, "O preço mensal personalizado não pode ser negativo.")
                                    query = {"tab": active_tab, "billing_month": billing_month_value}
                                    if return_to:
                                        query["return_to"] = return_to
                                    return redirect(f"{request.path}?{urlencode(query)}")

                            with transaction.atomic():
                                management_session = GroupSession.objects.select_related("schedule").select_for_update().get(
                                    id=management_session.id
                                )
                                all_target_sessions = list(
                                    _future_class_sessions_for_session(management_session, for_update=True)
                                )
                                target_sessions = [
                                    session_item
                                    for session_item in all_target_sessions
                                    if session_item.date.weekday() in selected_enrolment_weekdays
                                ]
                                if not target_sessions:
                                    messages.error(request, "Sem sessões futuras para os dias selecionados.")
                                else:
                                    existing_qs = (
                                        GroupEnrollment.objects
                                        .select_for_update()
                                        .filter(session__in=all_target_sessions, client=client)
                                    )
                                    existing_map = {enrol.session_id: enrol for enrol in existing_qs}
                                    active_statuses = {
                                        GroupEnrollment.STATUS_BOOKED,
                                        GroupEnrollment.STATUS_WAITLIST,
                                    }
                                    missing_targets = [
                                        target
                                        for target in target_sessions
                                        if not (
                                            existing_map.get(target.id)
                                            and existing_map[target.id].status in active_statuses
                                        )
                                    ]
                                    new_status = None
                                    blocked_by_capacity = False
                                    if missing_targets:
                                        can_book_all = True
                                        for target in missing_targets:
                                            if target.spots_left <= 0:
                                                can_book_all = False
                                                break
                                        if can_book_all:
                                            new_status = GroupEnrollment.STATUS_BOOKED
                                        elif management_session.service.allow_waitlist:
                                            new_status = GroupEnrollment.STATUS_WAITLIST
                                        else:
                                            blocked_by_capacity = True

                                    if blocked_by_capacity:
                                        messages.error(request, "Turma cheia para os dias selecionados.")
                                    else:
                                        deselected_sessions = [
                                            item
                                            for item in all_target_sessions
                                            if item.date.weekday() not in selected_enrolment_weekdays
                                        ]
                                        cancelled_count = 0
                                        for target in deselected_sessions:
                                            existing = existing_map.get(target.id)
                                            if existing and existing.status in active_statuses:
                                                existing.status = GroupEnrollment.STATUS_CANCELLED
                                                existing.save(update_fields=["status", "updated_at"])
                                                cancelled_count += 1
                                                promote_group_waitlist(target)

                                        upsert_count = 0
                                        for target in target_sessions:
                                            existing = existing_map.get(target.id)
                                            if existing:
                                                if new_status and existing.status != new_status:
                                                    existing.status = new_status
                                                    existing.save(update_fields=["status", "updated_at"])
                                                    upsert_count += 1
                                            elif new_status:
                                                GroupEnrollment.objects.create(session=target, client=client, status=new_status)
                                                upsert_count += 1

                                        membership, membership_created = GroupMembership.objects.update_or_create(
                                            client=client,
                                            family_key=family_key,
                                            defaults={
                                                "service": schedule.service,
                                                "professional": schedule.professional,
                                                "schedule": schedule,
                                                "class_name": schedule.name or schedule.service.name,
                                                "weekdays": ",".join(str(day) for day in sorted(selected_enrolment_weekdays)),
                                                "monthly_price_override": monthly_price_override,
                                                "is_active": True,
                                            },
                                        )
                                        membership_after = _membership_snapshot(membership)
                                        log_audit_event(
                                            category="group_membership",
                                            action="create" if membership_created else "update",
                                            request=request,
                                            instance=membership,
                                            source="group_schedule_edit",
                                            message="Plano de turma do cliente atualizado.",
                                            before=membership_before or {},
                                            after=membership_after,
                                            metadata={
                                                "target_session_ids": [item.id for item in target_sessions],
                                                "all_session_ids": [item.id for item in all_target_sessions],
                                                "selected_weekdays": sorted(selected_enrolment_weekdays),
                                                "new_status": new_status or "",
                                                "upsert_count": upsert_count,
                                                "cancelled_count": cancelled_count,
                                            },
                                        )

                                        if cancelled_count and not upsert_count and not new_status:
                                            messages.success(request, "Plano do cliente atualizado.")
                                        elif new_status == GroupEnrollment.STATUS_WAITLIST:
                                            messages.info(request, "Cliente atualizado e colocado em lista de espera nos novos dias.")
                                        elif upsert_count or cancelled_count:
                                            messages.success(request, "Inscrição e plano mensal atualizados.")
                                        else:
                                            messages.info(request, "Plano mensal atualizado.")
                elif action in {"cancel_enrolment", "mark_attended", "mark_no_show", "promote_waitlist"}:
                    enrolment_id = (request.POST.get("enrolment_id") or "").strip()
                    enrolment = (
                        GroupEnrollment.objects
                        .select_related("session")
                        .filter(id=enrolment_id, session=management_session)
                        .first()
                    )
                    if not enrolment:
                        messages.error(request, "Inscrição inválida.")
                    else:
                        if action == "cancel_enrolment":
                            before_enrolment = _enrolment_snapshot(enrolment)
                            with transaction.atomic():
                                management_session = GroupSession.objects.select_related("schedule").select_for_update().get(
                                    id=management_session.id
                                )
                                target_sessions = list(_future_class_sessions_for_session(management_session, for_update=True))
                                target_enrolments = (
                                    GroupEnrollment.objects
                                    .select_for_update()
                                    .filter(
                                        session__in=target_sessions,
                                        client_id=enrolment.client_id,
                                        status__in=[GroupEnrollment.STATUS_BOOKED, GroupEnrollment.STATUS_WAITLIST],
                                    )
                                )
                                updated = 0
                                for item in target_enrolments:
                                    item.status = GroupEnrollment.STATUS_CANCELLED
                                    item.save(update_fields=["status", "updated_at"])
                                    updated += 1
                                for target in target_sessions:
                                    promote_group_waitlist(target)
                                GroupMembership.objects.filter(
                                    client_id=enrolment.client_id,
                                    family_key=family_key,
                                ).update(is_active=False, updated_at=timezone.now())
                            enrolment.refresh_from_db(fields=["status", "updated_at"])
                            membership = GroupMembership.objects.filter(
                                client_id=enrolment.client_id,
                                family_key=family_key,
                            ).first()
                            log_audit_event(
                                category="group_enrollment",
                                action="cancel",
                                request=request,
                                instance=enrolment,
                                source="group_schedule_edit",
                                message="Inscrição de turma cancelada no backoffice.",
                                before=before_enrolment,
                                after=_enrolment_snapshot(enrolment),
                                metadata={
                                    "session_ids": [item.id for item in target_sessions],
                                    "updated_count": updated,
                                    "membership_after": _membership_snapshot(membership) if membership else {},
                                },
                            )
                            if updated > 1:
                                messages.success(request, "Inscrição cancelada em todas as sessões futuras da turma.")
                            else:
                                messages.success(request, "Inscrição cancelada.")
                        elif action == "mark_attended":
                            before_enrolment = _enrolment_snapshot(enrolment)
                            enrolment.status = GroupEnrollment.STATUS_ATTENDED
                            enrolment.save(update_fields=["status", "updated_at"])
                            log_audit_event(
                                category="group_enrollment",
                                action="mark_attended",
                                request=request,
                                instance=enrolment,
                                source="group_schedule_edit",
                                message="Presença marcada na gestão de turma.",
                                before=before_enrolment,
                                after=_enrolment_snapshot(enrolment),
                                metadata={"session_id": management_session.id},
                            )
                            messages.success(request, "Presença marcada.")
                        elif action == "mark_no_show":
                            before_enrolment = _enrolment_snapshot(enrolment)
                            enrolment.status = GroupEnrollment.STATUS_NO_SHOW
                            enrolment.save(update_fields=["status", "updated_at"])
                            log_audit_event(
                                category="group_enrollment",
                                action="mark_no_show",
                                request=request,
                                instance=enrolment,
                                source="group_schedule_edit",
                                message="Falta marcada na gestão de turma.",
                                before=before_enrolment,
                                after=_enrolment_snapshot(enrolment),
                                metadata={"session_id": management_session.id},
                            )
                            messages.success(request, "Falta marcada.")
                        elif action == "promote_waitlist":
                            if enrolment.status == GroupEnrollment.STATUS_WAITLIST and management_session.spots_left > 0:
                                before_enrolment = _enrolment_snapshot(enrolment)
                                enrolment.status = GroupEnrollment.STATUS_BOOKED
                                enrolment.save(update_fields=["status", "updated_at"])
                                log_audit_event(
                                    category="group_enrollment",
                                    action="promote_waitlist",
                                    request=request,
                                    instance=enrolment,
                                    source="group_schedule_edit",
                                    message="Inscrição promovida da lista de espera.",
                                    before=before_enrolment,
                                    after=_enrolment_snapshot(enrolment),
                                    metadata={"session_id": management_session.id},
                                )
                                messages.success(request, "Inscrição promovida.")
                            else:
                                messages.error(request, "Não é possível promover.")
                else:
                    messages.error(request, "Ação inválida.")

            query = {"tab": active_tab, "billing_month": billing_month_value}
            if return_to:
                query["return_to"] = return_to
            return redirect(f"{request.path}?{urlencode(query)}")

    family_ids = _schedule_family_ids(schedule) or [schedule.id]
    family_weekdays = (
        GroupSchedule.objects
        .filter(id__in=family_ids)
        .order_by("weekday")
        .values_list("weekday", flat=True)
    )
    if request.method != "POST":
        selected_weekdays = list(family_weekdays)
    weekday_map = dict(GroupSchedule.WEEKDAY_CHOICES)
    family_weekday_labels = [weekday_map.get(day, str(day)) for day in family_weekdays]
    family_weekday_choices = [(day, weekday_map.get(day, str(day))) for day in family_weekdays]
    management_session = _resolve_management_session()
    family_next_session = management_session

    booked_enrolments = GroupEnrollment.objects.none()
    waitlist_enrolments = GroupEnrollment.objects.none()
    if management_session:
        booked_enrolments = (
            GroupEnrollment.objects
            .select_related("client", "client__client_profile")
            .filter(session=management_session, status__in=group_booked_statuses())
            .order_by("created_at")
        )
        waitlist_enrolments = (
            GroupEnrollment.objects
            .select_related("client", "client__client_profile")
            .filter(session=management_session, status=GroupEnrollment.STATUS_WAITLIST)
            .order_by("created_at")
        )
        membership_map = {
            item.client_id: item
            for item in GroupMembership.objects.filter(
                family_key=family_key,
                client_id__in=[enrol.client_id for enrol in booked_enrolments] + [enrol.client_id for enrol in waitlist_enrolments],
                is_active=True,
            )
        }
        for enrol in booked_enrolments:
            membership = membership_map.get(enrol.client_id)
            if not membership:
                enrol.membership_days_label = "Todos os dias da turma"
                enrol.membership_monthly_price_override = None
                continue
            weekday_values = membership.weekday_values()
            if weekday_values:
                enrol.membership_days_label = ", ".join(weekday_map.get(day, str(day)) for day in weekday_values)
            else:
                enrol.membership_days_label = "Todos os dias da turma"
            enrol.membership_monthly_price_override = membership.monthly_price_override

    monthly_charges = GroupMonthlyCharge.objects.none()
    monthly_totals = {
        "total": Decimal("0.00"),
        "paid": Decimal("0.00"),
        "unpaid": Decimal("0.00"),
        "paid_count": 0,
        "unpaid_count": 0,
    }
    if family_key:
        ensure_group_monthly_charges(
            start_date=billing_month,
            end_date=billing_month,
            family_keys=[family_key],
        )
        monthly_charges = (
            GroupMonthlyCharge.objects
            .select_related("client", "client__client_profile")
            .filter(
                family_key=family_key,
                month=billing_month,
            )
            .order_by("client__client_profile__full_name", "client__username")
        )
        monthly_totals = {
            "total": monthly_charges.aggregate(total=Coalesce(Sum("final_price"), Decimal("0.00"))).get("total") or Decimal("0.00"),
            "paid": monthly_charges.filter(status=GroupMonthlyCharge.STATUS_PAID).aggregate(total=Coalesce(Sum("final_price"), Decimal("0.00"))).get("total") or Decimal("0.00"),
            "unpaid": monthly_charges.filter(status=GroupMonthlyCharge.STATUS_UNPAID).aggregate(total=Coalesce(Sum("final_price"), Decimal("0.00"))).get("total") or Decimal("0.00"),
            "paid_count": monthly_charges.filter(status=GroupMonthlyCharge.STATUS_PAID).count(),
            "unpaid_count": monthly_charges.filter(status=GroupMonthlyCharge.STATUS_UNPAID).count(),
        }

    base_query = {"return_to": return_to, "billing_month": billing_month_value}
    tab_urls = {}
    for tab in ("edit", "enrollments", "monthly"):
        tab_query = {k: v for k, v in base_query.items() if v}
        tab_query["tab"] = tab
        tab_urls[tab] = f"{request.path}?{urlencode(tab_query)}"

    return render(
        request,
        "core/group_schedule_edit.html",
        {
            "schedule": schedule,
            "services": services,
            "professionals": professionals,
            "errors": errors,
            "return_to": return_to,
            "weekday_choices": business_weekday_choices,
            "selected_weekdays": selected_weekdays,
            "family_weekday_labels": family_weekday_labels,
            "family_weekday_choices": family_weekday_choices,
            "family_repeat_count": len(family_weekday_labels),
            "family_next_session": family_next_session,
            "active_tab": active_tab,
            "tab_urls": tab_urls,
            "session": management_session,
            "booked_enrolments": booked_enrolments,
            "waitlist_enrolments": waitlist_enrolments,
            "billing_month": billing_month_value,
            "monthly_charges": monthly_charges,
            "monthly_totals": monthly_totals,
            "can_manage_session": True,
        },
    )


def group_session_create_view(request):
    if not can_access_backoffice(request.user):
        return HttpResponseForbidden("Acesso apenas para backoffice.")

    services = Service.objects.filter(service_type="group").order_by("name")
    professionals = Professional.objects.select_related("user").prefetch_related("services").order_by("user__username")
    weekday_choices = [choice for choice in GroupSchedule.WEEKDAY_CHOICES if choice[0] <= 4]
    allowed_weekdays = {value for value, _label in weekday_choices}
    errors = []
    selected_weekdays = []
    return_to = _safe_return_to(request, request.POST.get("return_to") or request.GET.get("return_to"))

    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        service_id = (request.POST.get("service_id") or "").strip()
        professional_id = (request.POST.get("professional_id") or "").strip()
        start_date_str = (request.POST.get("start_date") or "").strip()
        weekday_list = request.POST.getlist("weekdays")
        for w in weekday_list:
            if str(w).isdigit():
                w_int = int(w)
                if w_int in allowed_weekdays and w_int not in selected_weekdays:
                    selected_weekdays.append(w_int)
        time_str = (request.POST.get("time") or "").strip()
        capacity_str = (request.POST.get("capacity") or "").strip()
        duration_str = (request.POST.get("duration_minutes") or "").strip()
        notes = (request.POST.get("notes") or "").strip()

        if not (name and service_id and professional_id and start_date_str and time_str and weekday_list):
            errors.append("Preenche nome, serviço, profissional, data de início, dias da semana e hora.")
        else:
            service = get_object_or_404(Service, id=service_id, service_type="group")
            professional = Professional.objects.filter(id=professional_id).first()
            if not professional:
                errors.append("Seleciona um profissional válido.")
            elif not professional.services.filter(id=service.id).exists():
                errors.append("Profissional inválido para este serviço.")

            try:
                start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
                time_obj = datetime.strptime(time_str, "%H:%M").time()
            except Exception:
                errors.append("Data ou hora inválidos.")
                start_date = time_obj = None
                weekday_values = []
            else:
                weekday_values = []
                for w in weekday_list:
                    try:
                        w_int = int(w)
                        if w_int not in allowed_weekdays:
                            raise ValueError()
                        weekday_values.append(w_int)
                    except Exception:
                        errors.append("Dia da semana inválido. Só são permitidos dias úteis (segunda a sexta).")
                        break
                if weekday_values:
                    weekday_values = sorted(set(weekday_values))
                    selected_weekdays = list(weekday_values)

            if not errors and start_date:
                today = timezone.localdate()
                now_t = timezone.localtime().time()
                if start_date < today or (start_date == today and time_obj <= now_t):
                    errors.append("Não podes criar sessões no passado.")

            if not errors and professional:
                conflicts = []
                for w in weekday_values:
                    if GroupSchedule.objects.filter(
                        professional=professional,
                        weekday=w,
                        time=time_obj,
                    ).exists():
                        conflicts.append(w)
                if conflicts:
                    labels = [dict(GroupSchedule.WEEKDAY_CHOICES).get(w, str(w)) for w in conflicts]
                    errors.append(
                        "Já existe uma turma recorrente neste(s) dia(s)/hora para este profissional: "
                        + ", ".join(labels)
                        + "."
                    )

            if not errors:
                capacity = int(capacity_str) if capacity_str.isdigit() else None
                duration_minutes = int(duration_str) if duration_str.isdigit() else None
                created_ids = []
                for w in weekday_values:
                    schedule = GroupSchedule.objects.create(
                        service=service,
                        name=name,
                        professional=professional,
                        weekday=w,
                        time=time_obj,
                        start_date=start_date,
                        capacity=capacity,
                        duration_minutes=duration_minutes,
                        notes=notes,
                    )
                    created_ids.append(schedule.id)
                ensure_group_sessions_for_schedules(
                    schedules=GroupSchedule.objects.filter(id__in=created_ids)
                )
                created_schedules_after = _family_schedule_snapshots(created_ids)
                messages.success(request, "Turma recorrente criada.")
                next_session = (
                    GroupSession.objects.filter(schedule_id__in=created_ids)
                    .order_by("date", "time")
                    .first()
                )
                log_audit_event(
                    category="group_schedule",
                    action="create",
                    request=request,
                    instance=next_session or GroupSchedule.objects.filter(id__in=created_ids).order_by("id").first(),
                    source="group_session_create",
                    message="Turma recorrente criada.",
                    after={"schedules": created_schedules_after},
                    metadata={
                        "schedule_ids": created_ids,
                        "weekday_values": weekday_values,
                        "next_session_id": next_session.id if next_session else None,
                    },
                )
                if next_session:
                    redirect_url = reverse("group_session_detail_admin", kwargs={"session_id": next_session.id})
                else:
                    redirect_url = reverse("group_sessions_admin_list")
                if return_to:
                    return redirect(f"{redirect_url}?return_to={return_to}")
                return redirect(redirect_url)

    return render(
        request,
        "core/group_session_create.html",
        {
            "services": services,
            "professionals": professionals,
            "weekday_choices": weekday_choices,
            "selected_weekdays": selected_weekdays,
            "errors": errors,
            "return_to": return_to,
        },
    )


@professional_required
def professional_group_sessions_view(request):
    prof = _get_professional_or_403(request.user)
    if prof is None:
        return HttpResponseForbidden("Acesso apenas para profissionais.")

    update_group_sessions_statuses()
    ensure_group_sessions_for_schedules(
        schedules=GroupSchedule.objects.filter(professional=prof, is_active=True)
        .select_related("service", "professional")
    )

    qs = (
        GroupSession.objects
        .select_related("service", "professional", "professional__user")
        .filter(professional=prof)
        .order_by("date", "time")
    )

    q = (request.GET.get("q") or "").strip()
    service_id = (request.GET.get("service_id") or "").strip()
    status = (request.GET.get("status") or "").strip()
    date_from = (request.GET.get("date_from") or "").strip()
    date_to = (request.GET.get("date_to") or "").strip()

    if service_id:
        qs = qs.filter(service_id=service_id)
    if status:
        qs = qs.filter(status=status)
    if date_from:
        qs = qs.filter(date__gte=date_from)
    if date_to:
        qs = qs.filter(date__lte=date_to)
    if q:
        qs = apply_terms_filter(qs, q, ["service__name__icontains"])

    qs = qs.annotate(
        booked_count=Count("enrolments", filter=Q(enrolments__status__in=group_booked_statuses())),
        waitlist_count=Count("enrolments", filter=Q(enrolments__status=GroupEnrollment.STATUS_WAITLIST)),
    )

    page = Paginator(qs, 15).get_page(request.GET.get("page") or 1)
    params = request.GET.copy()
    if "page" in params:
        params.pop("page")
    pagination_qs = params.urlencode()

    return render(
        request,
        "core/group_sessions_admin_list.html",
        {
            "sessions": page,
            "return_to": request.get_full_path(),
            "services": Service.objects.filter(service_type="group").order_by("name"),
            "professionals": [prof],
            "filters": {
                "q": q,
                "service_id": service_id,
                "professional_id": str(prof.id),
                "status": status,
                "date_from": date_from,
                "date_to": date_to,
            },
            "status_choices": GroupSession.STATUS_CHOICES,
            "pagination_qs": pagination_qs,
            "is_professional_view": True,
        },
    )


@professional_required
def professional_group_session_detail_view(request, session_id):
    prof = _get_professional_or_403(request.user)
    if prof is None:
        return HttpResponseForbidden("Acesso apenas para profissionais.")

    session = get_object_or_404(
        GroupSession.objects.select_related("service", "professional", "professional__user"),
        id=session_id,
        professional=prof,
    )

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        if action in ("mark_attended", "mark_no_show"):
            enrolment_id = (request.POST.get("enrolment_id") or "").strip()
            enrolment = (
                GroupEnrollment.objects
                .select_related("session")
                .filter(id=enrolment_id, session=session)
                .first()
            )
            if not enrolment:
                messages.error(request, "Inscrição inválida.")
            elif action == "mark_attended":
                before = _enrolment_snapshot(enrolment)
                enrolment.status = GroupEnrollment.STATUS_ATTENDED
                enrolment.save(update_fields=["status", "updated_at"])
                log_audit_event(
                    category="group_enrollment",
                    action="mark_attended",
                    request=request,
                    instance=enrolment,
                    source="group_session_professional",
                    message="Presença marcada pelo profissional.",
                    before=before,
                    after=_enrolment_snapshot(enrolment),
                    metadata={"session_id": session.id},
                )
                messages.success(request, "Presença marcada.")
            elif action == "mark_no_show":
                before = _enrolment_snapshot(enrolment)
                enrolment.status = GroupEnrollment.STATUS_NO_SHOW
                enrolment.save(update_fields=["status", "updated_at"])
                log_audit_event(
                    category="group_enrollment",
                    action="mark_no_show",
                    request=request,
                    instance=enrolment,
                    source="group_session_professional",
                    message="Falta marcada pelo profissional.",
                    before=before,
                    after=_enrolment_snapshot(enrolment),
                    metadata={"session_id": session.id},
                )
                messages.success(request, "Falta marcada.")

    booked_enrolments = (
        GroupEnrollment.objects
        .select_related("client", "client__client_profile")
        .filter(session=session, status__in=group_booked_statuses())
        .order_by("created_at")
    )
    waitlist_enrolments = (
        GroupEnrollment.objects
        .select_related("client", "client__client_profile")
        .filter(session=session, status=GroupEnrollment.STATUS_WAITLIST)
        .order_by("created_at")
    )

    return render(
        request,
        "core/group_session_detail_admin.html",
        {
            "session": session,
            "booked_enrolments": booked_enrolments,
            "waitlist_enrolments": waitlist_enrolments,
            "return_to": reverse("professional_group_sessions"),
            "is_professional_view": True,
            "can_manage_session": False,
        },
    )


def group_session_detail_admin_view(request, session_id):
    if not can_access_backoffice(request.user):
        return HttpResponseForbidden("Acesso apenas para backoffice.")

    session = get_object_or_404(
        GroupSession.objects.select_related("service", "professional", "professional__user", "schedule"),
        id=session_id,
    )
    if not can_view_all_calendar(request.user):
        prof = Professional.objects.filter(user=request.user).first()
        if session.professional_id and prof and session.professional_id != prof.id:
            return HttpResponseForbidden("Sem acesso a esta sessão.")

    month_raw = (request.POST.get("billing_month") or request.GET.get("billing_month") or "").strip()
    try:
        billing_month = datetime.strptime(month_raw, "%Y-%m").date().replace(day=1)
    except Exception:
        billing_month = timezone.localdate().replace(day=1)
    billing_month_value = billing_month.strftime("%Y-%m")

    family_key = group_schedule_family_key(session.schedule) if session.schedule_id else ""
    if family_key:
        ensure_group_monthly_charges(
            start_date=billing_month,
            end_date=billing_month,
            family_keys=[family_key],
        )

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        if action == "cancel_session":
            before_session = _session_snapshot(session)
            session.status = GroupSession.STATUS_CANCELLED
            session.save(update_fields=["status", "updated_at"])
            GroupEnrollment.objects.filter(
                session=session,
                status__in=[GroupEnrollment.STATUS_BOOKED, GroupEnrollment.STATUS_WAITLIST],
            ).update(status=GroupEnrollment.STATUS_CANCELLED)
            log_audit_event(
                category="group_session",
                action="cancel",
                request=request,
                instance=session,
                source="group_session_admin",
                message="Sessão de turma cancelada.",
                before=before_session,
                after=_session_snapshot(session),
            )
            messages.success(request, "Sessão cancelada.")
        elif action == "complete_session":
            before_session = _session_snapshot(session)
            session.status = GroupSession.STATUS_COMPLETED
            session.save(update_fields=["status", "updated_at"])
            log_audit_event(
                category="group_session",
                action="complete",
                request=request,
                instance=session,
                source="group_session_admin",
                message="Sessão de turma concluída.",
                before=before_session,
                after=_session_snapshot(session),
            )
            messages.success(request, "Sessão concluída.")
        elif action == "cancel_schedule":
            if not session.schedule_id:
                messages.error(request, "Sem recorrência associada.")
            else:
                schedule = session.schedule
                before_schedule = _schedule_snapshot(schedule)
                schedule.is_active = False
                schedule.save(update_fields=["is_active", "updated_at"])
                today = timezone.localdate()
                cancelled_session_ids = list(
                    GroupSession.objects.filter(
                        schedule=schedule,
                        date__gte=today,
                        status=GroupSession.STATUS_SCHEDULED,
                    ).values_list("id", flat=True)
                )
                GroupSession.objects.filter(
                    schedule=schedule,
                    date__gte=today,
                    status=GroupSession.STATUS_SCHEDULED,
                ).update(status=GroupSession.STATUS_CANCELLED)
                log_audit_event(
                    category="group_schedule",
                    action="cancel",
                    request=request,
                    instance=schedule,
                    source="group_session_admin",
                    message="Recorrência da turma cancelada.",
                    before=before_schedule,
                    after=_schedule_snapshot(schedule),
                    metadata={"cancelled_session_ids": cancelled_session_ids},
                )
                messages.success(request, "Recorrência cancelada e sessões futuras anuladas.")
        elif action == "add_enrolment":
            client_id = (request.POST.get("client_id") or "").strip()
            client_profile_id = (request.POST.get("client_profile_id") or "").strip()
            if not client_id and not client_profile_id:
                messages.error(request, "Seleciona um cliente.")
            else:
                if client_profile_id:
                    client_profile = ClientProfile.objects.select_related("user").filter(id=client_profile_id).first()
                else:
                    client = User.objects.filter(id=client_id).first()
                    client_profile = (
                        ClientProfile.objects.select_related("user").filter(user=client).first()
                        if client else None
                    )
                if not client_profile:
                    messages.error(request, "Cliente inválido.")
                else:
                    client = _ensure_group_client_user(client_profile)
                    with transaction.atomic():
                        session = GroupSession.objects.select_related("schedule").select_for_update().get(id=session.id)
                        target_sessions = list(_future_class_sessions_for_session(session, for_update=True))
                        existing_qs = (
                            GroupEnrollment.objects
                            .select_for_update()
                            .filter(session__in=target_sessions, client=client)
                        )
                        existing_map = {enrol.session_id: enrol for enrol in existing_qs}
                        active_statuses = {
                            GroupEnrollment.STATUS_BOOKED,
                            GroupEnrollment.STATUS_WAITLIST,
                        }
                        if target_sessions and all(
                            existing_map.get(s.id) and existing_map[s.id].status in active_statuses
                            for s in target_sessions
                        ):
                            messages.info(request, "Este cliente já está inscrito.")
                        else:
                            can_book_all = True
                            for target in target_sessions:
                                existing = existing_map.get(target.id)
                                if existing and existing.status in active_statuses:
                                    continue
                                if target.spots_left <= 0:
                                    can_book_all = False
                                    break

                            if can_book_all:
                                new_status = GroupEnrollment.STATUS_BOOKED
                            elif session.service.allow_waitlist:
                                new_status = GroupEnrollment.STATUS_WAITLIST
                            else:
                                messages.error(request, "Turma cheia.")
                                new_status = None

                            if new_status:
                                changed_session_ids = []
                                for target in target_sessions:
                                    existing = existing_map.get(target.id)
                                    if existing:
                                        if existing.status != new_status:
                                            existing.status = new_status
                                            existing.save(update_fields=["status", "updated_at"])
                                            changed_session_ids.append(target.id)
                                    else:
                                        GroupEnrollment.objects.create(session=target, client=client, status=new_status)
                                        changed_session_ids.append(target.id)
                                log_audit_event(
                                    category="group_enrollment",
                                    action="add",
                                    request=request,
                                    instance=session,
                                    source="group_session_admin",
                                    message="Cliente inscrito em turma pelo backoffice.",
                                    after=_session_snapshot(session),
                                    metadata={
                                        "client_id": client.id,
                                        "new_status": new_status,
                                        "target_session_ids": [item.id for item in target_sessions],
                                        "changed_session_ids": changed_session_ids,
                                    },
                                )
                                if new_status == GroupEnrollment.STATUS_WAITLIST:
                                    messages.info(request, "Cliente colocado na lista de espera da turma.")
                                elif len(target_sessions) > 1:
                                    messages.success(request, "Cliente inscrito em todas as sessões futuras da turma.")
                                else:
                                    messages.success(request, "Cliente inscrito com sucesso.")
        elif action in ("cancel_enrolment", "mark_attended", "mark_no_show", "promote_waitlist"):
            enrolment_id = (request.POST.get("enrolment_id") or "").strip()
            enrolment = (
                GroupEnrollment.objects
                .select_related("session")
                .filter(id=enrolment_id, session=session)
                .first()
            )
            if not enrolment:
                messages.error(request, "Inscrição inválida.")
            else:
                if action == "cancel_enrolment":
                    before_enrolment = _enrolment_snapshot(enrolment)
                    with transaction.atomic():
                        session = GroupSession.objects.select_related("schedule").select_for_update().get(id=session.id)
                        target_sessions = list(_future_class_sessions_for_session(session, for_update=True))
                        target_enrolments = (
                            GroupEnrollment.objects
                            .select_for_update()
                            .filter(
                                session__in=target_sessions,
                                client_id=enrolment.client_id,
                                status__in=[GroupEnrollment.STATUS_BOOKED, GroupEnrollment.STATUS_WAITLIST],
                            )
                        )
                        updated = 0
                        for item in target_enrolments:
                            item.status = GroupEnrollment.STATUS_CANCELLED
                            item.save(update_fields=["status", "updated_at"])
                            updated += 1
                        for target in target_sessions:
                            promote_group_waitlist(target)
                    enrolment.refresh_from_db(fields=["status", "updated_at"])
                    log_audit_event(
                        category="group_enrollment",
                        action="cancel",
                        request=request,
                        instance=enrolment,
                        source="group_session_admin",
                        message="Inscrição cancelada na sessão de turma.",
                        before=before_enrolment,
                        after=_enrolment_snapshot(enrolment),
                        metadata={
                            "session_ids": [item.id for item in target_sessions],
                            "updated_count": updated,
                        },
                    )
                    if updated > 1:
                        messages.success(request, "Inscrição cancelada em todas as sessões futuras da turma.")
                    else:
                        messages.success(request, "Inscrição cancelada.")
                elif action == "mark_attended":
                    before_enrolment = _enrolment_snapshot(enrolment)
                    enrolment.status = GroupEnrollment.STATUS_ATTENDED
                    enrolment.save(update_fields=["status", "updated_at"])
                    log_audit_event(
                        category="group_enrollment",
                        action="mark_attended",
                        request=request,
                        instance=enrolment,
                        source="group_session_admin",
                        message="Presença marcada na sessão de turma.",
                        before=before_enrolment,
                        after=_enrolment_snapshot(enrolment),
                    )
                    messages.success(request, "Presença marcada.")
                elif action == "mark_no_show":
                    before_enrolment = _enrolment_snapshot(enrolment)
                    enrolment.status = GroupEnrollment.STATUS_NO_SHOW
                    enrolment.save(update_fields=["status", "updated_at"])
                    log_audit_event(
                        category="group_enrollment",
                        action="mark_no_show",
                        request=request,
                        instance=enrolment,
                        source="group_session_admin",
                        message="Falta marcada na sessão de turma.",
                        before=before_enrolment,
                        after=_enrolment_snapshot(enrolment),
                    )
                    messages.success(request, "Falta marcada.")
                elif action == "promote_waitlist":
                    if enrolment.status == GroupEnrollment.STATUS_WAITLIST and session.spots_left > 0:
                        before_enrolment = _enrolment_snapshot(enrolment)
                        enrolment.status = GroupEnrollment.STATUS_BOOKED
                        enrolment.save(update_fields=["status", "updated_at"])
                        log_audit_event(
                            category="group_enrollment",
                            action="promote_waitlist",
                            request=request,
                            instance=enrolment,
                            source="group_session_admin",
                            message="Inscrição promovida da lista de espera.",
                            before=before_enrolment,
                            after=_enrolment_snapshot(enrolment),
                        )
                        messages.success(request, "Inscrição promovida.")
                    else:
                        messages.error(request, "Não é possível promover.")
        elif action in ("mark_monthly_paid", "mark_monthly_unpaid"):
            if not family_key:
                messages.error(request, "Esta sessão não pertence a uma turma recorrente.")
            else:
                charge_id = (request.POST.get("charge_id") or "").strip()
                if not charge_id.isdigit():
                    messages.error(request, "Mensalidade inválida.")
                else:
                    charge = GroupMonthlyCharge.objects.filter(
                        id=int(charge_id),
                        family_key=family_key,
                        month=billing_month,
                    ).first()
                    if not charge:
                        messages.error(request, "Mensalidade não encontrada.")
                    elif action == "mark_monthly_paid":
                        before_charge = _monthly_charge_snapshot(charge)
                        charge.status = GroupMonthlyCharge.STATUS_PAID
                        charge.paid_at = timezone.now()
                        charge.save(update_fields=["status", "paid_at", "updated_at"])
                        log_audit_event(
                            category="group_monthly_charge",
                            action="mark_paid",
                            request=request,
                            instance=charge,
                            source="group_session_admin",
                            message="Mensalidade de turma marcada como paga.",
                            before=before_charge,
                            after=_monthly_charge_snapshot(charge),
                        )
                        messages.success(request, "Mensalidade marcada como paga.")
                    else:
                        before_charge = _monthly_charge_snapshot(charge)
                        charge.status = GroupMonthlyCharge.STATUS_UNPAID
                        charge.paid_at = None
                        charge.save(update_fields=["status", "paid_at", "updated_at"])
                        log_audit_event(
                            category="group_monthly_charge",
                            action="mark_unpaid",
                            request=request,
                            instance=charge,
                            source="group_session_admin",
                            message="Mensalidade de turma marcada como em dívida.",
                            before=before_charge,
                            after=_monthly_charge_snapshot(charge),
                        )
                        messages.success(request, "Mensalidade marcada como em dívida.")

            query = {"billing_month": billing_month_value}
            return_to_post = _safe_return_to(request, request.POST.get("return_to"))
            if return_to_post:
                query["return_to"] = return_to_post
            return redirect(f"{request.path}?{urlencode(query)}")

    booked_enrolments = (
        GroupEnrollment.objects
        .select_related("client", "client__client_profile")
        .filter(session=session, status__in=group_booked_statuses())
        .order_by("created_at")
    )
    waitlist_enrolments = (
        GroupEnrollment.objects
        .select_related("client", "client__client_profile")
        .filter(session=session, status=GroupEnrollment.STATUS_WAITLIST)
        .order_by("created_at")
    )

    return_to = _safe_return_to(request, request.GET.get("return_to"))
    monthly_charges = GroupMonthlyCharge.objects.none()
    monthly_totals = {
        "total": Decimal("0.00"),
        "paid": Decimal("0.00"),
        "unpaid": Decimal("0.00"),
        "paid_count": 0,
        "unpaid_count": 0,
    }
    if family_key:
        ensure_group_monthly_charges(
            start_date=billing_month,
            end_date=billing_month,
            family_keys=[family_key],
        )
        monthly_charges = (
            GroupMonthlyCharge.objects
            .select_related("client", "client__client_profile")
            .filter(
                family_key=family_key,
                month=billing_month,
            )
            .order_by("client__client_profile__full_name", "client__username")
        )
        monthly_totals = {
            "total": monthly_charges.aggregate(total=Coalesce(Sum("final_price"), Decimal("0.00"))).get("total") or Decimal("0.00"),
            "paid": monthly_charges.filter(status=GroupMonthlyCharge.STATUS_PAID).aggregate(total=Coalesce(Sum("final_price"), Decimal("0.00"))).get("total") or Decimal("0.00"),
            "unpaid": monthly_charges.filter(status=GroupMonthlyCharge.STATUS_UNPAID).aggregate(total=Coalesce(Sum("final_price"), Decimal("0.00"))).get("total") or Decimal("0.00"),
            "paid_count": monthly_charges.filter(status=GroupMonthlyCharge.STATUS_PAID).count(),
            "unpaid_count": monthly_charges.filter(status=GroupMonthlyCharge.STATUS_UNPAID).count(),
        }

    return render(
        request,
        "core/group_session_detail_admin.html",
        {
            "session": session,
            "booked_enrolments": booked_enrolments,
            "waitlist_enrolments": waitlist_enrolments,
            "return_to": return_to,
            "is_professional_view": False,
            "can_manage_session": True,
            "billing_month": billing_month_value,
            "monthly_charges": monthly_charges,
            "monthly_totals": monthly_totals,
        },
    )
