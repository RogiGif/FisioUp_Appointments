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
from core.utils.pricing import compute_pricing
from core.services.subcontracting import sync_subcontractor_payout
from core.utils.holidays import is_portuguese_holiday
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
    MoloniIntegration,
    ClientImportLog,
    ClientImportBatch,
    ClientImportRow,
    Partner,
    PartnerServicePrice,
    ContentPost,
)

from core.views.common import *

SERIES_WEEKDAY_OPTIONS = [
    ("0", "Segunda-feira"),
    ("1", "Terça-feira"),
    ("2", "Quarta-feira"),
    ("3", "Quinta-feira"),
    ("4", "Sexta-feira"),
]


def _parse_series_weekdays(payload):
    if not payload:
        return []
    raw_values = payload.getlist("weekdays") if hasattr(payload, "getlist") else []
    normalized = []
    seen = set()
    for raw in raw_values:
        value = str(raw or "").strip()
        if value not in {"0", "1", "2", "3", "4"}:
            continue
        if value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def _validate_series_weekdays(freq, weekdays):
    freq_value = normalize_series_frequency(freq)
    if freq_value == "weekly":
        return ""
    quota = series_frequency_weekly_quota(freq)
    if quota and len(weekdays) != quota:
        return f"Para a frequência selecionada tens de escolher exatamente {quota} dia(s) da semana."
    return ""


def _series_weekday_matches(date_obj, freq, weekdays):
    if normalize_series_frequency(freq) == "daily":
        return True
    if weekdays:
        return str(date_obj.weekday()) in weekdays
    return not (normalize_series_frequency(freq) == "weekdays" and date_obj.weekday() >= 5)


def _book_series_client_view(request, profile):
    today = timezone.localdate()
    now_t = timezone.localtime().time()
    now_t = timezone.localtime().time()

    services = Service.objects.all().order_by("name")
    service_id = (request.GET.get("service_id") or request.POST.get("service_id") or "").strip()
    start_date_str = (request.GET.get("start_date") or request.POST.get("start_date") or "").strip()
    count_str = (request.GET.get("count") or request.POST.get("count") or "").strip()
    freq = normalize_series_frequency(request.GET.get("freq") or request.POST.get("freq") or "")
    preferred_professional_id = (request.GET.get("professional_id") or request.POST.get("professional_id") or "").strip()
    selected_weekdays = _parse_series_weekdays(request.POST if request.method == "POST" else request.GET)

    professionals_qs = Professional.objects.select_related("user").all().order_by("user__username")
    if service_id:
        professionals_qs = professionals_qs.filter(services__id=service_id).distinct()
        if not preferred_professional_id:
            only_one = list(professionals_qs[:2])
            if len(only_one) == 1:
                preferred_professional_id = str(only_one[0].id)
    else:
        professionals_qs = professionals_qs.none()

    sessions = []
    general_errors = []
    line_errors = {}
    max_count = 20

    if request.method == "POST" and (request.POST.get("action") or "").strip() == "confirm":
        dates = request.POST.getlist("session_date")
        prof_ids = request.POST.getlist("session_professional_id")
        times = request.POST.getlist("session_time")
        symptomatology_global = (request.POST.get("symptomatology_global") or "").strip()

        if not service_id:
            general_errors.append("Seleciona o serviço.")
        if not dates or len(dates) != len(prof_ids) or len(dates) != len(times):
            general_errors.append("Dados das sessões inválidos.")

        if service_id and not Service.objects.filter(id=service_id).exists():
            general_errors.append("Serviço inválido.")

        service = Service.objects.filter(id=service_id).first()
        if service and service.service_type == "group":
            general_errors.append("Este serviço é de turma. Usa a gestão de turmas.")
        weekdays_error = _validate_series_weekdays(freq, selected_weekdays)
        if weekdays_error:
            general_errors.append(weekdays_error)

        for idx, (d, p, t) in enumerate(zip(dates, prof_ids, times)):
            row_error = ""
            try:
                date_obj = datetime.strptime(d, "%Y-%m-%d").date()
            except Exception:
                row_error = "Data inválida."
                line_errors[idx] = row_error
                continue

            if not service:
                row_error = "Serviço inválido."
            elif date_obj < today:
                row_error = "Data no passado."
            elif not p:
                row_error = "Escolhe o profissional."
            elif not t:
                row_error = "Escolhe o horário."
            elif not Professional.objects.filter(id=p, services__id=service_id).exists():
                row_error = "Profissional inválido para este serviço."
            else:
                prof = Professional.objects.filter(id=p).first()
                time_obj = datetime.strptime(t, "%H:%M").time()
                if date_obj == today and time_obj <= now_t:
                    row_error = "Este horário já passou."
                elif not professional_works_on_date(prof, date_obj):
                    row_error = "Profissional não atende nesse dia."
                elif _is_slot_blocked(prof, date_obj, time_obj):
                    row_error = "Horário indisponível."
                else:
                    slots_now = _get_slots(prof, date_obj, service=service)
                    if t not in slots_now:
                        row_error = "Horário já não disponível."

            if row_error:
                line_errors[idx] = row_error

        if general_errors or line_errors:
            for idx, d in enumerate(dates):
                date_display = d
                try:
                    date_display = datetime.strptime(d, "%Y-%m-%d").date()
                except Exception:
                    pass
                sessions.append(
                    {
                        "date_display": date_display,
                        "date_value": d,
                        "professional_id": prof_ids[idx] if idx < len(prof_ids) else "",
                        "time": times[idx] if idx < len(times) else "",
                        "error": line_errors.get(idx, ""),
                    }
                )
        else:
            series_id = uuid4()
            created = 0
            client_user = profile.user
            client_profile = profile
            pricing = compute_pricing(service, client_profile)
            with transaction.atomic():
                for d, p, t in zip(dates, prof_ids, times):
                    date_obj = datetime.strptime(d, "%Y-%m-%d").date()
                    time_obj = datetime.strptime(t, "%H:%M").time()
                    prof = Professional.objects.get(id=p)
                    Appointment.objects.create(
                        client=client_user,
                        professional=prof,
                        service=service,
                        date=date_obj,
                        time=time_obj,
                        symptomatology=symptomatology_global,
                        series_id=series_id,
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
                    created += 1

            messages.success(request, f"Foram enviadas {created} marcações para confirmação.")
            return redirect("my_appointments")

    if service_id and start_date_str and count_str:
        try:
            count = max(1, int(count_str))
        except Exception:
            count = 0
        if count > max_count:
            count = max_count
            general_errors.append("O máximo por série é 20 sessões.")

        try:
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        except Exception:
            start_date = None
            general_errors.append("Data inicial inválida.")

        service = Service.objects.filter(id=service_id).first()
        if service and service.service_type == "group":
            general_errors.append("Este serviço é de turma. Usa a gestão de turmas.")
        weekdays_error = _validate_series_weekdays(freq, selected_weekdays)
        if weekdays_error:
            general_errors.append(weekdays_error)

        prof_for_series = None
        if preferred_professional_id:
            prof_for_series = Professional.objects.filter(
                id=preferred_professional_id, services__id=service_id
            ).first()
        prof_candidates = list(professionals_qs)
        if count and start_date and not general_errors:
            if not prof_for_series and not prof_candidates:
                general_errors.append("Sem profissionais disponíveis para este serviço.")
            else:
                current_start = start_date if start_date >= today else today
                weekly_quota = series_frequency_weekly_quota(freq)
                current_week_start = None
                sessions_in_week = 0
                attempts = 0
                max_attempts = max(30, count * 30)
                while len(sessions) < count and attempts < max_attempts:
                    attempts += 1

                    if weekly_quota:
                        week_start = current_start - timedelta(days=current_start.weekday())
                        if current_week_start != week_start:
                            current_week_start = week_start
                            sessions_in_week = 0
                        if sessions_in_week >= weekly_quota:
                            current_start = current_week_start + timedelta(days=7)
                            continue

                    if not _series_weekday_matches(current_start, freq, selected_weekdays):
                        current_start = current_start + timedelta(days=1)
                        continue

                    prof_for_row = None
                    slot_time = ""

                    if prof_for_series:
                        if professional_works_on_date(prof_for_series, current_start):
                            slots_now = _get_slots(prof_for_series, current_start, service=service)
                            if slots_now:
                                prof_for_row = prof_for_series
                                slot_time = slots_now[0]
                    else:
                        for p in prof_candidates:
                            if not professional_works_on_date(p, current_start):
                                continue
                            slots_now = _get_slots(p, current_start, service=service)
                            if slots_now:
                                prof_for_row = p
                                slot_time = slots_now[0]
                                break

                    if prof_for_row and slot_time:
                        sessions.append(
                            {
                                "date_display": current_start,
                                "date_value": current_start.strftime("%Y-%m-%d"),
                                "professional_id": str(prof_for_row.id),
                                "time": slot_time,
                                "error": "",
                            }
                        )
                        if weekly_quota:
                            sessions_in_week += 1
                            current_start = current_start + timedelta(days=1)
                        elif freq == "weekly":
                            current_start = current_start + timedelta(days=7)
                        else:
                            current_start = current_start + timedelta(days=1)
                    else:
                        current_start = current_start + timedelta(days=1)

                if len(sessions) < count:
                    general_errors.append("Não foi possível encontrar horários suficientes para a série.")

    upcoming_appointments = list(
        Appointment.objects
        .filter(client=request.user, date__gte=today)
        .exclude(
            status__in=[
                Appointment.STATUS_COMPLETED,
                Appointment.STATUS_IN_DEBT,
                Appointment.STATUS_CANCELLED,
                Appointment.STATUS_NO_SHOW,
            ]
        )
        .select_related("service", "professional", "professional__user")
        .order_by("date", "time", "id")[:5]
    )
    status_label_fn = globals().get("_status_label")
    for a in upcoming_appointments:
        a.status_label = status_label_fn(a.status) if callable(status_label_fn) else a.status

    return render(
        request,
        "core/book_duralux.html",
        {
            "booking_mode": "serie",
            "services": services,
            "series_service_id": service_id,
            "series_start_date": start_date_str,
            "series_count": count_str,
            "series_freq": freq,
            "series_preferred_professional_id": preferred_professional_id,
            "series_sessions": sessions,
            "series_general_errors": general_errors,
            "series_professionals": professionals_qs,
            "series_weekday_options": SERIES_WEEKDAY_OPTIONS,
            "series_weekdays": selected_weekdays,
            "upcoming_appointments": upcoming_appointments,
            "back_to_appointments_url": reverse("my_appointments"),
        },
    )


def book_view(request):
    mode = (request.GET.get("mode") or request.POST.get("mode") or "").strip().lower()
    if request.path.endswith("/marcar/serie/") and not mode:
        mode = "serie"
    if mode not in {"serie"}:
        mode = "single"

    try:
        profile = request.user.client_profile
    except ClientProfile.DoesNotExist:
        next_url = "/marcar/?mode=serie" if mode == "serie" else "/marcar/"
        return redirect(f"/perfil/?next={next_url}")

    ClinicalRecord.objects.get_or_create(
        client=profile,
        defaults={"updated_by": request.user},
    )

    required_fields = ["full_name", "phone", "address_line1", "postal_code"]
    missing_basic = any(not getattr(profile, f) for f in required_fields)
    missing_location = not (profile.locality or profile.city)
    if missing_basic or missing_location:
        next_url = "/marcar/?mode=serie" if mode == "serie" else "/marcar/"
        return redirect(f"/perfil/?next={next_url}")

    if mode == "serie":
        return _book_series_client_view(request, profile)

    today = timezone.localdate()

    reschedule_id = (request.GET.get("reschedule_id") or request.POST.get("reschedule_id") or "").strip()
    reschedule_appt = None
    if reschedule_id and mode == "serie":
        reschedule_id = ""

    if reschedule_id:
        reschedule_appt = Appointment.objects.select_related(
            "client", "service", "professional", "professional__user"
        ).filter(id=reschedule_id).first()
        if not reschedule_appt or not can_modify_appointment(request.user, reschedule_appt):
            messages.error(request, "Marcação inválida para reagendar.")
            return redirect("my_appointments")
        if reschedule_appt.status in [
            Appointment.STATUS_COMPLETED,
            Appointment.STATUS_IN_DEBT,
            Appointment.STATUS_CANCELLED,
            Appointment.STATUS_NO_SHOW,
        ]:
            messages.error(request, "Não podes reagendar uma marcação concluída, em dívida, cancelada ou em falta.")
            return redirect("my_appointments")
        if reschedule_appt.client_id != request.user.id:
            return HttpResponseForbidden("Não podes reagendar esta marcação.")
        if reschedule_appt.date == today:
            messages.error(request, "Não podes reagendar no dia da consulta.")
            return redirect("my_appointments")

    # Mensagens (separadas!)
    message = ""       # erros / validações / slots
    info_message = ""  # info do profissional (dias)

    # Seleções via GET (para UI)
    selected_service_id = (request.GET.get("service_id") or "").strip()
    selected_professional_id = (request.GET.get("professional_id") or "").strip()
    selected_date = (request.GET.get("date") or "").strip()
    selected_time = (request.GET.get("time") or "").strip()

    if reschedule_appt:
        if not selected_service_id and reschedule_appt.service_id:
            selected_service_id = str(reschedule_appt.service_id)
        if not selected_professional_id and reschedule_appt.professional_id:
            selected_professional_id = str(reschedule_appt.professional_id)
        if not selected_date and reschedule_appt.date:
            selected_date = reschedule_appt.date.strftime("%Y-%m-%d")
        if not selected_time and reschedule_appt.time:
            selected_time = reschedule_appt.time.strftime("%H:%M")
    rate_status = 200
    retry_after = 0

    # Querysets base
    services = Service.objects.all().order_by("name")
    professionals_qs = Professional.objects.select_related("user").all().order_by("user__username")

    filter_date_obj = None
    no_services_for_date = False
    if selected_date:
        try:
            filter_date_obj = datetime.strptime(selected_date, "%Y-%m-%d").date()
        except ValueError:
            filter_date_obj = None

    # Data primeiro: só mostrar serviços com horários disponíveis nessa data
    if filter_date_obj:
        available_service_ids = []
        for service_option in services:
            has_any_prof_available = False
            for prof_option in professionals_qs.filter(services__id=service_option.id).distinct():
                day_slots = _get_slots(
                    prof_option,
                    filter_date_obj,
                    step_minutes=service_option.duration_minutes,
                )
                if day_slots:
                    has_any_prof_available = True
                    break
            if has_any_prof_available:
                available_service_ids.append(service_option.id)

        services = services.filter(id__in=available_service_ids)
        no_services_for_date = len(available_service_ids) == 0

    # Se o serviço selecionado não está disponível para a data atual, limpa seleção dependente
    if selected_service_id and not services.filter(id=selected_service_id).exists():
        selected_service_id = ""
        selected_professional_id = ""
        selected_time = ""

    # Se o serviço for de turma, redireciona para lista de sessões
    if selected_service_id:
        service_obj = services.filter(id=selected_service_id).first() or Service.objects.filter(id=selected_service_id).first()
        if service_obj and service_obj.service_type == "group":
            return redirect("group_sessions_list", service_id=service_obj.id)

    selected_service = services.filter(id=selected_service_id).first() if selected_service_id else None

    # Filtrar profissionais por serviço e (quando existe) pela data escolhida
    if selected_service:
        professionals_qs = professionals_qs.filter(services__id=selected_service.id).distinct()
        if filter_date_obj:
            available_prof_ids = []
            for prof_option in professionals_qs:
                day_slots = _get_slots(
                    prof_option,
                    filter_date_obj,
                    step_minutes=selected_service.duration_minutes,
                )
                if day_slots:
                    available_prof_ids.append(prof_option.id)
            professionals_qs = professionals_qs.filter(id__in=available_prof_ids)

    # Se só houver 1 profissional para data+serviço, auto-seleciona
    if selected_date and selected_service_id and not selected_professional_id:
        only_one = list(professionals_qs[:2])
        if len(only_one) == 1:
            selected_professional_id = str(only_one[0].id)
            params = {
                "date": selected_date,
                "service_id": selected_service_id,
                "professional_id": selected_professional_id,
            }
            if reschedule_id:
                params["reschedule_id"] = reschedule_id
            return redirect(f"{request.path}?{urlencode(params)}")

    # Se o profissional selecionado não pertence ao queryset filtrado -> limpa
    if selected_professional_id and not professionals_qs.filter(id=selected_professional_id).exists():
        selected_professional_id = ""
        selected_time = ""

    slots = []
    prof_days = []

    # ✅ “Extra”: se já escolheu profissional, mostrar dias (sem precisar de escolher data)
    if selected_professional_id:
        prof = professionals_qs.filter(id=selected_professional_id).first()
        if prof:
            prof_days = professional_weekdays_labels(prof)
            if prof_days:
                info_message = f"Este profissional atende: {', '.join(prof_days)}."
            else:
                info_message = "Este profissional ainda não tem horários configurados."

    # POST → criar marcação
    if request.method == "POST":
        blocked_user, retry_user = check_rate_limit(
            request,
            name="book_user_minute",
            limit=10,
            window=60,
            by_user=True,
            by_ip=False,
        )
        blocked_ip, retry_ip = check_rate_limit(
            request,
            name="book_ip_hour",
            limit=30,
            window=3600,
            by_ip=True,
        )
        if blocked_user or blocked_ip:
            message = "Demasiadas tentativas. Tenta novamente em alguns minutos."
            rate_status = 429
            retry_after = max(retry_user, retry_ip)
            if is_json_request(request):
                return rate_limited_response(request, message, retry_after)
        else:
            service_id = (request.POST.get("service_id") or "").strip()
            professional_id = (request.POST.get("professional_id") or "").strip()
            date_str = (request.POST.get("date") or "").strip()
            time_str = (request.POST.get("time") or "").strip()
            symptomatology = (request.POST.get("symptomatology") or "").strip()

            # manter seleções no render em caso de erro
            selected_service_id = service_id
            selected_professional_id = professional_id
            selected_date = date_str
            selected_time = time_str

            # refaz queryset filtrado pelo serviço/data (para dropdown não ficar vazio)
            professionals_qs = Professional.objects.select_related("user").all().order_by("user__username")
            if selected_service_id:
                professionals_qs = professionals_qs.filter(services__id=selected_service_id).distinct()
                if selected_date:
                    try:
                        filter_date_obj = datetime.strptime(selected_date, "%Y-%m-%d").date()
                    except ValueError:
                        filter_date_obj = None
                    if filter_date_obj:
                        selected_service_obj = Service.objects.filter(id=selected_service_id).first()
                        if selected_service_obj:
                            available_prof_ids = []
                            for prof_option in professionals_qs:
                                day_slots = _get_slots(
                                    prof_option,
                                    filter_date_obj,
                                    step_minutes=selected_service_obj.duration_minutes,
                                )
                                if day_slots:
                                    available_prof_ids.append(prof_option.id)
                            professionals_qs = professionals_qs.filter(id__in=available_prof_ids)

            if not (service_id and professional_id and date_str and time_str):
                message = "Dados incompletos."
            else:
                # valida relação serviço-profissional
                if not Professional.objects.filter(id=professional_id, services__id=service_id).exists():
                    message = "Profissional inválido para este serviço."
                else:
                    service = get_object_or_404(Service, id=service_id)
                    if service.service_type == "group":
                        return redirect("group_sessions_list", service_id=service.id)
                    prof = get_object_or_404(Professional, id=professional_id)

                    prof_days = professional_weekdays_labels(prof)
                    if prof_days:
                        info_message = f"Este profissional atende: {', '.join(prof_days)}."
                    else:
                        info_message = "Este profissional ainda não tem horários configurados."

                date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
                time_obj = datetime.strptime(time_str, "%H:%M").time()
                is_same_slot = bool(
                    reschedule_appt
                    and reschedule_appt.professional_id == prof.id
                    and reschedule_appt.date == date_obj
                    and reschedule_appt.time == time_obj
                )

                if date_obj < today:
                    message = "Não podes marcar consultas no passado."
                    slots = []
                elif date_obj == today and time_obj <= now_t:
                    message = "Este horário já passou."
                    slots = _get_slots(prof, date_obj, service=service)
                elif not is_same_slot and _is_slot_blocked(prof, date_obj, time_obj):
                    message = "Este horário está indisponível."
                    slots = _get_slots(prof, date_obj, service=service)
                elif not professional_works_on_date(prof, date_obj):
                    message = f"Este profissional não atende nesse dia. Atende: {', '.join(prof_days) or '—'}."
                    slots = []
                else:
                    slots_now = _get_slots(prof, date_obj, service=service)
                    if time_str not in slots_now and not is_same_slot:
                        message = "Esse horário já não está disponível. Atualiza a página."
                        slots = slots_now
                    else:
                        reschedule_old_date = None
                        reschedule_old_time = None
                        try:
                            with transaction.atomic():
                                client_profile = getattr(request.user, "client_profile", None)
                                pricing = compute_pricing(service, client_profile)
                                if reschedule_appt:
                                    appt = reschedule_appt
                                    old_date, old_time = appt.date, appt.time
                                    old_status = appt.status
                                    old_service = appt.service
                                    old_prof = appt.professional
                                    reschedule_old_date = old_date
                                    reschedule_old_time = old_time
                                    appt.service = service
                                    appt.professional = prof
                                    appt.date = date_obj
                                    appt.time = time_obj
                                    appt.symptomatology = symptomatology
                                    appt.status = Appointment.STATUS_PENDING
                                    appt.base_price = pricing["base_price_applied"]
                                    appt.partner = pricing["partner"]
                                    appt.partner_price = pricing["partner_price_applied"]
                                    appt.discount_type = pricing["discount_type"]
                                    appt.discount_value = pricing["discount_value"]
                                    appt.final_price = pricing["final_price"]
                                    appt.session_index = pricing["session_index"]
                                    appt.pricing_tier = pricing["pricing_tier"]
                                    appt.base_price_applied = pricing["base_price_applied"]
                                    appt.partner_price_applied = pricing["partner_price_applied"]
                                    appt.discount_applied = pricing["discount_applied"]
                                    appt.save()

                                    note_parts = []
                                    if old_service and old_service.id != service.id:
                                        note_parts.append(f"Serviço: {old_service.name} → {service.name}")
                                    if old_prof and old_prof.id != prof.id:
                                        note_parts.append(
                                            f"Profissional: {old_prof.user.get_full_name() or old_prof.user.username} → {prof.user.get_full_name() or prof.user.username}"
                                        )
                                    log_appt(
                                        AppointmentLog.ACTION_RESCHEDULED,
                                        appt,
                                        request.user,
                                        old_date=old_date,
                                        old_time=old_time,
                                        new_date=appt.date,
                                        new_time=appt.time,
                                        old_status=old_status,
                                        new_status=appt.status,
                                        note="; ".join(note_parts) if note_parts else "",
                                        request=request,
                                    )
                                else:
                                    appt = Appointment.objects.create(
                                        client=request.user,
                                        professional=prof,
                                        service=service,
                                        date=date_obj,
                                        time=time_obj,
                                        symptomatology=symptomatology,
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
                            if reschedule_appt and settings_obj.notify_clinic_on_client_reschedule and clinic_to:
                                send_templated_email(
                                    clinic_to,
                                    f"Marcação em confirmação — {service.name} — {reschedule_old_date}→{appt.date}",
                                    "emails/clinic_appointment_event.html",
                                    "emails/clinic_appointment_event.txt",
                                    {
                                        "event_type": "pending_confirmation",
                                        "event_title": "Marcação em confirmação (reagendada)",
                                        "client_name": request.user.get_full_name() or request.user.username,
                                        "client_phone": getattr(getattr(request.user, "client_profile", None), "phone", ""),
                                        "service_name": service.name,
                                        "professional_name": prof.user.get_full_name() or prof.user.username,
                                        "old_date": reschedule_old_date,
                                        "old_time": reschedule_old_time,
                                        "new_date": appt.date,
                                        "new_time": appt.time,
                                        "cancelled_at": "",
                                        "actor": "Cliente",
                                        "admin_url": request.build_absolute_uri("/prof/calendario/"),
                                    },
                                    event="reschedule_client",
                                )
                            elif settings_obj.notify_clinic_on_new_booking and clinic_to:
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
                                        "professional_name": prof.user.get_full_name() or prof.user.username,
                                        "old_date": "",
                                        "old_time": "",
                                        "new_date": appt.date,
                                        "new_time": appt.time,
                                        "cancelled_at": "",
                                        "actor": "Cliente",
                                        "admin_url": request.build_absolute_uri(f"/admin/core/appointment/{appt.id}/change/"),
                                    },
                                    event="new_booking",
                                )

                            if settings_obj.notify_professional_on_new_booking:
                                prof_email = getattr(prof.user, "email", "")
                                if prof_email:
                                    if reschedule_appt:
                                        send_templated_email(
                                            prof_email,
                                            f"Marcação em confirmação — {service.name} — {appt.date} {appt.time}",
                                            "emails/clinic_appointment_event.html",
                                            "emails/clinic_appointment_event.txt",
                                            {
                                                "event_type": "pending_confirmation",
                                                "event_title": "Marcação em confirmação (reagendada)",
                                                "client_name": request.user.get_full_name() or request.user.username,
                                                "client_phone": getattr(getattr(request.user, "client_profile", None), "phone", ""),
                                                "service_name": service.name,
                                                "professional_name": prof.user.get_full_name() or prof.user.username,
                                                "old_date": reschedule_old_date,
                                                "old_time": reschedule_old_time,
                                                "new_date": appt.date,
                                                "new_time": appt.time,
                                                "cancelled_at": "",
                                                "actor": "Cliente",
                                                "admin_url": "",
                                            },
                                            event="reschedule_client",
                                        )
                                    else:
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
                                                "professional_name": prof.user.get_full_name() or prof.user.username,
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
                                    log_email_skip("new_booking", "Nova marcação", "Profissional sem email", "")

                            client_email = request.user.email or ""
                            if client_email:
                                send_templated_email(
                                    client_email,
                                    (
                                        f"Pedido de reagendamento recebido — {service.name} em {appt.date} {appt.time}"
                                        if reschedule_appt
                                        else f"Pedido de marcação recebido — {service.name} em {appt.date} {appt.time}"
                                    ),
                                    "emails/appointment_pending_confirmation.html",
                                    "emails/appointment_pending_confirmation.txt",
                                    {
                                        "client_name": request.user.get_full_name() or request.user.username,
                                        "service_name": service.name,
                                        "professional_name": prof.user.get_full_name() or prof.user.username,
                                        "date": appt.date,
                                        "time": appt.time,
                                        "symptomatology": symptomatology,
                                        "is_reschedule": bool(reschedule_appt),
                                        "manage_url": request.build_absolute_uri(reverse("my_appointments")),
                                    },
                                    event="reschedule_client" if reschedule_appt else "new_booking",
                                )
                            else:
                                log_email_skip(
                                    "reschedule_client" if reschedule_appt else "new_booking",
                                    "Pedido de marcação em confirmação",
                                    "Cliente sem email",
                                    "",
                                )

                            if reschedule_appt:
                                messages.success(request, "Pedido de reagendamento enviado para confirmação.")
                            else:
                                messages.success(request, "Pedido de marcação enviado para confirmação.")
                            return redirect("my_appointments")

                        except IntegrityError:
                            message = "Esse horário acabou de ser reservado. Escolhe outro."
                            slots = _get_slots(prof, date_obj, service=service)

    # GET → só calcular slots quando tiver serviço + profissional + data
    if selected_service_id and selected_professional_id and selected_date and request.method != "POST":
        blocked_slots, retry_slots = check_rate_limit(
            request,
            name="slots_ip_minute",
            limit=30,
            window=60,
            by_ip=True,
        )
        if blocked_slots:
            message = "Demasiadas tentativas. Tenta novamente em alguns minutos."
            rate_status = 429
            retry_after = retry_slots
            if is_json_request(request):
                return rate_limited_response(request, message, retry_after)
        else:
            service = get_object_or_404(Service, id=selected_service_id)
            prof = get_object_or_404(Professional, id=selected_professional_id)

            try:
                date_obj = datetime.strptime(selected_date, "%Y-%m-%d").date()
            except ValueError:
                message = "Formato de data inválido."
                date_obj = None

            if date_obj:
                if date_obj < today:
                    message = "Não podes marcar consultas no passado."
                    slots = []
                elif date_obj == today:
                    slots = _get_slots(prof, date_obj, service=service)
                    if not slots:
                        message = "Não há horários disponíveis para este dia."
                elif not professional_works_on_date(prof, date_obj):
                    prof_days = professional_weekdays_labels(prof)
                    message = f"Este profissional não atende nesse dia. Atende: {', '.join(prof_days) or '—'}."
                    slots = []
                else:
                    slots = _get_slots(prof, date_obj, service=service)
                    if not slots:
                        message = "Não há horários disponíveis para este dia."
                if reschedule_appt and reschedule_appt.time and reschedule_appt.date.strftime("%Y-%m-%d") == selected_date and str(reschedule_appt.professional_id) == str(selected_professional_id):
                    res_time = reschedule_appt.time.strftime("%H:%M")
                    if res_time not in slots:
                        slots = sorted(slots + [res_time])

    upcoming_appointments = list(
        Appointment.objects
        .filter(client=request.user, date__gte=today)
        .exclude(
            status__in=[
                Appointment.STATUS_COMPLETED,
                Appointment.STATUS_IN_DEBT,
                Appointment.STATUS_CANCELLED,
                Appointment.STATUS_NO_SHOW,
            ]
        )
        .select_related("service", "professional", "professional__user")
        .order_by("date", "time", "id")[:5]
    )

    status_label_fn = globals().get("_status_label")
    for a in upcoming_appointments:
        a.status_label = status_label_fn(a.status) if callable(status_label_fn) else a.status

    price_preview = None
    if selected_service_id:
        service_for_price = Service.objects.filter(id=selected_service_id).first()
        if service_for_price:
            client_profile = getattr(request.user, "client_profile", None)
            price_preview = compute_pricing(service_for_price, client_profile)

    response = render(
        request,
        "core/book_duralux.html",
        {
            "booking_mode": "single",
            "professionals": professionals_qs,
            "services": services,
            "selected_service_id": selected_service_id,
            "selected_professional_id": selected_professional_id,
            "selected_date": selected_date,
            "selected_time": selected_time,
            "slots": slots,
            "no_services_for_date": no_services_for_date,
            "message": message,
            "info_message": info_message,
            "upcoming_appointments": upcoming_appointments,
            "today": today,
            "prof_days": prof_days,
            "price_preview": price_preview,
            "back_to_appointments_url": reverse("my_appointments"),
            "reschedule_id": reschedule_id,
            "reschedule_appointment": reschedule_appt,
        },
        status=rate_status,
    )
    if rate_status == 429 and retry_after:
        response["Retry-After"] = str(retry_after)
    return response


def slots_api_view(request):
    service_id = (request.GET.get("service_id") or "").strip()
    professional_id = (request.GET.get("professional_id") or "").strip()
    date_str = (request.GET.get("date") or "").strip()
    reschedule_id = (request.GET.get("reschedule_id") or "").strip()

    if not (service_id and professional_id and date_str):
        return JsonResponse({"ok": False, "slots": [], "message": "Dados incompletos."})

    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        return JsonResponse({"ok": False, "slots": [], "message": "Data inválida."})

    today = timezone.localdate()
    if date_obj < today:
        return JsonResponse({"ok": False, "slots": [], "message": "Data no passado."})
    if is_portuguese_holiday(date_obj):
        return JsonResponse({"ok": False, "slots": [], "message": "Não é possível marcar em feriado nacional."})

    if not Professional.objects.filter(id=professional_id, services__id=service_id).exists():
        return JsonResponse({"ok": False, "slots": [], "message": "Profissional inválido para este serviço."})

    service = Service.objects.filter(id=service_id).first()
    prof = Professional.objects.filter(id=professional_id).first()
    if not service or not prof:
        return JsonResponse({"ok": False, "slots": [], "message": "Dados inválidos."})
    if service.service_type == "group":
        return JsonResponse({"ok": False, "slots": [], "message": "Serviço de turma não usa horários individuais."})

    reschedule_appt = None
    if reschedule_id:
        reschedule_appt = Appointment.objects.select_related("service", "professional", "client").filter(id=reschedule_id).first()
        if not reschedule_appt:
            return JsonResponse({"ok": False, "slots": [], "message": "Marcação inválida para reagendar."})
        if not can_modify_appointment(request.user, reschedule_appt):
            return JsonResponse({"ok": False, "slots": [], "message": "Não tens permissão para reagendar esta marcação."}, status=403)
        if reschedule_appt.status in [
            Appointment.STATUS_COMPLETED,
            Appointment.STATUS_IN_DEBT,
            Appointment.STATUS_CANCELLED,
            Appointment.STATUS_NO_SHOW,
        ]:
            return JsonResponse({"ok": False, "slots": [], "message": "Não podes reagendar esta marcação."})
        if str(reschedule_appt.service_id) != service_id:
            return JsonResponse({"ok": False, "slots": [], "message": "Serviço inválido para este reagendamento."})

    is_current_reschedule_slot_context = bool(
        reschedule_appt
        and reschedule_appt.professional_id == prof.id
        and reschedule_appt.date == date_obj
        and reschedule_appt.time
    )

    works_on_date = professional_works_on_date(prof, date_obj)
    if not works_on_date and not is_current_reschedule_slot_context:
        return JsonResponse({"ok": False, "slots": [], "message": "Este profissional não atende nesse dia."})

    slots = _get_slots(prof, date_obj, service=service) if works_on_date else []
    if is_current_reschedule_slot_context:
        reschedule_time = reschedule_appt.time.strftime("%H:%M")
        if reschedule_time not in slots:
            slots = sorted(slots + [reschedule_time])

    if not slots:
        return JsonResponse({"ok": False, "slots": [], "message": "Sem horários disponíveis."})

    return JsonResponse({"ok": True, "slots": slots, "message": ""})


def bulk_book_view(request):
    is_staff_flow = can_view_all_calendar(request.user) or Professional.objects.filter(user=request.user).exists()
    clients_qs = None
    clients_page = None
    client_query = (request.GET.get("q") or "").strip()
    page_number = (request.GET.get("page") or "1").strip()
    selected_client_id = (
        request.GET.get("client_profile_id")
        or request.POST.get("client_profile_id")
        or request.GET.get("client_id")
        or request.POST.get("client_id")
        or ""
    ).strip()
    base_params = request.GET.copy()
    if "client_id" in base_params:
        base_params.pop("client_id")
    if "client_profile_id" in base_params:
        base_params.pop("client_profile_id")
    if "page" in base_params:
        base_params.pop("page")
    base_params_str = base_params.urlencode()
    base_has_params = bool(base_params_str)
    selected_client = None

    if is_staff_flow:
        clients_qs = ClientProfile.objects.select_related("user").order_by("full_name")
        if client_query:
            clients_qs = apply_terms_filter(
                clients_qs,
                client_query,
                [
                    "full_name__icontains",
                    "user__username__icontains",
                    "phone__icontains",
                    "nif__icontains",
                    "user__first_name__icontains",
                    "user__last_name__icontains",
                    "user__email__icontains",
                ],
            )
        paginator = Paginator(clients_qs, 5)
        clients_page = paginator.get_page(page_number)
        profile = None
        if selected_client_id:
            profile = ClientProfile.objects.filter(id=selected_client_id).first()
            if not profile and selected_client_id.isdigit():
                profile = ClientProfile.objects.filter(user_id=selected_client_id).first()
            if profile:
                selected_client = profile
                selected_client_id = str(profile.id)
    else:
        try:
            profile = request.user.client_profile
        except ClientProfile.DoesNotExist:
            return redirect("/perfil/?next=/marcar/serie/")

        required_fields = ["full_name", "phone", "address_line1", "postal_code"]
        missing_basic = any(not getattr(profile, f) for f in required_fields)
        missing_location = not (profile.locality or profile.city)
        if missing_basic or missing_location:
            return redirect("/perfil/?next=/marcar/serie/")

    services = Service.objects.all().order_by("name")
    service_id = (request.GET.get("service_id") or request.POST.get("service_id") or "").strip()
    start_date_str = (request.GET.get("start_date") or request.POST.get("start_date") or "").strip()
    count_str = (request.GET.get("count") or request.POST.get("count") or "").strip()
    freq = normalize_series_frequency(request.GET.get("freq") or request.POST.get("freq") or "")
    preferred_professional_id = (request.GET.get("professional_id") or request.POST.get("professional_id") or "").strip()

    professionals_qs = Professional.objects.select_related("user").all().order_by("user__username")
    if service_id:
        professionals_qs = professionals_qs.filter(services__id=service_id).distinct()

    general_errors = []
    line_errors = {}
    sessions = []
    max_count = 20

    if request.method == "POST" and (request.POST.get("action") or "").strip() == "confirm":
        if is_staff_flow and not selected_client_id:
            general_errors.append("Seleciona o cliente.")

        service_id = (request.POST.get("service_id") or "").strip()
        dates = request.POST.getlist("session_date")
        prof_ids = request.POST.getlist("session_professional_id")
        times = request.POST.getlist("session_time")
        symptomatology_global = (request.POST.get("symptomatology_global") or "").strip()

        if not service_id:
            general_errors.append("Seleciona o serviço.")
        if not dates or len(dates) != len(prof_ids) or len(dates) != len(times):
            general_errors.append("Dados das sessões inválidos.")

        if service_id and not Service.objects.filter(id=service_id).exists():
            general_errors.append("Serviço inválido.")

        service = Service.objects.filter(id=service_id).first()
        if service and service.service_type == "group":
            general_errors.append("Este serviço é de turma. Usa a gestão de turmas.")
        today = timezone.localdate()
        now_t = timezone.localtime().time()

        for idx, (d, p, t) in enumerate(zip(dates, prof_ids, times)):
            row_error = ""
            try:
                date_obj = datetime.strptime(d, "%Y-%m-%d").date()
            except Exception:
                row_error = "Data inválida."
                line_errors[idx] = row_error
                continue

            if not service:
                row_error = "Serviço inválido."
            elif date_obj < today:
                row_error = "Data no passado."
            elif not p:
                row_error = "Escolhe o profissional."
            elif not t:
                row_error = "Escolhe o horário."
            elif not Professional.objects.filter(id=p, services__id=service_id).exists():
                row_error = "Profissional inválido para este serviço."
            else:
                prof = Professional.objects.filter(id=p).first()
                time_obj = datetime.strptime(t, "%H:%M").time()
                if date_obj == today and time_obj <= now_t:
                    row_error = "Este horário já passou."
                elif not professional_works_on_date(prof, date_obj):
                    row_error = "Profissional não atende nesse dia."
                elif _is_slot_blocked(prof, date_obj, time_obj):
                    row_error = "Horário indisponível."
                else:
                    slots_now = _get_slots(prof, date_obj, service=service)
                    if t not in slots_now:
                        row_error = "Horário já não disponível."

            if row_error:
                line_errors[idx] = row_error

        if general_errors or line_errors:
            for idx, d in enumerate(dates):
                date_display = d
                try:
                    date_display = datetime.strptime(d, "%Y-%m-%d").date()
                except Exception:
                    pass
                sessions.append(
                    {
                        "date_display": date_display,
                        "date_value": d,
                        "professional_id": prof_ids[idx] if idx < len(prof_ids) else "",
                        "time": times[idx] if idx < len(times) else "",
                        "error": line_errors.get(idx, ""),
                    }
                )
            return render(
                request,
                "core/bulk_book.html",
                {
                    "services": services,
                    "service_id": service_id,
                    "professionals": professionals_qs,
                    "start_date": start_date_str,
                    "count": count_str,
                    "freq": freq,
                    "preferred_professional_id": preferred_professional_id,
                    "sessions": sessions,
                    "general_errors": general_errors,
                },
            )

        if is_staff_flow and not profile:
            general_errors.append("Cliente inválido.")

        if general_errors:
            for idx, d in enumerate(dates):
                date_display = d
                try:
                    date_display = datetime.strptime(d, "%Y-%m-%d").date()
                except Exception:
                    pass
                sessions.append(
                    {
                        "date_display": date_display,
                        "date_value": d,
                        "professional_id": prof_ids[idx] if idx < len(prof_ids) else "",
                        "time": times[idx] if idx < len(times) else "",
                        "error": line_errors.get(idx, ""),
                    }
                )
            return render(
                request,
                "core/bulk_book.html",
                {
                    "services": services,
                    "service_id": service_id,
                    "professionals": professionals_qs,
                    "start_date": start_date_str,
                    "count": count_str,
                    "freq": freq,
                    "preferred_professional_id": preferred_professional_id,
                    "sessions": sessions,
                    "general_errors": general_errors,
                    "clients": clients_page,
                    "selected_client_id": selected_client_id,
                    "is_staff_flow": is_staff_flow,
                    "client_query": client_query,
                    "base_params": base_params_str,
                    "base_has_params": base_has_params,
                    "selected_client": selected_client,
                },
            )

        series_id = uuid4()
        created = 0
        client_user = profile.user if profile else request.user
        client_profile = profile or getattr(client_user, "client_profile", None)
        pricing = compute_pricing(service, client_profile)
        initial_status = Appointment.STATUS_SCHEDULED if is_staff_flow else Appointment.STATUS_PENDING
        with transaction.atomic():
            for d, p, t in zip(dates, prof_ids, times):
                date_obj = datetime.strptime(d, "%Y-%m-%d").date()
                time_obj = datetime.strptime(t, "%H:%M").time()
                prof = Professional.objects.get(id=p)
                Appointment.objects.create(
                    client=client_user,
                    professional=prof,
                    service=service,
                    date=date_obj,
                    time=time_obj,
                    symptomatology=symptomatology_global,
                    series_id=series_id,
                    status=initial_status,
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
                created += 1

        if initial_status == Appointment.STATUS_SCHEDULED:
            messages.success(request, f"Foram criadas {created} marcações em série já confirmadas.")
        else:
            messages.success(request, f"Foram enviadas {created} marcações para confirmação.")
        return redirect("my_appointments")

    if service_id and start_date_str and count_str:
        try:
            count = max(1, int(count_str))
        except Exception:
            count = 0
        if count > max_count:
            count = max_count
            general_errors.append("O máximo por série é 20 sessões.")

        try:
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        except Exception:
            start_date = None
            general_errors.append("Data inicial inválida.")

        service = Service.objects.filter(id=service_id).first()
        if service and service.service_type == "group":
            general_errors.append("Este serviço é de turma. Usa a gestão de turmas.")

        prof_for_series = None
        if preferred_professional_id:
            prof_for_series = Professional.objects.filter(
                id=preferred_professional_id, services__id=service_id
            ).first()
        if count and start_date and not general_errors:
            today = timezone.localdate()
            current_start = start_date if start_date >= today else today
            # avançar até ao próximo dia válido
            while True:
                if freq == "weekdays" and current_start.weekday() >= 5:
                    current_start = current_start + timedelta(days=1)
                    continue
                if prof_for_series and not professional_works_on_date(prof_for_series, current_start):
                    current_start = current_start + timedelta(days=1)
                    continue
                break
            dates = _build_series_dates(current_start, count, freq, prof=prof_for_series)
            for d in dates:
                sessions.append(
                    {
                        "date_display": d,
                        "date_value": d.strftime("%Y-%m-%d"),
                        "professional_id": preferred_professional_id,
                        "time": "",
                        "error": "",
                    }
                )

    return render(
        request,
        "core/bulk_book.html",
        {
            "services": services,
            "service_id": service_id,
            "professionals": professionals_qs,
            "start_date": start_date_str,
            "count": count_str,
            "freq": freq,
            "preferred_professional_id": preferred_professional_id,
            "sessions": sessions,
            "general_errors": general_errors,
            "clients": clients_page,
            "selected_client_id": selected_client_id,
            "is_staff_flow": is_staff_flow,
            "client_query": client_query,
            "base_params": base_params_str,
            "base_has_params": base_has_params,
            "selected_client": selected_client,
        },
    )


def _professional_book_series_view(
    request,
    *,
    client_profile,
    client_user,
    prof,
    can_book_any,
    back_to_calendar_url,
    single_mode_url,
    series_mode_url,
):
    today = timezone.localdate()
    now_t = timezone.localtime().time()
    week = (request.GET.get("week") or request.POST.get("week") or "").strip()
    status = (request.GET.get("status") or request.POST.get("status") or "").strip()
    q = (request.GET.get("q") or request.POST.get("q") or "").strip()

    services = Service.objects.all().order_by("name") if can_book_any else prof.services.all().order_by("name")
    service_id = (request.GET.get("service_id") or request.POST.get("service_id") or "").strip()
    start_date_str = (request.GET.get("start_date") or request.POST.get("start_date") or "").strip()
    count_str = (request.GET.get("count") or request.POST.get("count") or "").strip()
    freq = normalize_series_frequency(request.GET.get("freq") or request.POST.get("freq") or "")
    preferred_professional_id = (request.GET.get("professional_id") or request.POST.get("professional_id") or "").strip()
    selected_weekdays = _parse_series_weekdays(request.POST if request.method == "POST" else request.GET)
    send_client_email_raw = (
        request.POST.get("send_client_email")
        if request.method == "POST"
        else request.GET.get("send_client_email")
    )
    send_client_email_on_create = True
    if send_client_email_raw is not None:
        send_client_email_on_create = str(send_client_email_raw).strip().lower() in {"1", "true", "on", "yes"}

    professionals_qs = Professional.objects.select_related("user").order_by("user__username")
    if not can_book_any:
        professionals_qs = professionals_qs.filter(id=prof.id)
        preferred_professional_id = str(prof.id)
    elif service_id:
        professionals_qs = professionals_qs.filter(services__id=service_id).distinct()
        if preferred_professional_id and not professionals_qs.filter(id=preferred_professional_id).exists():
            preferred_professional_id = ""
        if not preferred_professional_id:
            only_one = list(professionals_qs[:2])
            if len(only_one) == 1:
                preferred_professional_id = str(only_one[0].id)
    else:
        professionals_qs = professionals_qs.none()
        preferred_professional_id = ""

    sessions = []
    general_errors = []
    line_errors = {}
    max_count = 20

    if request.method == "POST" and (request.POST.get("action") or "").strip() == "confirm":
        dates = request.POST.getlist("session_date")
        prof_ids = request.POST.getlist("session_professional_id")
        times = request.POST.getlist("session_time")
        symptomatology_global = (request.POST.get("symptomatology_global") or "").strip()

        if not service_id:
            general_errors.append("Seleciona o serviço.")
        if not dates or len(dates) != len(prof_ids) or len(dates) != len(times):
            general_errors.append("Dados das sessões inválidos.")

        if service_id and not Service.objects.filter(id=service_id).exists():
            general_errors.append("Serviço inválido.")

        service = Service.objects.filter(id=service_id).first()
        if service and service.service_type == "group":
            general_errors.append("Este serviço é de turma. Usa a gestão de turmas.")
        weekdays_error = _validate_series_weekdays(freq, selected_weekdays)
        if weekdays_error:
            general_errors.append(weekdays_error)

        for idx, (d, p, t) in enumerate(zip(dates, prof_ids, times)):
            row_error = ""
            try:
                date_obj = datetime.strptime(d, "%Y-%m-%d").date()
            except Exception:
                row_error = "Data inválida."
                line_errors[idx] = row_error
                continue

            if not can_book_any:
                p = str(prof.id)

            if not service:
                row_error = "Serviço inválido."
            elif date_obj < today:
                row_error = "Data no passado."
            elif not p:
                row_error = "Escolhe o profissional."
            elif not t:
                row_error = "Escolhe o horário."
            elif not Professional.objects.filter(id=p, services__id=service_id).exists():
                row_error = "Profissional inválido para este serviço."
            else:
                prof_row = Professional.objects.filter(id=p).first()
                if not can_book_any and prof_row and prof_row.id != prof.id:
                    row_error = "Profissional inválido para este utilizador."
                else:
                    time_obj = datetime.strptime(t, "%H:%M").time()
                    if date_obj == today and time_obj <= now_t:
                        row_error = "Este horário já passou."
                    elif not professional_works_on_date(prof_row, date_obj):
                        row_error = "Profissional não atende nesse dia."
                    elif _is_slot_blocked(prof_row, date_obj, time_obj):
                        row_error = "Horário indisponível."
                    else:
                        slots_now = _get_slots(prof_row, date_obj, service=service)
                        if t not in slots_now:
                            row_error = "Horário já não disponível."

            if row_error:
                line_errors[idx] = row_error

        if general_errors or line_errors:
            for idx, d in enumerate(dates):
                date_display = d
                try:
                    date_display = datetime.strptime(d, "%Y-%m-%d").date()
                except Exception:
                    pass
                sessions.append(
                    {
                        "date_display": date_display,
                        "date_value": d,
                        "professional_id": prof_ids[idx] if idx < len(prof_ids) else "",
                        "time": times[idx] if idx < len(times) else "",
                        "error": line_errors.get(idx, ""),
                    }
                )
        else:
            series_id = uuid4()
            created = 0
            created_appointments = []
            pricing = compute_pricing(service, client_profile)
            with transaction.atomic():
                for d, p, t in zip(dates, prof_ids, times):
                    date_obj = datetime.strptime(d, "%Y-%m-%d").date()
                    time_obj = datetime.strptime(t, "%H:%M").time()
                    prof_row = prof if not can_book_any else Professional.objects.get(id=p)
                    appt = Appointment.objects.create(
                        client=client_user,
                        professional=prof_row,
                        service=service,
                        date=date_obj,
                        time=time_obj,
                        symptomatology=symptomatology_global,
                        series_id=series_id,
                        status=Appointment.STATUS_SCHEDULED,
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
                    created_appointments.append(appt)
                    created += 1

            if send_client_email_on_create:
                settings_obj = clinic_settings()
                if settings_obj.notify_client_on_new_booking:
                    client_email = client_user.email or ""
                    if client_email:
                        for appt in created_appointments:
                            send_templated_email(
                                client_email,
                                f"Marcação confirmada — {service.name} em {appt.date} {appt.time}",
                                "emails/appointment_confirmed.html",
                                "emails/appointment_confirmed.txt",
                                {
                                    "client_name": client_user.get_full_name() or client_user.username,
                                    "service_name": service.name,
                                    "professional_name": appt.professional.user.get_full_name() or appt.professional.user.username,
                                    "date": appt.date,
                                    "time": appt.time,
                                    "symptomatology": symptomatology_global,
                                    "manage_url": request.build_absolute_uri(reverse("my_appointments")),
                                },
                                event="new_booking",
                            )
                    else:
                        log_email_skip("new_booking", "Marcação confirmada", "Cliente sem email", "")

            messages.success(request, f"Foram criadas {created} marcações em série já confirmadas.")
            return redirect(back_to_calendar_url)

    if service_id and start_date_str and count_str:
        try:
            count = max(1, int(count_str))
        except Exception:
            count = 0
        if count > max_count:
            count = max_count
            general_errors.append("O máximo por série é 20 sessões.")

        try:
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        except Exception:
            start_date = None
            general_errors.append("Data inicial inválida.")

        service = Service.objects.filter(id=service_id).first()
        if service and service.service_type == "group":
            general_errors.append("Este serviço é de turma. Usa a gestão de turmas.")
        weekdays_error = _validate_series_weekdays(freq, selected_weekdays)
        if weekdays_error:
            general_errors.append(weekdays_error)

        prof_for_series = None
        if preferred_professional_id:
            prof_for_series = Professional.objects.filter(
                id=preferred_professional_id, services__id=service_id
            ).first()

        prof_candidates = list(professionals_qs)
        if count and start_date and not general_errors:
            if not prof_for_series and not prof_candidates:
                general_errors.append("Sem profissionais disponíveis para este serviço.")
            else:
                current_start = start_date if start_date >= today else today
                weekly_quota = series_frequency_weekly_quota(freq)
                current_week_start = None
                sessions_in_week = 0
                attempts = 0
                max_attempts = max(30, count * 30)
                while len(sessions) < count and attempts < max_attempts:
                    attempts += 1

                    if weekly_quota:
                        week_start = current_start - timedelta(days=current_start.weekday())
                        if current_week_start != week_start:
                            current_week_start = week_start
                            sessions_in_week = 0
                        if sessions_in_week >= weekly_quota:
                            current_start = current_week_start + timedelta(days=7)
                            continue

                    if not _series_weekday_matches(current_start, freq, selected_weekdays):
                        current_start = current_start + timedelta(days=1)
                        continue

                    prof_for_row = None
                    slot_time = ""

                    if prof_for_series:
                        if professional_works_on_date(prof_for_series, current_start):
                            slots_now = _get_slots(prof_for_series, current_start, service=service)
                            if slots_now:
                                prof_for_row = prof_for_series
                                slot_time = slots_now[0]
                    else:
                        for p in prof_candidates:
                            if not professional_works_on_date(p, current_start):
                                continue
                            slots_now = _get_slots(p, current_start, service=service)
                            if slots_now:
                                prof_for_row = p
                                slot_time = slots_now[0]
                                break

                    if prof_for_row and slot_time:
                        sessions.append(
                            {
                                "date_display": current_start,
                                "date_value": current_start.strftime("%Y-%m-%d"),
                                "professional_id": str(prof_for_row.id),
                                "time": slot_time,
                                "error": "",
                            }
                        )
                        if weekly_quota:
                            sessions_in_week += 1
                            current_start = current_start + timedelta(days=1)
                        elif freq == "weekly":
                            current_start = current_start + timedelta(days=7)
                        else:
                            current_start = current_start + timedelta(days=1)
                    else:
                        current_start = current_start + timedelta(days=1)

                if len(sessions) < count:
                    general_errors.append("Não foi possível encontrar horários suficientes para a série.")
        elif count and start_date and not service:
            general_errors.append("Serviço inválido.")

    return render(
        request,
        "core/professional_book.html",
        {
            "booking_mode": "serie",
            "client_profile": client_profile,
            "client_id": client_user.id,
            "services": services,
            "series_service_id": service_id,
            "series_start_date": start_date_str,
            "series_count": count_str,
            "series_freq": freq,
            "series_preferred_professional_id": preferred_professional_id,
            "series_sessions": sessions,
            "series_general_errors": general_errors,
            "series_professionals": professionals_qs,
            "series_weekday_options": SERIES_WEEKDAY_OPTIONS,
            "series_weekdays": selected_weekdays,
            "is_admin": can_book_any,
            "single_mode_url": single_mode_url,
            "series_mode_url": series_mode_url,
            "back_to_calendar_url": back_to_calendar_url,
            "series_fixed_professional_name": prof.user.get_full_name() or prof.user.username if prof else "",
            "send_client_email_on_create": send_client_email_on_create,
            "week": week,
            "status": status,
            "q": q,
        },
    )


def professional_book_view(request):
    """
    Profissional cria marcação PARA um cliente.
    Fluxo:
    - entra por /prof/marcar/?client_id=ID
    - profissional é fixo (o user logado)
    """
    mode = (request.GET.get("mode") or request.POST.get("mode") or "").strip().lower()
    if mode not in {"serie"}:
        mode = "single"

    # só profissional ou admin
    is_prof = Professional.objects.filter(user=request.user).exists()
    is_admin = can_view_all_calendar(request.user)
    can_book_any = can_book_for_any_professional(request.user)
    if not (is_admin or is_prof):
        return HttpResponseForbidden("Acesso restrito a profissionais.")

    prof = Professional.objects.filter(user=request.user).first()
    if not prof and not is_admin:
        return HttpResponseForbidden("Profissional não encontrado.")

    client_profile_id = request.GET.get("client_profile_id") or request.POST.get("client_profile_id")
    client_id = request.GET.get("client_id") or request.POST.get("client_id")
    def _build_back_to_calendar_url():
        keys = ["week", "service_id", "professional_id", "status", "q"]
        params = {}
        for key in keys:
            value = (request.GET.get(key) or request.POST.get(key) or "").strip()
            if value:
                params[key] = value
        base_url = reverse("professional_calendar")
        if params:
            return f"{base_url}?{urlencode(params)}"
        return base_url

    back_to_calendar_url = _build_back_to_calendar_url()

    if not client_profile_id and not client_id:
        params = {}
        for key in [
            "date",
            "time",
            "service_id",
            "professional_id",
            "week",
            "occupied_professional_id",
            "occupied_date",
            "occupied_time",
            "status",
            "q",
        ]:
            value = (request.GET.get(key) or "").strip()
            if value:
                params[key] = value
        if params:
            return redirect(f"{reverse('professional_clients')}?{urlencode(params)}")
        return redirect("professional_clients")

    # Prioridade: client_profile_id (ID do cliente), fallback antigo por client_id (user)
    if client_profile_id:
        client_profile = get_object_or_404(ClientProfile, id=client_profile_id)
    else:
        client_user = get_object_or_404(User, id=client_id)
        client_profile = get_object_or_404(ClientProfile, user=client_user)

    client_user = client_profile.user
    if not client_user:
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

    def _build_mode_url(target_mode):
        params = {
            "client_profile_id": client_profile.id,
        }
        if client_user:
            params["client_id"] = client_user.id
        for key in ["week", "status", "q"]:
            value = (request.GET.get(key) or request.POST.get(key) or "").strip()
            if value:
                params[key] = value
        if target_mode == "serie":
            params["mode"] = "serie"
        return f"{reverse('professional_book')}?{urlencode(params)}"

    single_mode_url = _build_mode_url("single")
    series_mode_url = _build_mode_url("serie")

    if mode == "serie":
        return _professional_book_series_view(
            request,
            client_profile=client_profile,
            client_user=client_user,
            prof=prof,
            can_book_any=can_book_any,
            back_to_calendar_url=back_to_calendar_url,
            single_mode_url=single_mode_url,
            series_mode_url=series_mode_url,
        )

    professionals = Professional.objects.select_related("user").order_by("user__username")
    selected_professional_id = request.GET.get("professional_id") or request.POST.get("professional_id") or ""
    occupied_professional_id = request.GET.get("occupied_professional_id") or request.POST.get("occupied_professional_id") or ""
    occupied_date = request.GET.get("occupied_date") or request.POST.get("occupied_date") or ""
    occupied_time = request.GET.get("occupied_time") or request.POST.get("occupied_time") or ""
    selected_prof = prof
    if can_book_any:
        if selected_professional_id:
            selected_prof = get_object_or_404(Professional, id=selected_professional_id)
        else:
            selected_prof = None

    if can_book_any:
        services = Service.objects.all().order_by("name")
    else:
        services = prof.services.all().order_by("name")
    message = ""

    selected_service_id = request.GET.get("service_id") or request.POST.get("service_id") or ""
    selected_date = request.GET.get("date") or request.POST.get("date") or ""
    selected_time = request.GET.get("time") or request.POST.get("time") or ""
    week = request.GET.get("week") or request.POST.get("week") or ""
    status = request.GET.get("status") or request.POST.get("status") or ""
    q = request.GET.get("q") or request.POST.get("q") or ""
    send_client_email_raw = (
        request.POST.get("send_client_email")
        if request.method == "POST"
        else request.GET.get("send_client_email")
    )
    send_client_email_on_create = True
    if send_client_email_raw is not None:
        send_client_email_on_create = str(send_client_email_raw).strip().lower() in {"1", "true", "on", "yes"}
    slots = []
    selection_in_past = False
    if selected_date and selected_time:
        try:
            date_obj = datetime.strptime(selected_date, "%Y-%m-%d").date()
            time_obj = datetime.strptime(selected_time, "%H:%M").time()
            today = timezone.localdate()
            now_t = timezone.localtime().time()
            if date_obj < today or (date_obj == today and time_obj <= now_t):
                selection_in_past = True
        except ValueError:
            selection_in_past = False



    no_services_for_date = False
    filter_date_obj = None
    if selected_date:
        try:
            filter_date_obj = datetime.strptime(selected_date, "%Y-%m-%d").date()
        except ValueError:
            filter_date_obj = None

    if filter_date_obj:
        services_for_date_ids = []
        if can_book_any:
            for service_option in services:
                has_any_prof_available = False
                for prof_option in professionals:
                    if not prof_option.services.filter(id=service_option.id).exists():
                        continue
                    day_slots = _get_slots(
                        prof_option,
                        filter_date_obj,
                        step_minutes=service_option.duration_minutes,
                    )
                    if day_slots:
                        has_any_prof_available = True
                        break
                if has_any_prof_available:
                    services_for_date_ids.append(service_option.id)
        elif selected_prof:
            for service_option in services:
                day_slots = _get_slots(
                    selected_prof,
                    filter_date_obj,
                    step_minutes=service_option.duration_minutes,
                )
                if day_slots:
                    services_for_date_ids.append(service_option.id)
        services = services.filter(id__in=services_for_date_ids)
        no_services_for_date = len(services_for_date_ids) == 0

    if selected_service_id and not services.filter(id=selected_service_id).exists():
        selected_service_id = ""
        selected_time = ""
        if can_book_any:
            selected_professional_id = ""
            selected_prof = None

    selected_service = services.filter(id=selected_service_id).first() if selected_service_id else None
    if can_book_any and selected_service and filter_date_obj:
        available_prof_ids = []
        for prof_option in professionals:
            if not prof_option.services.filter(id=selected_service.id).exists():
                continue
            day_slots = _get_slots(
                prof_option,
                filter_date_obj,
                step_minutes=selected_service.duration_minutes,
            )
            if day_slots:
                available_prof_ids.append(prof_option.id)
        professionals = professionals.filter(id__in=available_prof_ids)

        if selected_professional_id and not professionals.filter(id=selected_professional_id).exists():
            selected_professional_id = ""
            selected_time = ""
            selected_prof = None
        elif selected_professional_id:
            selected_prof = professionals.filter(id=selected_professional_id).first()

    if selected_service and selected_date and selected_prof:
        date_obj = datetime.strptime(selected_date, "%Y-%m-%d").date()
        slots = _get_slots(selected_prof, date_obj, service=selected_service)

    selected_professional_name = selected_prof.user.get_full_name() or selected_prof.user.username if selected_prof else ""

    if request.method == "POST":
        blocked, retry_after = check_rate_limit(
            request,
            name="professional_book_user_minute",
            limit=20,
            window=60,
            by_user=True,
            by_ip=True,
        )
        if blocked:
            message = "Demasiadas tentativas. Tenta novamente em alguns minutos."
            service_map = {
                str(p.id): [{"id": s.id, "name": s.name} for s in p.services.all().order_by("name")]
                for p in professionals
            }
            response = render(
                request,
                "core/professional_book.html",
                {
                    "booking_mode": "single",
                    "client_profile": client_profile,
                    "client_id": client_user.id,
                    "services": services,
                    "professionals": professionals,
                    "selected_professional_id": selected_professional_id,
                    "selected_professional_name": selected_professional_name,
                    "selected_service_id": selected_service_id,
                    "selected_date": selected_date,
                    "selected_time": selected_time,
                    "occupied_professional_id": occupied_professional_id,
                    "occupied_date": occupied_date,
                    "occupied_time": occupied_time,
                    "no_services_for_date": no_services_for_date,
                    "slots": slots,
                    "message": message,
                    "week": week,
                    "is_admin": can_book_any,
                    "service_map_json": json.dumps(service_map, ensure_ascii=True),
                    "back_to_calendar_url": back_to_calendar_url,
                    "single_mode_url": single_mode_url,
                    "series_mode_url": series_mode_url,
                    "send_client_email_on_create": send_client_email_on_create,
                },
                status=429,
            )
            response["Retry-After"] = str(retry_after)
            return response
        service_id = request.POST.get("service_id")
        date_str = request.POST.get("date")
        time_str = request.POST.get("time")
        professional_id = request.POST.get("professional_id") or ""
        symptomatology = (request.POST.get("symptomatology") or "").strip()

        if not (service_id and date_str and time_str) or (can_book_any and not professional_id):
            message = "Dados incompletos."
        else:
            service = get_object_or_404(Service, id=service_id)
            date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
            if can_book_any:
                selected_prof = get_object_or_404(Professional, id=professional_id)
                selected_professional_id = str(selected_prof.id)
            else:
                selected_prof = prof
            if (
                occupied_professional_id
                and occupied_date
                and occupied_time
                and date_str == occupied_date
                and time_str == occupied_time
                and str(selected_prof.id) == str(occupied_professional_id)
            ):
                message = "Este profissional já tem marcação neste horário."
            if selected_prof and not selected_prof.services.filter(id=service.id).exists():
                message = "Este profissional não realiza este serviço."
            today = timezone.localdate()
            now_t = timezone.localtime().time()

            if date_obj < today:
                message = "Não podes marcar no passado."
            elif date_obj == today:
                time_obj = datetime.strptime(time_str, "%H:%M").time()
                if time_obj <= now_t:
                    message = "Este horário já passou."

            if message:
                service_map = {
                    str(p.id): [{"id": s.id, "name": s.name} for s in p.services.all().order_by("name")]
                    for p in professionals
                }
                valid_slots = _get_slots(selected_prof, date_obj, service=service) if selected_prof else []
                selected_service_id = service_id
                selected_date = date_str
                selected_time = time_str
                slots = valid_slots
                selected_professional_name = selected_prof.user.get_full_name() or selected_prof.user.username if selected_prof else ""
                return render(
                    request,
                    "core/professional_book.html",
                    {
                        "booking_mode": "single",
                        "client_profile": client_profile,
                        "client_id": client_user.id,
                        "services": services,
                        "professionals": professionals,
                        "selected_professional_id": selected_professional_id,
                        "selected_professional_name": selected_professional_name,
                        "selected_service_id": selected_service_id,
                        "selected_date": selected_date,
                        "selected_time": selected_time,
                        "occupied_professional_id": occupied_professional_id,
                        "occupied_date": occupied_date,
                        "occupied_time": occupied_time,
                        "no_services_for_date": no_services_for_date,
                        "slots": slots,
                        "message": message,
                        "week": week,
                        "is_admin": can_book_any,
                        "service_map_json": json.dumps(service_map, ensure_ascii=True),
                        "back_to_calendar_url": back_to_calendar_url,
                        "single_mode_url": single_mode_url,
                        "series_mode_url": series_mode_url,
                        "status": status,
                        "q": q,
                        "send_client_email_on_create": send_client_email_on_create,
                    },
                )

            time_obj = datetime.strptime(time_str, "%H:%M").time()
            if _is_slot_blocked(selected_prof, date_obj, time_obj):
                message = "Este horário está indisponível."
                service_map = {
                    str(p.id): [{"id": s.id, "name": s.name} for s in p.services.all().order_by("name")]
                    for p in professionals
                }
                valid_slots = _get_slots(selected_prof, date_obj, service=service)
                selected_service_id = service_id
                selected_date = date_str
                selected_time = time_str
                slots = valid_slots
                selected_professional_name = selected_prof.user.get_full_name() or selected_prof.user.username if selected_prof else ""
                return render(
                    request,
                    "core/professional_book.html",
                    {
                        "booking_mode": "single",
                        "client_profile": client_profile,
                        "client_id": client_user.id,
                        "services": services,
                        "professionals": professionals,
                        "selected_professional_id": selected_professional_id,
                        "selected_professional_name": selected_professional_name,
                        "selected_service_id": selected_service_id,
                        "selected_date": selected_date,
                        "selected_time": selected_time,
                        "occupied_professional_id": occupied_professional_id,
                        "occupied_date": occupied_date,
                        "occupied_time": occupied_time,
                        "no_services_for_date": no_services_for_date,
                        "slots": slots,
                        "message": message,
                        "week": week,
                        "is_admin": can_book_any,
                        "service_map_json": json.dumps(service_map, ensure_ascii=True),
                        "back_to_calendar_url": back_to_calendar_url,
                        "single_mode_url": single_mode_url,
                        "series_mode_url": series_mode_url,
                        "status": status,
                        "q": q,
                        "send_client_email_on_create": send_client_email_on_create,
                    },
                )

            valid_slots = _get_slots(selected_prof, date_obj, service=service)
            if time_str not in valid_slots:
                message = "Hora inválida ou já ocupada."
                selected_service_id = service_id
                selected_date = date_str
                selected_time = time_str
                slots = valid_slots
            else:
                pricing = compute_pricing(service, client_profile)
                appt = Appointment.objects.create(
                        client=client_user,
                        professional=selected_prof,
                        service=service,
                        date=date_obj,
                        time=time_obj,
                        symptomatology=symptomatology,
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
                        f"Nova marcação — {service.name} — {appt.date} {appt.time}",
                        "emails/clinic_appointment_event.html",
                        "emails/clinic_appointment_event.txt",
                        {
                            "event_type": "created",
                            "event_title": "Nova marcação",
                            "client_name": client_user.get_full_name() or client_user.username,
                            "client_phone": getattr(client_profile, "phone", ""),
                            "service_name": service.name,
                            "professional_name": selected_prof.user.get_full_name() or selected_prof.user.username,
                            "old_date": "",
                            "old_time": "",
                            "new_date": appt.date,
                            "new_time": appt.time,
                            "cancelled_at": "",
                            "actor": "Clínica",
                            "admin_url": request.build_absolute_uri(f"/admin/core/appointment/{appt.id}/change/"),
                        },
                        event="new_booking",
                    )

                if settings_obj.notify_professional_on_new_booking:
                    prof_email = getattr(selected_prof.user, "email", "")
                    if prof_email:
                        send_templated_email(
                            prof_email,
                            f"Nova marcação — {service.name} — {appt.date} {appt.time}",
                            "emails/clinic_appointment_event.html",
                            "emails/clinic_appointment_event.txt",
                            {
                                "event_type": "created",
                                "event_title": "Nova marcação",
                                "client_name": client_user.get_full_name() or client_user.username,
                                "client_phone": getattr(client_profile, "phone", ""),
                                "service_name": service.name,
                                "professional_name": selected_prof.user.get_full_name() or selected_prof.user.username,
                                "old_date": "",
                                "old_time": "",
                                "new_date": appt.date,
                                "new_time": appt.time,
                                "cancelled_at": "",
                                "actor": "Clínica",
                                "admin_url": "",
                            },
                            event="new_booking",
                        )
                    else:
                        log_email_skip("new_booking", "Nova marcação", "Profissional sem email", "")

                if settings_obj.notify_client_on_new_booking and send_client_email_on_create:
                    client_email = client_user.email or ""
                    if client_email:
                        send_templated_email(
                            client_email,
                            f"Marcação confirmada — {service.name} em {appt.date} {appt.time}",
                            "emails/appointment_confirmed.html",
                            "emails/appointment_confirmed.txt",
                            {
                                "client_name": client_user.get_full_name() or client_user.username,
                                "service_name": service.name,
                                "professional_name": selected_prof.user.get_full_name() or selected_prof.user.username,
                                "date": appt.date,
                                "time": appt.time,
                                "symptomatology": symptomatology,
                                "manage_url": request.build_absolute_uri(reverse("my_appointments")),
                            },
                            event="new_booking",
                        )
                    else:
                        log_email_skip("new_booking", "Marcação confirmada", "Cliente sem email", "")
                messages.success(request, "Marcação criada para o cliente.")
                return redirect(back_to_calendar_url)

    service_map = {
        str(p.id): [{"id": s.id, "name": s.name} for s in p.services.all().order_by("name")]
        for p in professionals
    }
    service_map_json = json.dumps(service_map, ensure_ascii=True)

    return render(
        request,
        "core/professional_book.html",
        {
            "booking_mode": "single",
            "client_profile": client_profile,
            "client_id": client_user.id,
            "services": services,
            "professionals": professionals,
            "service_map": service_map,
            "selected_professional_id": selected_professional_id,
            "selected_professional_name": selected_professional_name,
            "selected_service_id": selected_service_id,
            "selected_date": selected_date,
            "selected_time": selected_time,
            "occupied_professional_id": occupied_professional_id,
            "occupied_date": occupied_date,
            "occupied_time": occupied_time,
            "no_services_for_date": no_services_for_date,
            "slots": slots,
            "message": message,
            "week": week,
            "is_admin": can_book_any,
            "service_map_json": service_map_json,
            "back_to_calendar_url": back_to_calendar_url,
            "selection_in_past": selection_in_past,
            "status": status,
            "q": q,
            "single_mode_url": single_mode_url,
            "series_mode_url": series_mode_url,
            "send_client_email_on_create": send_client_email_on_create,
        },
    )
    week = request.POST.get("week") or request.GET.get("week") or ""
    if week:
        return redirect(f"/prof/calendario/?week={week}")
        return redirect("professional_calendar")


def my_appointments_view(request):
    """
    Página do cliente/profissional: lista de marcações futuras e passadas.
    """
    prof = Professional.objects.filter(user=request.user).first()
    is_professional_view = bool(prof)

    display_name = (request.user.get_full_name() or request.user.username or "").strip()
    display_name = display_name or "utilizador"
    profile = None
    profile_incomplete = False

    if is_professional_view:
        profile = prof
    else:
        profile = getattr(request.user, "client_profile", None)
        if profile:
            required_fields = ["full_name", "phone", "address_line1", "postal_code"]
            missing_basic = any(not getattr(profile, f, None) for f in required_fields)
            missing_location = not (profile.locality or profile.city)
            profile_incomplete = missing_basic or missing_location

    if is_professional_view:
        qs = (
            Appointment.objects
            .filter(professional=prof)
            .exclude(status=Appointment.STATUS_CANCELLED)
            .select_related("service", "client")
            .order_by("-date", "-time", "-id")
        )
    else:
        qs = (
            Appointment.objects
            .filter(client=request.user)
            .exclude(status=Appointment.STATUS_CANCELLED)
            .select_related("service", "professional", "professional__user")
            .order_by("-date", "-time", "-id")
        )

    today = timezone.localdate()
    now_t = timezone.localtime().time()

    upcoming = []
    past = []

    def payment_meta(appt):
        if appt.is_paid:
            return ("Pago", "success")
        if appt.status == Appointment.STATUS_IN_DEBT:
            return ("Em dívida", "danger")
        return ("Por pagar", "warning")

    month_filter = (request.GET.get("month") or "").strip()
    month_year = None
    month_value = None
    if month_filter:
        try:
            month_year = datetime.strptime(month_filter, "%Y-%m")
            month_value = month_year.strftime("%Y-%m")
        except Exception:
            month_year = None

    for a in qs:
        # label bonito para o template (sem mexer no model)
        a.status_label = _status_label(a.status)
        a.payment_label, a.payment_badge = payment_meta(a)

        is_future = (a.date > today) or (a.date == today and a.time and a.time >= now_t)
        # Só conta como futura se estiver agendada ou em confirmação
        if a.status not in (Appointment.STATUS_SCHEDULED, Appointment.STATUS_PENDING):
            past.append(a)
        else:
            if is_future:
                if month_year:
                    if a.date.year == month_year.year and a.date.month == month_year.month:
                        upcoming.append(a)
                else:
                    upcoming.append(a)
            else:
                past.append(a)

    next_appt = None
    if upcoming:
        # reorder upcoming crescente (próxima primeiro)
        upcoming_sorted = sorted(upcoming, key=lambda x: (x.date, x.time or dtime.min, x.id))
        next_appt = upcoming_sorted[0]
        upcoming = upcoming_sorted

    # também mete label no next (redundante mas seguro)
    if next_appt:
        next_appt.status_label = _status_label(next_appt.status)
        next_appt.payment_label, next_appt.payment_badge = payment_meta(next_appt)

    # Turmas (apenas para clientes)
    group_upcoming = []
    group_past = []
    next_group_upcoming = None
    if not is_professional_view:
        update_group_sessions_statuses()

        group_enrolments = (
            GroupEnrollment.objects
            .select_related("session", "session__service", "session__professional", "session__professional__user")
            .filter(
                client=request.user,
                status__in=group_booked_statuses() + [GroupEnrollment.STATUS_WAITLIST],
            )
            .order_by("session__date", "session__time")
        )
        for e in group_enrolments:
            s = e.session
            is_future = (s.date > today) or (s.date == today and s.time and s.time > now_t)
            (group_upcoming if is_future else group_past).append(e)
        if group_upcoming:
            next_group_upcoming = group_upcoming[0]

    upcoming_page = Paginator(upcoming, 5).get_page(request.GET.get("page") or 1)
    past_page = Paginator(past, 6).get_page(request.GET.get("past_page") or 1)
    if is_professional_view:
        upcoming_months = (
            Appointment.objects
            .filter(professional=prof)
            .exclude(
                status__in=[
                    Appointment.STATUS_COMPLETED,
                    Appointment.STATUS_IN_DEBT,
                    Appointment.STATUS_CANCELLED,
                    Appointment.STATUS_NO_SHOW,
                ]
            )
            .dates("date", "month", order="ASC")
        )
    else:
        upcoming_months = (
            Appointment.objects
            .filter(client=request.user)
            .exclude(
                status__in=[
                    Appointment.STATUS_COMPLETED,
                    Appointment.STATUS_IN_DEBT,
                    Appointment.STATUS_CANCELLED,
                    Appointment.STATUS_NO_SHOW,
                ]
            )
            .dates("date", "month", order="ASC")
        )

    return render(
        request,
        "core/my_appointments.html",
        {
            "display_name": display_name,
            "profile": profile,
            "profile_incomplete": profile_incomplete,
            "is_professional_view": is_professional_view,
            "client_upcoming_count": len(upcoming),
            "next_appointment": next_appt,
            "upcoming": upcoming_page,
            "past": past_page,
            "group_upcoming": group_upcoming,
            "group_past": group_past,
            "next_group_upcoming": next_group_upcoming,
            "upcoming_months": upcoming_months,
            "selected_month": month_value,
        },
    )


def cancel_appointment_view(request, appointment_id):
    """
    Cancelar uma marcação do próprio cliente.
    Regras:
    - só pode cancelar marcações dele
    - não pode cancelar se já estiver completed
    - marca como cancelled (não apaga)
    """
    appt = get_object_or_404(
        Appointment,
        id=appointment_id,
        client=request.user,
    )

    if appt.status in {Appointment.STATUS_COMPLETED, Appointment.STATUS_IN_DEBT, Appointment.STATUS_NO_SHOW}:
        return HttpResponseForbidden("Não podes cancelar uma marcação concluída, em dívida ou em falta.")

    if appt.status == Appointment.STATUS_CANCELLED:
        # idempotente: já está cancelada
        return redirect("my_appointments")

    old_status = appt.status
    appt.status = Appointment.STATUS_CANCELLED
    appt.save(update_fields=["status"])

    log_appt(
        AppointmentLog.ACTION_CANCELLED,
        appt,
        request.user,
        old_status=old_status,
        new_status=appt.status,
        request=request,
    )

    settings_obj = clinic_settings()
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
                    "event_title": "Marcação cancelada pelo cliente",
                    "client_name": request.user.get_full_name() or request.user.username,
                    "client_phone": getattr(getattr(request.user, "client_profile", None), "phone", ""),
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
                event="cancel_client",
            )
        else:
            log_email_skip(
                "cancel_client",
                "Marcação cancelada",
                "Email da clínica vazio.",
            )

    return redirect("my_appointments")


def reschedule_appointment_view(request, appointment_id):
    """
    Reagendar marcação:
    - Admin pode tudo
    - Profissional só pode reagendar marcações dele
    - Cliente só pode reagendar marcações dele
    - Não permite reagendar se estiver completed/cancelled
    """
    appt = get_object_or_404(
        Appointment.objects.select_related(
            "client", "service", "professional", "professional__user"
        ),
        id=appointment_id,
    )

    if not can_modify_appointment(request.user, appt):
        return HttpResponseForbidden("Não podes reagendar esta marcação.")

    if appt.status in [
        Appointment.STATUS_COMPLETED,
        Appointment.STATUS_IN_DEBT,
        Appointment.STATUS_CANCELLED,
        Appointment.STATUS_NO_SHOW,
    ]:
        return HttpResponseForbidden("Não podes reagendar uma marcação concluída, em dívida, cancelada ou em falta.")

    is_prof_flow = can_view_all_calendar(request.user) or Professional.objects.filter(user=request.user).exists()

    if is_prof_flow:
        params = {}
        for key in ["week", "status", "q"]:
            value = (request.GET.get(key) or request.POST.get(key) or "").strip()
            if value:
                params[key] = value

        if appt.date:
            params["date"] = appt.date.strftime("%Y-%m-%d")
        if appt.time:
            params["time"] = appt.time.strftime("%H:%M")
        if appt.service_id:
            params["service_id"] = str(appt.service_id)
            params["service_label"] = (appt.service.name or "").strip()
        if appt.professional_id:
            params["professional_id"] = str(appt.professional_id)
            params["professional_label"] = (
                (appt.professional.user.get_full_name() or appt.professional.user.username or "").strip()
            )

        params["quick_open"] = "1"
        params["reschedule_id"] = str(appt.id)
        params["quick_client_user_id"] = str(appt.client_id)

        client_profile = getattr(appt.client, "client_profile", None)
        if client_profile:
            params["quick_client_profile_id"] = str(client_profile.id)

        client_label = (
            (getattr(client_profile, "full_name", "") or "").strip()
            or (appt.client.get_full_name() or "").strip()
            or (appt.client.username or "").strip()
        )
        if client_label:
            params["quick_client_label"] = client_label

        base_calendar_url = reverse("professional_calendar")
        query = urlencode(params)
        return redirect(f"{base_calendar_url}?{query}" if query else base_calendar_url)

    today = timezone.localdate()
    if appt.date == today:
        messages.error(request, "Não podes reagendar no dia da consulta.")
        return redirect("my_appointments")

    params = {
        "service_id": appt.service_id,
        "professional_id": appt.professional_id,
        "date": appt.date.strftime("%Y-%m-%d") if appt.date else "",
        "time": appt.time.strftime("%H:%M") if appt.time else "",
        "reschedule_id": appt.id,
    }
    return redirect(f"/marcar/?{urlencode(params)}")


def complete_appointment_view(request, appointment_id):
    appt = get_object_or_404(Appointment, id=appointment_id)

    if not can_modify_appointment(request.user, appt):
        return HttpResponseForbidden("Não podes concluir esta marcação.")

    if appt.status in {Appointment.STATUS_COMPLETED, Appointment.STATUS_IN_DEBT, Appointment.STATUS_NO_SHOW}:
        return redirect(request.META.get("HTTP_REFERER", "/"))

    old_status = appt.status
    appt.status = "completed"
    appt.save(update_fields=["status"])
    sync_subcontractor_payout(appt, actor=request.user)

    log_appt(
        AppointmentLog.ACTION_COMPLETED,
        appt,
        request.user,
        old_status=old_status,
        new_status=appt.status,
        request=request,
    )

    messages.success(request, "Marcação marcada como concluída.")

    if can_view_all_calendar(request.user):
        return redirect("professional_calendar")

    return redirect("my_appointments")


def api_professionals_by_service(request):
    service_id = request.GET.get("service_id")
    if not service_id:
        return JsonResponse({"results": []})

    qs = (
        Professional.objects
        .filter(services__id=service_id)
        .select_related("user")
        .order_by("user__username")
        .distinct()
    )

    data = []
    for p in qs:
        data.append(
            {
                "id": p.id,
                "label": p.user.get_full_name() or p.user.username,
                "weekdays": professional_weekdays(p),
                "weekdays_label": professional_weekdays_labels(p),
            }
        )
    return JsonResponse({"results": data})
