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
from core.utils.pricing import compute_pricing, compute_group_monthly_pricing
from core.utils.holidays import is_portuguese_holiday
from core.services.audit import log_audit_event, snapshot_instance
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
    ClinicSettings,
)
from core.services.scheduling import (
    build_slots,
    get_working_weekdays,
    get_working_blocks,
    is_time_in_working_blocks,
)

GROUP_SCHEDULE_LOOKAHEAD_WEEKS = 12


def log_appt(action, appt, actor, *, old_date=None, old_time=None, new_date=None, new_time=None,
             old_status=None, new_status=None, note="", request=None):
    AppointmentLog.objects.create(
        appointment=appt,
        action=action,
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        old_date=old_date,
        old_time=old_time,
        new_date=new_date,
        new_time=new_time,
        old_status=old_status,
        new_status=new_status,
        note=note or "",
    )
    before = {}
    after = snapshot_instance(
        appt,
        fields=[
            "client_id",
            "professional_id",
            "service_id",
            "date",
            "time",
            "status",
            "is_paid",
            "paid_at",
            "completed_at",
            "completed_by_id",
            "final_price",
        ],
    )
    if old_date is not None:
        before["date"] = old_date
    if old_time is not None:
        before["time"] = old_time
    if old_status is not None:
        before["status"] = old_status
    if new_date is not None:
        after["date"] = new_date
    if new_time is not None:
        after["time"] = new_time
    if new_status is not None:
        after["status"] = new_status

    log_audit_event(
        category="appointments",
        action=action,
        request=request,
        actor=actor,
        instance=appt,
        source="appointment_log",
        message=note or dict(AppointmentLog.ACTION_CHOICES).get(action, action),
        before=before,
        after=after,
        metadata={"appointment_log_action": action, "note": note or ""},
    )


def apply_bulk_appointment_action(
    *,
    appointments,
    action,
    actor,
    today=None,
    now_t=None,
    audit_source="",
):
    valid_actions = {
        "confirm_selected",
        "confirm_and_paid_selected",
        "mark_completed_and_paid_selected",
        "mark_no_show_selected",
        "mark_completed_selected",
        "mark_in_debt_selected",
    }
    if action not in valid_actions:
        raise ValueError("Ação inválida.")

    today = today or timezone.localdate()
    now_t = now_t or timezone.localtime().time()
    now_ts = timezone.now()

    result = {
        "selected": 0,
        "updated": 0,
        "status_changed": 0,
        "paid_changed": 0,
        "skipped_future": 0,
        "skipped_locked": 0,
        "skipped_unpaid": 0,
        "unchanged": 0,
        "status_transitions": [],
    }
    hard_locked_statuses = {
        Appointment.STATUS_CANCELLED,
        Appointment.STATUS_NO_SHOW,
    }
    locked_statuses = {
        Appointment.STATUS_COMPLETED,
        Appointment.STATUS_IN_DEBT,
        Appointment.STATUS_CANCELLED,
        Appointment.STATUS_NO_SHOW,
    }
    audit_note = f"Ação em massa ({audit_source or 'geral'}): {action}"

    with transaction.atomic():
        for appt in appointments:
            result["selected"] += 1
            old_status = appt.status
            changed_fields = []
            is_past = (
                appt.date < today
                or (appt.date == today and (appt.time or dtime.min) < now_t)
            )

            if action == "mark_no_show_selected":
                if not is_past:
                    result["skipped_future"] += 1
                    continue
                if appt.status in locked_statuses:
                    result["skipped_locked"] += 1
                    continue
                if appt.status != Appointment.STATUS_NO_SHOW:
                    appt.status = Appointment.STATUS_NO_SHOW
                    changed_fields.append("status")
            elif action == "mark_completed_selected":
                if not is_past:
                    result["skipped_future"] += 1
                    continue
                if appt.status in hard_locked_statuses or appt.status == Appointment.STATUS_COMPLETED:
                    result["skipped_locked"] += 1
                    continue
                if not appt.is_paid:
                    result["skipped_unpaid"] += 1
                    continue
                if appt.status != Appointment.STATUS_COMPLETED:
                    appt.status = Appointment.STATUS_COMPLETED
                    changed_fields.append("status")
            elif action == "mark_completed_and_paid_selected":
                if not is_past:
                    result["skipped_future"] += 1
                    continue
                if appt.status in hard_locked_statuses or appt.status == Appointment.STATUS_COMPLETED:
                    result["skipped_locked"] += 1
                    continue
                if not appt.is_paid:
                    appt.is_paid = True
                    appt.paid_at = now_ts
                    changed_fields.extend(["is_paid", "paid_at"])
                    result["paid_changed"] += 1
                if appt.status != Appointment.STATUS_COMPLETED:
                    appt.status = Appointment.STATUS_COMPLETED
                    changed_fields.append("status")
            elif action == "mark_in_debt_selected":
                if not is_past:
                    result["skipped_future"] += 1
                    continue
                if appt.status in hard_locked_statuses or appt.status == Appointment.STATUS_COMPLETED:
                    result["skipped_locked"] += 1
                    continue
                if appt.status != Appointment.STATUS_IN_DEBT:
                    appt.status = Appointment.STATUS_IN_DEBT
                    changed_fields.append("status")
            else:
                if appt.status in locked_statuses:
                    result["skipped_locked"] += 1
                    continue
                if is_past and appt.status in {
                    Appointment.STATUS_PENDING,
                    Appointment.STATUS_SCHEDULED,
                    Appointment.STATUS_AWAITING_VALIDATION,
                }:
                    new_status = Appointment.STATUS_AWAITING_VALIDATION
                elif appt.status == Appointment.STATUS_PENDING:
                    new_status = Appointment.STATUS_SCHEDULED
                else:
                    new_status = appt.status

                if new_status != appt.status:
                    appt.status = new_status
                    changed_fields.append("status")

                if action == "confirm_and_paid_selected" and not appt.is_paid:
                    appt.is_paid = True
                    appt.paid_at = now_ts
                    changed_fields.extend(["is_paid", "paid_at"])
                    result["paid_changed"] += 1

            if "status" in changed_fields:
                if appt.status in {Appointment.STATUS_COMPLETED, Appointment.STATUS_IN_DEBT}:
                    appt.completed_at = now_ts
                    appt.completed_by = actor
                    changed_fields.extend(["completed_at", "completed_by"])
                elif appt.completed_at or appt.completed_by:
                    appt.completed_at = None
                    appt.completed_by = None
                    changed_fields.extend(["completed_at", "completed_by"])

            if not changed_fields:
                result["unchanged"] += 1
                continue

            appt.save(update_fields=list(dict.fromkeys(changed_fields)))
            result["updated"] += 1

            if "status" in changed_fields:
                result["status_changed"] += 1
                result["status_transitions"].append(
                    {
                        "appointment": appt,
                        "old_status": old_status,
                        "new_status": appt.status,
                    }
                )
                log_appt(
                    AppointmentLog.ACTION_STATUS_UPDATED,
                    appt,
                    actor,
                    old_status=old_status,
                    new_status=appt.status,
                    note=audit_note,
                )

    return result


def can_modify_appointment(user, appointment):
    if can_view_all_calendar(user):
        return True

    prof = Professional.objects.filter(user=user).first()
    if prof:
        return appointment.professional_id == prof.id

    return appointment.client_id == user.id


def professional_weekdays_labels(prof):
    map_pt = {
        0: "segunda-feira",
        1: "terça-feira",
        2: "quarta-feira",
        3: "quinta-feira",
        4: "sexta-feira",
        5: "sábado",
        6: "domingo",
    }
    weekdays = get_working_weekdays(prof)
    return [map_pt.get(w, str(w)) for w in weekdays]


def professional_weekdays(prof):
    return get_working_weekdays(prof)


def professional_works_on_date(prof, date_obj):
    return bool(get_working_blocks(prof, date_obj))


def _status_label(value: str) -> str:
    labels = {
        "scheduled": "Agendada",
        "pending_confirmation": "Em confirmação",
        "awaiting_validation": "A aguardar validação",
        "no_show": "Falta",
        "completed": "Concluída",
        "in_debt": "Em dívida",
        "cancelled": "Cancelada",
    }
    return labels.get(value or "", value or "-")


def update_group_sessions_statuses():
    today = timezone.localdate()
    now_t = timezone.localtime().time()
    GroupSession.objects.filter(
        date__lt=today,
        status=GroupSession.STATUS_SCHEDULED,
    ).update(status=GroupSession.STATUS_COMPLETED)
    GroupSession.objects.filter(
        date=today,
        time__lt=now_t,
        status=GroupSession.STATUS_SCHEDULED,
    ).update(status=GroupSession.STATUS_COMPLETED)


def group_booked_statuses():
    return [
        GroupEnrollment.STATUS_BOOKED,
        GroupEnrollment.STATUS_ATTENDED,
        GroupEnrollment.STATUS_NO_SHOW,
    ]


def _month_floor(date_obj):
    return date_obj.replace(day=1)


def _next_month(date_obj):
    if date_obj.month == 12:
        return date_obj.replace(year=date_obj.year + 1, month=1, day=1)
    return date_obj.replace(month=date_obj.month + 1, day=1)


def group_schedule_family_key(schedule):
    if not schedule:
        return ""
    name_key = (schedule.name or "").strip().lower()
    time_key = schedule.time.strftime("%H:%M") if schedule.time else ""
    start_key = schedule.start_date.isoformat() if schedule.start_date else ""
    return f"{schedule.service_id}|{schedule.professional_id}|{name_key}|{time_key}|{start_key}"


def ensure_group_monthly_charges(start_date=None, end_date=None, *, client_ids=None, family_keys=None):
    """
    Gera/atualiza mensalidades de turma por cliente/mês (não por sessão).
    """
    today = timezone.localdate()
    start_ref = _month_floor(start_date or today)
    end_ref = _month_floor(end_date or today)
    if end_ref < start_ref:
        start_ref, end_ref = end_ref, start_ref
    end_exclusive = _next_month(end_ref)

    enrolled_qs = (
        GroupEnrollment.objects
        .select_related(
            "session",
            "session__schedule",
            "session__service",
            "session__professional",
            "client",
            "client__client_profile",
        )
        .filter(
            status__in=group_booked_statuses(),
            session__status__in=[GroupSession.STATUS_SCHEDULED, GroupSession.STATUS_COMPLETED],
            session__schedule__isnull=False,
            session__date__gte=start_ref,
            session__date__lt=end_exclusive,
        )
    )
    if client_ids:
        enrolled_qs = enrolled_qs.filter(client_id__in=list(client_ids))

    valid_family_keys = set(family_keys or [])
    wanted_rows = {}
    for enrolment in enrolled_qs:
        session = enrolment.session
        schedule = session.schedule
        family_key = group_schedule_family_key(schedule)
        if not family_key:
            continue
        if valid_family_keys and family_key not in valid_family_keys:
            continue
        month_key = _month_floor(session.date)
        map_key = (enrolment.client_id, family_key, month_key)
        existing = wanted_rows.get(map_key)
        if existing is None:
            wanted_rows[map_key] = {
                "client": enrolment.client,
                "client_profile": getattr(enrolment.client, "client_profile", None),
                "schedule": schedule,
                "service": session.service,
                "professional": session.professional,
                "class_name": session.name or (schedule.name if schedule else "") or (session.service.name if session.service else ""),
                "month": month_key,
                "family_key": family_key,
            }

    memberships = (
        GroupMembership.objects
        .filter(
            is_active=True,
            client_id__in=[row["client"].id for row in wanted_rows.values()],
            family_key__in=[row["family_key"] for row in wanted_rows.values()],
        )
        .select_related("client")
    )
    membership_map = {(item.client_id, item.family_key): item for item in memberships}

    created = 0
    updated = 0
    for row in wanted_rows.values():
        membership = membership_map.get((row["client"].id, row["family_key"]))
        pricing = compute_group_monthly_pricing(
            row["service"],
            row["client_profile"],
            monthly_price_override=membership.monthly_price_override if membership else None,
        )
        charge, was_created = GroupMonthlyCharge.objects.get_or_create(
            client=row["client"],
            family_key=row["family_key"],
            month=row["month"],
            defaults={
                "service": row["service"],
                "professional": row["professional"],
                "schedule": row["schedule"],
                "class_name": row["class_name"],
                "base_price": pricing["base_price_applied"],
                "partner": pricing["partner"],
                "partner_price": pricing["partner_price_applied"],
                "discount_type": pricing["discount_type"],
                "discount_value": pricing["discount_value"],
                "discount_applied": pricing["discount_applied"],
                "final_price": pricing["final_price"],
                "status": GroupMonthlyCharge.STATUS_UNPAID,
            },
        )
        if was_created:
            created += 1
            continue

        if charge.status == GroupMonthlyCharge.STATUS_PAID:
            continue

        changed = (
            charge.service_id != row["service"].id
            or charge.professional_id != (row["professional"].id if row["professional"] else None)
            or charge.schedule_id != (row["schedule"].id if row["schedule"] else None)
            or charge.class_name != row["class_name"]
            or charge.base_price != pricing["base_price_applied"]
            or charge.partner_id != (pricing["partner"].id if pricing["partner"] else None)
            or charge.partner_price != pricing["partner_price_applied"]
            or charge.discount_type != pricing["discount_type"]
            or charge.discount_value != pricing["discount_value"]
            or charge.discount_applied != pricing["discount_applied"]
            or charge.final_price != pricing["final_price"]
        )
        if not changed:
            continue

        charge.service = row["service"]
        charge.professional = row["professional"]
        charge.schedule = row["schedule"]
        charge.class_name = row["class_name"]
        charge.base_price = pricing["base_price_applied"]
        charge.partner = pricing["partner"]
        charge.partner_price = pricing["partner_price_applied"]
        charge.discount_type = pricing["discount_type"]
        charge.discount_value = pricing["discount_value"]
        charge.discount_applied = pricing["discount_applied"]
        charge.final_price = pricing["final_price"]
        charge.save(
            update_fields=[
                "service",
                "professional",
                "schedule",
                "class_name",
                "base_price",
                "partner",
                "partner_price",
                "discount_type",
                "discount_value",
                "discount_applied",
                "final_price",
                "updated_at",
            ]
        )
        updated += 1

    return {"created": created, "updated": updated}


def apply_terms_filter(qs, q, lookups):
    terms = [t for t in (q or "").split() if t]
    if not terms:
        return qs
    for term in terms:
        term_q = Q()
        for lookup in lookups:
            term_q |= Q(**{lookup: term})
        qs = qs.filter(term_q)
    return qs


def normalize_phone_number(value):
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def normalize_client_name(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.lower().strip().split())


def normalize_email_address(value):
    return str(value or "").strip().lower()


def build_client_name_phone_key(full_name, phone):
    name_key = normalize_client_name(full_name)
    phone_key = normalize_phone_number(phone)
    if not name_key or not phone_key:
        return ""
    return f"{name_key}|{phone_key}"


def find_potential_duplicate_clients(full_name, phone="", email="", *, exclude_profile_id=None, limit=5):
    name_key = normalize_client_name(full_name)
    phone_key = normalize_phone_number(phone)
    phone_tail = phone_key[-9:] if len(phone_key) >= 9 else phone_key
    email_key = normalize_email_address(email)
    name_tokens = [token for token in name_key.split() if len(token) >= 3]

    if not any([name_key, phone_key, email_key]):
        return []

    qs = ClientProfile.objects.select_related("user").all()
    if exclude_profile_id:
        qs = qs.exclude(pk=exclude_profile_id)

    search_q = Q()
    if phone_key:
        search_q |= Q(phone__icontains=phone_key)
        if phone_tail and phone_tail != phone_key:
            search_q |= Q(phone__endswith=phone_tail)
    if email_key:
        search_q |= Q(user__email__iexact=email_key)
    if name_tokens:
        token_q = Q()
        for token in name_tokens[:3]:
            token_q &= Q(full_name__icontains=token)
        search_q |= token_q
    elif name_key:
        search_q |= Q(full_name__icontains=str(full_name or "").strip())

    candidates = []
    for profile in qs.filter(search_q).distinct():
        profile_name_key = normalize_client_name(profile.full_name)
        profile_phone_key = normalize_phone_number(profile.phone)
        profile_phone_tail = profile_phone_key[-9:] if len(profile_phone_key) >= 9 else profile_phone_key
        profile_email_key = normalize_email_address(profile.user.email if getattr(profile, "user_id", None) else "")

        name_exact = bool(name_key and profile_name_key == name_key)
        name_close = bool(
            name_key
            and profile_name_key
            and (
                profile_name_key in name_key
                or name_key in profile_name_key
                or all(token in profile_name_key for token in name_tokens[:2])
            )
        )
        phone_match = bool(
            phone_key
            and profile_phone_key
            and (
                profile_phone_key == phone_key
                or (phone_tail and profile_phone_tail == phone_tail)
            )
        )
        email_match = bool(email_key and profile_email_key and profile_email_key == email_key)

        if not (email_match or (phone_match and (name_exact or name_close))):
            continue

        reasons = []
        score = 0
        if email_match:
            reasons.append("email")
            score += 6
        if phone_match:
            reasons.append("telefone")
            score += 4
        if name_exact:
            reasons.append("nome")
            score += 4
        elif name_close:
            reasons.append("nome semelhante")
            score += 2

        candidates.append(
            {
                "profile": profile,
                "reasons": reasons,
                "score": score,
            }
        )

    candidates.sort(key=lambda item: (-item["score"], item["profile"].full_name.lower(), item["profile"].id))
    return candidates[:limit]


def find_existing_client_by_name_phone(full_name, phone, *, exclude_profile_id=None):
    matches = find_potential_duplicate_clients(
        full_name,
        phone=phone,
        exclude_profile_id=exclude_profile_id,
        limit=1,
    )
    return matches[0]["profile"] if matches else None


def promote_group_waitlist(session):
    if not session or session.capacity_value <= 0:
        return None
    if session.spots_left <= 0:
        return None
    wait = (
        session.enrolments
        .filter(status=GroupEnrollment.STATUS_WAITLIST)
        .order_by("created_at")
        .first()
    )
    if not wait:
        return None
    wait.status = GroupEnrollment.STATUS_BOOKED
    wait.save(update_fields=["status", "updated_at"])
    return wait


def can_cancel_group_enrollment(user, session):
    if can_access_backoffice(user) or can_view_all_calendar(user):
        return True
    now_dt = timezone.localtime()
    start_dt = session.start_datetime
    hours_limit = ClinicSettings.get_solo().group_cancel_hours or 0
    return start_dt >= (now_dt + timedelta(hours=hours_limit))


def _next_weekday(date_obj, weekday):
    delta = (weekday - date_obj.weekday()) % 7
    return date_obj + timedelta(days=delta)


def ensure_group_sessions_for_schedules(
    schedules=None,
    *,
    start_date=None,
    weeks=None,
):
    lookahead = weeks or GROUP_SCHEDULE_LOOKAHEAD_WEEKS
    today = timezone.localdate()
    start = start_date or today
    if start < today:
        start = today
    horizon = start + timedelta(weeks=lookahead)

    if schedules is None:
        schedules = GroupSchedule.objects.filter(is_active=True).select_related("service", "professional")

    for schedule in schedules:
        if not schedule.is_active:
            continue
        if schedule.service.service_type != "group":
            continue
        family_schedule_ids = list(
            GroupSchedule.objects.filter(
                service_id=schedule.service_id,
                professional_id=schedule.professional_id,
                name=schedule.name,
                time=schedule.time,
                start_date=schedule.start_date,
            ).values_list("id", flat=True)
        ) or [schedule.id]
        first = schedule.start_date
        if start > first:
            first = start
        first = _next_weekday(first, schedule.weekday)
        current = first
        while current <= horizon:
            if current < schedule.start_date:
                current += timedelta(days=7)
                continue
            if Appointment.objects.filter(
                professional=schedule.professional,
                date=current,
                time=schedule.time,
            ).exists():
                current += timedelta(days=7)
                continue
            if GroupSession.objects.filter(
                professional=schedule.professional,
                date=current,
                time=schedule.time,
            ).exists():
                current += timedelta(days=7)
                continue
            new_session = GroupSession.objects.create(
                service=schedule.service,
                name=schedule.name,
                professional=schedule.professional,
                date=current,
                time=schedule.time,
                capacity=schedule.capacity,
                duration_minutes=schedule.duration_minutes,
                notes=schedule.notes,
                schedule=schedule,
            )
            reference_session = (
                GroupSession.objects
                .filter(
                    schedule_id__in=family_schedule_ids,
                    status=GroupSession.STATUS_SCHEDULED,
                    date__gte=today,
                )
                .exclude(id=new_session.id)
                .annotate(
                    active_enrolments=Count(
                        "enrolments",
                        filter=Q(
                            enrolments__status__in=[
                                GroupEnrollment.STATUS_BOOKED,
                                GroupEnrollment.STATUS_WAITLIST,
                            ]
                        ),
                    )
                )
                .filter(active_enrolments__gt=0)
                .order_by("-date", "-time")
                .first()
            )
            if reference_session:
                for template in GroupEnrollment.objects.filter(
                    session=reference_session,
                    status__in=[
                        GroupEnrollment.STATUS_BOOKED,
                        GroupEnrollment.STATUS_WAITLIST,
                    ],
                ):
                    GroupEnrollment.objects.get_or_create(
                        session=new_session,
                        client=template.client,
                        defaults={"status": template.status},
                    )
            current += timedelta(days=7)


def _time_range(start: dtime, end: dtime, step_minutes: int):
    current = datetime.combine(datetime.today().date(), start)
    end_dt = datetime.combine(datetime.today().date(), end)
    step = timedelta(minutes=step_minutes)
    while current < end_dt:
        yield current.time().replace(second=0, microsecond=0)
        current += step


def _service_slot_step_minutes(service, fallback_duration=None):
    duration = getattr(service, "duration_minutes", None) or fallback_duration or 30
    slot_interval = getattr(service, "slot_interval_minutes", None) or duration
    return max(min(slot_interval, duration), 1)


def _service_simultaneous_capacity(service):
    if not service or getattr(service, "service_type", None) == "group":
        return 1
    return max(getattr(service, "capacity", None) or 1, 1)


def _appointment_intervals_for_professional_day(prof: Professional, date_obj, *, exclude_appointment_id=None):
    intervals = []
    appointments = (
        Appointment.objects
        .filter(professional=prof, date=date_obj)
        .exclude(status=Appointment.STATUS_CANCELLED)
        .select_related("service")
    )
    if exclude_appointment_id:
        appointments = appointments.exclude(id=exclude_appointment_id)
    for appointment in appointments:
        duration = getattr(appointment.service, "duration_minutes", None) or 30
        start_dt = datetime.combine(date_obj, appointment.time)
        intervals.append((start_dt.time(), (start_dt + timedelta(minutes=duration)).time()))
    return intervals


def _group_session_intervals_for_professional_day(prof: Professional, date_obj):
    intervals = []
    group_sessions = (
        GroupSession.objects
        .filter(professional=prof, date=date_obj, status=GroupSession.STATUS_SCHEDULED)
        .select_related("service")
    )
    for session in group_sessions:
        duration = session.duration_minutes or getattr(session.service, "duration_minutes", None) or 60
        start_dt = datetime.combine(date_obj, session.time)
        intervals.append((start_dt.time(), (start_dt + timedelta(minutes=duration)).time()))
    return intervals


def _occupied_intervals_for_professional_day(prof: Professional, date_obj):
    return (
        _appointment_intervals_for_professional_day(prof, date_obj)
        + _group_session_intervals_for_professional_day(prof, date_obj)
    )


def _get_slots(prof: Professional, date_obj, step_minutes: int = None, *, service=None, exclude_appointment_id=None):
    if is_portuguese_holiday(date_obj):
        return []
    duration_minutes = getattr(service, "duration_minutes", None) or step_minutes or 30
    blocked = (
        BlockedSlot.objects
        .filter(professional=prof, date=date_obj)
        .values_list("time", flat=True)
    )
    return build_slots(
        prof,
        date_obj,
        service_duration_minutes=duration_minutes,
        blocked_slots=blocked,
        occupied_intervals=_appointment_intervals_for_professional_day(
            prof,
            date_obj,
            exclude_appointment_id=exclude_appointment_id,
        ),
        hard_blocked_intervals=_group_session_intervals_for_professional_day(prof, date_obj),
        slot_step_minutes=_service_slot_step_minutes(service, duration_minutes),
        simultaneous_capacity=_service_simultaneous_capacity(service),
    )


def _is_slot_occupied(prof: Professional, date_obj, time_obj, *, service=None, exclude_appointment_id=None) -> bool:
    if service is not None:
        duration = getattr(service, "duration_minutes", None) or 30
        capacity = _service_simultaneous_capacity(service)
        slot_start = datetime.combine(date_obj, time_obj)
        slot_end = slot_start + timedelta(minutes=duration)
        for group_start, group_end in _group_session_intervals_for_professional_day(prof, date_obj):
            group_start_dt = datetime.combine(date_obj, group_start)
            group_end_dt = datetime.combine(date_obj, group_end)
            if slot_start < group_end_dt and group_start_dt < slot_end:
                return True
        overlap_count = 0
        for appt_start, appt_end in _appointment_intervals_for_professional_day(
            prof,
            date_obj,
            exclude_appointment_id=exclude_appointment_id,
        ):
            appt_start_dt = datetime.combine(date_obj, appt_start)
            appt_end_dt = datetime.combine(date_obj, appt_end)
            if slot_start < appt_end_dt and appt_start_dt < slot_end:
                overlap_count += 1
                if overlap_count >= capacity:
                    return True
        return False
    if Appointment.objects.filter(
        professional=prof,
        date=date_obj,
        time=time_obj,
    ).exclude(status=Appointment.STATUS_CANCELLED).exists():
        return True
    return GroupSession.objects.filter(
        professional=prof,
        date=date_obj,
        time=time_obj,
        status=GroupSession.STATUS_SCHEDULED,
    ).exists()


def _find_matching_cancelled_appointment(*, client_user, professional, service, date_obj, time_obj):
    if not all([client_user, professional, service, date_obj, time_obj]):
        return None
    return (
        Appointment.objects
        .filter(
            client=client_user,
            professional=professional,
            service=service,
            date=date_obj,
            time=time_obj,
            status=Appointment.STATUS_CANCELLED,
        )
        .order_by("-created_at", "-id")
        .first()
    )


def _is_slot_blocked(prof: Professional, date_obj, time_obj) -> bool:
    return BlockedSlot.objects.filter(
        professional=prof,
        date=date_obj,
        time=time_obj,
    ).exists()


def _has_availability_window(prof: Professional, date_obj, time_obj) -> bool:
    if is_portuguese_holiday(date_obj):
        return False
    return is_time_in_working_blocks(prof, date_obj, time_obj)


def is_professional_available_at(prof: Professional, date_obj, time_obj) -> bool:
    if not _has_availability_window(prof, date_obj, time_obj):
        return False
    if _is_slot_blocked(prof, date_obj, time_obj):
        return False
    return not _is_slot_occupied(prof, date_obj, time_obj)


_SERIES_FREQUENCY_ALIASES = {
    "2x": "2x_week",
    "3x": "3x_week",
    "4x": "4x_week",
    "2x_weekly": "2x_week",
    "3x_weekly": "3x_week",
    "4x_weekly": "4x_week",
}

_SERIES_WEEKLY_QUOTAS = {
    "2x_week": 2,
    "3x_week": 3,
    "4x_week": 4,
}


def normalize_series_frequency(freq: str) -> str:
    value = (freq or "").strip().lower()
    if not value:
        return "weekly"
    value = _SERIES_FREQUENCY_ALIASES.get(value, value)
    allowed = {"daily", "weekly", "2x_week", "3x_week", "4x_week", "weekdays"}
    return value if value in allowed else "weekly"


def series_frequency_weekly_quota(freq: str):
    return _SERIES_WEEKLY_QUOTAS.get(normalize_series_frequency(freq))


def _build_series_dates(start_date, count, freq, prof=None):
    freq = normalize_series_frequency(freq)
    weekly_quota = series_frequency_weekly_quota(freq)
    dates = []
    current = start_date
    current_week_start = None
    sessions_in_week = 0
    while len(dates) < count:
        if weekly_quota:
            week_start = current - timedelta(days=current.weekday())
            if current_week_start != week_start:
                current_week_start = week_start
                sessions_in_week = 0
            if sessions_in_week >= weekly_quota:
                current = current_week_start + timedelta(days=7)
                continue

        should_add = True
        if freq == "weekdays" and current.weekday() >= 5:
            should_add = False
        if prof and not professional_works_on_date(prof, current):
            should_add = False

        if should_add:
            dates.append(current)
            if weekly_quota:
                sessions_in_week += 1

        if freq == "weekly":
            current = current + timedelta(days=7)
        else:
            current = current + timedelta(days=1)
    return dates


def _get_professional_or_403(user):
    """Devolve Professional do user ou None se não for profissional (admin/rececao não precisam)."""
    if can_view_all_calendar(user):
        return None  # admin não precisa de prof fixo
    if hasattr(user, "professional") and user.professional:
        return user.professional
    return None


def _monday_of_week(d):
    return d - timedelta(days=d.weekday())


def _apply_profile_autofocus(form, profile):
    # Remove autofocus prévio (se existir)
    for f in form.fields.values():
        f.widget.attrs.pop("autofocus", None)

    # Ordem de prioridade para completar o perfil
    priority_fields = [
        "full_name",
        "phone",
        "email",
        "gender",
        "nif",
        "address_line1",
        "postal_code",
        "locality",
        "county",
        "district",
        "city",
    ]

    # Usa dados do form se estiver bound; caso contrário usa o profile
    def _value(name):
        if form.is_bound:
            return (form.data.get(name) or "").strip()
        return (getattr(profile, name, "") or "").strip()

    for name in priority_fields:
        if name in form.fields and not _value(name):
            form.fields[name].widget.attrs["autofocus"] = "autofocus"
            break


def _safe_return_to(request, return_to):
    if not return_to:
        return None
    if url_has_allowed_host_and_scheme(
        return_to,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return return_to
    return None


__all__ = [
    "log_appt",
    "apply_bulk_appointment_action",
    "can_modify_appointment",
    "apply_terms_filter",
    "professional_weekdays_labels",
    "professional_weekdays",
    "professional_works_on_date",
    "_status_label",
    "_time_range",
    "_get_slots",
    "_is_slot_blocked",
    "_is_slot_occupied",
    "_has_availability_window",
    "is_professional_available_at",
    "normalize_series_frequency",
    "series_frequency_weekly_quota",
    "_build_series_dates",
    "_get_professional_or_403",
    "_monday_of_week",
    "_safe_return_to",
    "_apply_profile_autofocus",
    "update_group_sessions_statuses",
    "group_booked_statuses",
    "group_schedule_family_key",
    "ensure_group_monthly_charges",
]
