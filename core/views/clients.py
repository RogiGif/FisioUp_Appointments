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
from django.contrib.auth.password_validation import validate_password
from django.core.paginator import Paginator
from django.core.validators import validate_email
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q, Count, Sum, Min
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
from core.services.audit import log_audit_event
from core.forms import (
    RegisterForm,
    ClientProfileForm,
    ProfessionalProfileForm,
    StaffClientCreateForm,
    BackofficeServiceForm,
    BackofficeProfessionalForm,
    BackofficePartnerForm,
)
from core.utils.pricing import compute_pricing, recalculate_upcoming_appointment_prices
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
    GroupMonthlyCharge,
    MoloniIntegration,
    ClientImportLog,
    ClientImportBatch,
    ClientImportRow,
    Partner,
    PartnerServicePrice,
    ContentPost,
)

from core.views.common import *


def _clinical_record_audit_snapshot(record):
    return {
        "id": record.id,
        "client_id": record.client_id,
        "updated_by_id": record.updated_by_id,
        "has_allergies": bool((record.allergies or "").strip()),
        "has_conditions": bool((record.conditions or "").strip()),
        "has_notes": bool((record.notes or "").strip()),
        "allergies_length": len(record.allergies or ""),
        "conditions_length": len(record.conditions or ""),
        "notes_length": len(record.notes or ""),
        "updated_at": record.updated_at.isoformat() if record.updated_at else "",
    }


def _treatment_record_audit_snapshot(record):
    return {
        "id": record.id,
        "client_id": record.client_id,
        "professional_id": record.professional_id,
        "appointment_id": record.appointment_id,
        "service_id": record.service_id,
        "service_name": record.service_name,
        "date": record.date.isoformat() if record.date else "",
        "time": record.time.isoformat() if record.time else "",
        "notes_length": len(record.notes or ""),
        "created_by_id": record.created_by_id,
        "updated_by_id": record.updated_by_id,
    }


def professional_clients_view(request):
    """
    Lista de clientes que:
    - Já tiveram marcação com o profissional OU têm marcação futura com ele.
    Admin vê todos.
    """
    prof = _get_professional_or_403(request.user)
    if not can_view_all_calendar(request.user) and prof is None:
        return HttpResponseForbidden("Acesso restrito a profissionais.")

    q = (request.GET.get("q") or "").strip()
    today = timezone.localdate()

    can_edit_clients = can_access_backoffice(request.user)

    if can_view_all_calendar(request.user):
        # Admin/receção vê todos os clientes (inclui importados sem user)
        qs = ClientProfile.objects.select_related("user").all()
    else:
        # Profissionais devem ver todos os clientes (inclui importados sem user)
        eligible_users = (
            User.objects
            .filter(is_staff=False, is_superuser=False, professional__isnull=True)
            .exclude(groups__name__in=["TECHNICIAN", "ADMIN"])
            .distinct()
        )
        missing_profiles = eligible_users.filter(client_profile__isnull=True)
        for u in missing_profiles:
            ClientProfile.objects.get_or_create(
                user=u,
                defaults={
                    "full_name": (u.get_full_name() or u.username),
                    "created_by": request.user,
                    "updated_by": request.user,
                    "registration_status": "approved",
                },
            )

        qs = (
            ClientProfile.objects
            .select_related("user")
            .filter(
                Q(user__isnull=True)
                | Q(user__is_staff=False, user__is_superuser=False, user__professional__isnull=True)
            )
        )

    if q:
        qs = apply_terms_filter(
            qs,
            q,
            [
                "full_name__icontains",
                "user__username__icontains",
                "user__email__icontains",
                "phone__icontains",
                "nif__icontains",
            ],
        )

    sort_order = (request.GET.get("order") or "asc").lower()
    if sort_order not in {"asc", "desc"}:
        sort_order = "asc"
    order_prefix = "-" if sort_order == "desc" else ""
    clients_qs = qs.order_by(f"{order_prefix}full_name", f"{order_prefix}user__username")
    per_page_options = [5, 10, 15]
    try:
        per_page = int(request.GET.get("per_page") or per_page_options[0])
    except (TypeError, ValueError):
        per_page = per_page_options[0]
    if per_page not in per_page_options:
        per_page = per_page_options[0]

    paginator = Paginator(clients_qs, per_page)
    page_obj = paginator.get_page(request.GET.get("page"))

    base_params = request.GET.copy()
    base_params.pop("page", None)

    def qs_with(**kwargs):
        params = base_params.copy()
        for k, v in kwargs.items():
            params[k] = v
        return params.urlencode()

    toggle_order = "desc" if sort_order == "asc" else "asc"
    sort_toggle_qs = qs_with(order=toggle_order)

    total_pages = paginator.num_pages
    current_page = page_obj.number
    if total_pages <= 7:
        pages = list(range(1, total_pages + 1))
    else:
        pages = {1, total_pages}
        for n in range(current_page - 2, current_page + 3):
            if 1 < n < total_pages:
                pages.add(n)
        pages = sorted(pages)

    page_links = []
    prev_page = None
    for n in pages:
        if prev_page and n - prev_page > 1:
            page_links.append({"is_ellipsis": True})
        page_links.append({"number": n, "qs": qs_with(page=n), "is_current": n == current_page})
        prev_page = n
    selected_date = (request.GET.get("date") or "").strip()
    selected_time = (request.GET.get("time") or "").strip()
    selected_service_id = (request.GET.get("service_id") or "").strip()
    selected_professional_id = (request.GET.get("professional_id") or "").strip()
    occupied_professional_id = (request.GET.get("occupied_professional_id") or "").strip()
    occupied_date = (request.GET.get("occupied_date") or "").strip()
    occupied_time = (request.GET.get("occupied_time") or "").strip()
    week = (request.GET.get("week") or "").strip()

    return render(
        request,
        "core/prof/utentes_duralux.html",
        {
            "clients": page_obj.object_list,
            "page_obj": page_obj,
            "paginator": paginator,
            "page_links": page_links,
            "per_page": per_page,
            "per_page_options": per_page_options,
            "total_count": paginator.count,
            "prev_qs": qs_with(page=page_obj.previous_page_number()) if page_obj.has_previous() else "",
            "next_qs": qs_with(page=page_obj.next_page_number()) if page_obj.has_next() else "",
            "sort_order": sort_order,
            "sort_toggle_qs": sort_toggle_qs,
            "q": q,
            "is_admin": can_view_all_calendar(request.user),
            "can_edit_clients": can_edit_clients,
            "selected_date": selected_date,
            "selected_time": selected_time,
            "selected_service_id": selected_service_id,
            "selected_professional_id": selected_professional_id,
            "occupied_professional_id": occupied_professional_id,
            "occupied_date": occupied_date,
            "occupied_time": occupied_time,
            "week": week,
            "return_to": request.get_full_path(),
        },
    )


def professional_customer_detail_view(request, client_id):
    prof = _get_professional_or_403(request.user)
    if not can_view_all_calendar(request.user) and prof is None:
        return HttpResponseForbidden("Acesso restrito a profissionais.")

    profile = get_object_or_404(ClientProfile.objects.select_related("user"), id=client_id)
    is_admin_or_reception = can_view_all_calendar(request.user)
    today = timezone.localdate()
    now_t = timezone.localtime().time()

    can_edit_clients = can_access_backoffice(request.user)
    can_delete_clients = is_admin_role(request.user)
    allowed_tabs = {"profile", "movements", "notifications", "clinical", "partners_discounts"}
    active_tab = (request.GET.get("tab") or "profile").strip().lower()
    if active_tab not in allowed_tabs:
        active_tab = "profile"

    record, _ = ClinicalRecord.objects.get_or_create(
        client=profile,
        defaults={"updated_by": request.user},
    )

    client_user = profile.user
    total_appts = 0
    upcoming_appts = 0
    if profile.user_id:
        appt_qs = Appointment.objects.filter(client_id=profile.user_id)
        total_appts = appt_qs.count()
        upcoming_appts = appt_qs.filter(date__gte=today).count()

    scoped_appts_qs = (
        Appointment.objects
        .filter(client_id=profile.user_id) if profile.user_id else Appointment.objects.none()
    )
    if not is_admin_or_reception:
        scoped_appts_qs = scoped_appts_qs.filter(professional=prof)
    scoped_group_charges_qs = GroupMonthlyCharge.objects.none()
    if profile.user_id:
        first_group_date = (
            GroupEnrollment.objects
            .filter(
                client_id=profile.user_id,
                status__in=group_booked_statuses(),
                session__schedule__isnull=False,
            )
            .aggregate(first_date=Min("session__date"))
            .get("first_date")
        )
        if first_group_date:
            ensure_group_monthly_charges(
                start_date=first_group_date,
                end_date=today,
                client_ids=[profile.user_id],
            )
        scoped_group_charges_qs = GroupMonthlyCharge.objects.filter(client_id=profile.user_id)
        if not is_admin_or_reception:
            scoped_group_charges_qs = scoped_group_charges_qs.filter(professional=prof)

    valid_movement_statuses = {code for code, _ in Appointment.STATUS_CHOICES}
    movement_per_page_options = [5, 10, 15, 25]

    def _sanitize_movement_state(raw_status, raw_per_page, raw_page):
        status_value = (raw_status or "").strip()
        if status_value not in valid_movement_statuses:
            status_value = ""
        try:
            per_page_value = int(raw_per_page or movement_per_page_options[1])
        except (TypeError, ValueError):
            per_page_value = movement_per_page_options[1]
        if per_page_value < movement_per_page_options[0]:
            per_page_value = movement_per_page_options[0]
        if per_page_value > movement_per_page_options[-1]:
            per_page_value = movement_per_page_options[-1]
        if per_page_value not in movement_per_page_options:
            per_page_value = movement_per_page_options[1]
        try:
            page_value = int(raw_page or 1)
        except (TypeError, ValueError):
            page_value = 1
        if page_value < 1:
            page_value = 1
        return status_value, per_page_value, page_value

    movement_status, movement_per_page, movement_page = _sanitize_movement_state(
        request.GET.get("movement_status"),
        request.GET.get("movement_per_page"),
        request.GET.get("movement_page"),
    )

    def _movement_redirect_query_from(source):
        status_value, per_page_value, page_value = _sanitize_movement_state(
            source.get("movement_status"),
            source.get("movement_per_page"),
            source.get("movement_page"),
        )
        params = {
            "tab": "movements",
            "movement_per_page": per_page_value,
            "movement_page": page_value,
        }
        if status_value:
            params["movement_status"] = status_value
        return urlencode(params)

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        movements_bulk_action = (request.POST.get("movements_bulk_action") or "").strip()
        if action == "mark_debt_paid":
            active_tab = "movements"
            appointment_id = (request.POST.get("appointment_id") or "").strip()
            if not appointment_id.isdigit():
                messages.error(request, "Marcação inválida.")
            else:
                target = scoped_appts_qs.filter(
                    id=int(appointment_id),
                    status=Appointment.STATUS_IN_DEBT,
                ).first()
                if target is None:
                    messages.error(request, "A marcação não foi encontrada ou já não está em dívida.")
                else:
                    result = apply_bulk_appointment_action(
                        appointments=[target],
                        action="mark_completed_and_paid_selected",
                        actor=request.user,
                        today=today,
                        now_t=now_t,
                        audit_source="perfil_cliente",
                    )
                    if result["updated"] > 0:
                        messages.success(request, "Marcação marcada como concluída e paga.")
                    elif result["skipped_future"] > 0:
                        messages.error(request, "Só podes regularizar dívidas de marcações já realizadas.")
                    elif result["skipped_locked"] > 0:
                        messages.error(request, "Esta marcação não pode ser alterada no estado atual.")
                    else:
                        messages.info(request, "Sem alterações para aplicar.")

            redirect_params = _movement_redirect_query_from(request.POST)
            return redirect(f"{reverse('prof_customer_detail', kwargs={'client_id': profile.id})}?{redirect_params}")
        elif movements_bulk_action:
            active_tab = "movements"
            selected_ids = [int(v) for v in request.POST.getlist("appointment_ids") if str(v).isdigit()]
            if not selected_ids:
                messages.error(request, "Seleciona pelo menos uma marcação.")
                redirect_params = _movement_redirect_query_from(request.POST)
                return redirect(f"{reverse('prof_customer_detail', kwargs={'client_id': profile.id})}?{redirect_params}")

            bulk_action_map = {
                "mark_paid_selected": "mark_completed_and_paid_selected",
                "mark_in_debt_selected": "mark_in_debt_selected",
            }
            mapped_action = bulk_action_map.get(movements_bulk_action)
            if not mapped_action:
                messages.error(request, "Ação inválida.")
                redirect_params = _movement_redirect_query_from(request.POST)
                return redirect(f"{reverse('prof_customer_detail', kwargs={'client_id': profile.id})}?{redirect_params}")

            selected_appointments = list(
                scoped_appts_qs
                .filter(id__in=selected_ids)
                .order_by("date", "time", "id")
            )
            if not selected_appointments:
                messages.error(request, "Nenhuma marcação válida foi encontrada.")
                redirect_params = _movement_redirect_query_from(request.POST)
                return redirect(f"{reverse('prof_customer_detail', kwargs={'client_id': profile.id})}?{redirect_params}")

            result = apply_bulk_appointment_action(
                appointments=selected_appointments,
                action=mapped_action,
                actor=request.user,
                today=today,
                now_t=now_t,
                audit_source="movimentos_cliente",
            )
            if result["updated"] > 0:
                messages.success(request, f"{result['updated']} marcação(ões) atualizada(s).")
            else:
                messages.info(request, "Sem alterações para aplicar.")
            if result["skipped_future"] > 0:
                messages.warning(request, f"{result['skipped_future']} marcação(ões) futura(s) foram ignoradas.")
            if result["skipped_locked"] > 0:
                messages.warning(request, f"{result['skipped_locked']} marcação(ões) bloqueada(s) foram ignoradas.")
            if result["skipped_unpaid"] > 0:
                messages.warning(request, f"{result['skipped_unpaid']} marcação(ões) sem pagamento foram ignoradas.")

            redirect_params = _movement_redirect_query_from(request.POST)
            return redirect(f"{reverse('prof_customer_detail', kwargs={'client_id': profile.id})}?{redirect_params}")

    history_items = []
    if profile.user_id:
        logs_qs = AppointmentLog.objects.filter(appointment__client_id=profile.user_id)
        if not is_admin_or_reception:
            logs_qs = logs_qs.filter(appointment__professional=prof)
        logs_qs = logs_qs.select_related("appointment", "appointment__service").order_by("-created_at")[:10]
        for log in logs_qs:
            service_name = log.appointment.service.name if log.appointment and log.appointment.service else "Serviço"
            history_items.append(
                {
                    "date": log.created_at.date(),
                    "title": log.get_action_display(),
                    "detail": log.note or service_name,
                    "kind": service_name,
                }
            )

    financial_qs = scoped_appts_qs
    paid_qs = financial_qs.filter(
        status=Appointment.STATUS_COMPLETED,
        is_paid=True,
    )
    paid_group_qs = scoped_group_charges_qs.filter(status=GroupMonthlyCharge.STATUS_PAID)
    total_spent = (
        (paid_qs.aggregate(total=Coalesce(Sum("final_price"), Decimal("0.00")))["total"] or Decimal("0.00"))
        + (paid_group_qs.aggregate(total=Coalesce(Sum("final_price"), Decimal("0.00")))["total"] or Decimal("0.00"))
    )
    current_month_total = (
        (paid_qs.filter(
            date__year=today.year,
            date__month=today.month,
        ).aggregate(total=Coalesce(Sum("final_price"), Decimal("0.00")))["total"] or Decimal("0.00"))
        + (paid_group_qs.filter(
            month=today.replace(day=1),
        ).aggregate(total=Coalesce(Sum("final_price"), Decimal("0.00")))["total"] or Decimal("0.00"))
    )
    debt_total = (
        (financial_qs.filter(
            status=Appointment.STATUS_IN_DEBT,
        ).aggregate(total=Coalesce(Sum("final_price"), Decimal("0.00")))["total"] or Decimal("0.00"))
        + (scoped_group_charges_qs.filter(
            status=GroupMonthlyCharge.STATUS_UNPAID,
        ).aggregate(total=Coalesce(Sum("final_price"), Decimal("0.00")))["total"] or Decimal("0.00"))
    )
    balance = total_spent - debt_total

    movements = []
    movements_qs = (
        financial_qs
        .select_related("service", "professional", "professional__user")
        .order_by("-date", "-time", "-id")
    )
    if movement_status:
        movements_qs = movements_qs.filter(status=movement_status)

    movements_paginator = Paginator(movements_qs, movement_per_page)
    movements_page_obj = movements_paginator.get_page(movement_page)

    movement_base_params = request.GET.copy()
    movement_base_params["tab"] = "movements"
    movement_base_params["movement_per_page"] = str(movement_per_page)
    if movement_status:
        movement_base_params["movement_status"] = movement_status
    else:
        movement_base_params.pop("movement_status", None)
    movement_base_params.pop("movement_page", None)

    def movement_qs_with(**kwargs):
        params = movement_base_params.copy()
        for key, value in kwargs.items():
            if value in (None, ""):
                params.pop(key, None)
            else:
                params[key] = str(value)
        return params.urlencode()

    total_movement_pages = movements_paginator.num_pages
    current_movement_page = movements_page_obj.number
    if total_movement_pages <= 7:
        movement_pages = list(range(1, total_movement_pages + 1))
    else:
        movement_pages = {1, total_movement_pages}
        for n in range(current_movement_page - 2, current_movement_page + 3):
            if 1 < n < total_movement_pages:
                movement_pages.add(n)
        movement_pages = sorted(movement_pages)

    movement_page_links = []
    prev_page = None
    for n in movement_pages:
        if prev_page and n - prev_page > 1:
            movement_page_links.append({"is_ellipsis": True})
        movement_page_links.append(
            {"number": n, "qs": movement_qs_with(movement_page=n), "is_current": n == current_movement_page}
        )
        prev_page = n

    for appt in movements_page_obj.object_list:
        is_past = appt.date < today or (appt.date == today and (appt.time or dtime.min) < now_t)
        professional_name = (
            appt.professional.user.get_full_name()
            if appt.professional and appt.professional.user and appt.professional.user.get_full_name()
            else (appt.professional.user.username if appt.professional and appt.professional.user else "Profissional")
        )
        movements.append(
            {
                "id": appt.id,
                "date": appt.date,
                "time": appt.time,
                "description": appt.service.name if appt.service else "Serviço",
                "professional": professional_name,
                "amount": appt.final_price or Decimal("0.00"),
                "status": _status_label(appt.status),
                "can_settle_debt": appt.status == Appointment.STATUS_IN_DEBT and is_past,
            }
        )

    movement_status_options = [("", "Todos")] + list(Appointment.STATUS_CHOICES)

    return render(
        request,
        "core/prof_customer_view.html",
        {
            "client_profile": profile,
            "client_user": client_user,
            "clinical_record": record,
            "movements": movements,
            "history_items": history_items,
            "total_appts": total_appts,
            "upcoming_appts": upcoming_appts,
            "can_edit_clients": can_edit_clients,
            "can_delete_clients": can_delete_clients,
            "active_tab": active_tab,
            "total_spent": total_spent,
            "current_month_total": current_month_total,
            "balance": balance,
            "debt_total": debt_total,
            "movement_status": movement_status,
            "movement_status_options": movement_status_options,
            "movement_per_page": movement_per_page,
            "movement_per_page_options": movement_per_page_options,
            "movements_page_obj": movements_page_obj,
            "movements_paginator": movements_paginator,
            "movement_page_links": movement_page_links,
            "movements_prev_qs": (
                movement_qs_with(movement_page=movements_page_obj.previous_page_number())
                if movements_page_obj.has_previous()
                else ""
            ),
            "movements_next_qs": (
                movement_qs_with(movement_page=movements_page_obj.next_page_number())
                if movements_page_obj.has_next()
                else ""
            ),
        },
    )


@require_POST
def professional_customer_delete_view(request, client_id):
    prof = _get_professional_or_403(request.user)
    if not can_view_all_calendar(request.user) and prof is None:
        return HttpResponseForbidden("Acesso restrito a profissionais.")
    if not is_admin_role(request.user):
        return HttpResponseForbidden("Sem permissão para apagar clientes.")

    profile = get_object_or_404(ClientProfile.objects.select_related("user"), id=client_id)
    client_user = profile.user

    if client_user and (
        Appointment.objects.filter(client=client_user).exists()
        or GroupEnrollment.objects.filter(client=client_user).exists()
    ):
        messages.error(
            request,
            "Não foi possível apagar: este cliente tem histórico de marcações ou turmas.",
        )
        return redirect("prof_customer_detail", client_id=profile.id)

    client_name = profile.full_name or (client_user.username if client_user else "Cliente")
    if client_user:
        client_user.delete()
    else:
        profile.delete()

    messages.success(request, f'Cliente "{client_name}" apagado com sucesso.')
    return redirect("professional_clients")


def professional_client_record_view(request, client_id):
    """
    Ficha do cliente para profissionais:
    - Admin: pode ver tudo
    - Profissional: só se houver ligação por marcações (passadas ou futuras)
    """
    prof = _get_professional_or_403(request.user)
    if not can_view_all_calendar(request.user) and prof is None:
        return HttpResponseForbidden("Acesso restrito a profissionais.")

    profile = get_object_or_404(ClientProfile.objects.select_related("user"), id=client_id)

    # Registo clínico (assumindo 1 por cliente)
    record, _ = ClinicalRecord.objects.get_or_create(
        client=profile,
        defaults={"updated_by": request.user},
    )

    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "update_clinical":
            before = _clinical_record_audit_snapshot(record)
            record.conditions = (request.POST.get("conditions") or "").strip()
            record.notes = (request.POST.get("notes") or "").strip()
            record.updated_by = request.user
            record.save()
            log_audit_event(
                category="clinical_record",
                action="update",
                request=request,
                instance=record,
                source="professional_client_record",
                message="Ficha clínica atualizada pelo profissional.",
                before=before,
                after=_clinical_record_audit_snapshot(record),
            )
            messages.success(request, "Ficha clínica atualizada.")
            return redirect(request.path)

    # Consultas desse cliente (admin vê todas; profissional vê só as dele)
    appts_qs = (
        Appointment.objects
        .filter(client_id=profile.user_id) if profile.user_id else Appointment.objects.none()
        .select_related("service", "professional", "professional__user")
        .order_by("-date", "-time", "-id")
    )
    if not can_view_all_calendar(request.user):
        appts_qs = appts_qs.filter(professional=prof)

    appt_status = (request.GET.get("appt_status") or "").strip()
    if appt_status:
        appts_qs = appts_qs.filter(status=appt_status)

    appt_month = (request.GET.get("appt_month") or "").strip()
    if appt_month and "-" in appt_month:
        try:
            year_str, month_str = appt_month.split("-", 1)
            appts_qs = appts_qs.filter(date__year=int(year_str), date__month=int(month_str))
        except ValueError:
            appt_month = ""

    appt_page_size = request.GET.get("appt_page_size") or "5"
    if appt_page_size not in {"5", "10", "15", "25"}:
        appt_page_size = "5"
    appt_page_number = request.GET.get("appt_page") or "1"
    appt_paginator = Paginator(appts_qs, int(appt_page_size))
    appt_page_obj = appt_paginator.get_page(appt_page_number)
    appts = list(appt_page_obj.object_list)
    if "_status_label" in globals():
        for a in appts:
            a.status_label = _status_label(a.status)

    # ✅ LOGS (histórico) das marcações deste cliente
    logs_qs = AppointmentLog.objects.none()
    if profile.user_id:
        logs_qs = AppointmentLog.objects.filter(appointment__client_id=profile.user_id)
        if not can_view_all_calendar(request.user):
            logs_qs = logs_qs.filter(appointment__professional=prof)
        logs_qs = logs_qs.select_related("actor", "appointment", "appointment__service")

    log_action = (request.GET.get("log_action") or "").strip()
    if log_action:
        logs_qs = logs_qs.filter(action=log_action)

    log_q = (request.GET.get("log_q") or "").strip()
    if log_q:
        logs_qs = logs_qs.filter(
            Q(note__icontains=log_q)
            | Q(actor__username__icontains=log_q)
            | Q(appointment__service__name__icontains=log_q)
        )

    log_page_size = request.GET.get("log_page_size") or "5"
    if log_page_size not in {"5", "10", "15", "25"}:
        log_page_size = "5"
    log_page_number = request.GET.get("log_page") or "1"
    log_paginator = Paginator(logs_qs.order_by("-created_at"), int(log_page_size))
    log_page_obj = log_paginator.get_page(log_page_number)

    appt_query_params = request.GET.copy()
    appt_query_params.pop("appt_page", None)
    log_query_params = request.GET.copy()
    log_query_params.pop("log_page", None)

    return render(
        request,
        "core/professional_client_record.html",
        {
            "client_profile": profile,
            "record": record,
            "appointments": appts,
            "appt_page_obj": appt_page_obj,
            "appt_paginator": appt_paginator,
            "appt_status": appt_status,
            "appt_month": appt_month,
            "appt_page_size": appt_page_size,
            "logs": log_page_obj.object_list,
            "log_page_obj": log_page_obj,
            "log_paginator": log_paginator,
            "log_action": log_action,
            "log_q": log_q,
            "log_page_size": log_page_size,
            "appt_querystring": appt_query_params.urlencode(),
            "log_querystring": log_query_params.urlencode(),
            "is_admin": can_view_all_calendar(request.user),
            "appt_status_choices": Appointment.STATUS_CHOICES,
            "log_action_choices": AppointmentLog.ACTION_CHOICES,
        },
    )


def professional_edit_client_profile_view(request, client_id):
    prof = _get_professional_or_403(request.user)
    if not can_view_all_calendar(request.user) and prof is None:
        return HttpResponseForbidden("Acesso restrito a profissionais.")
    if not can_access_backoffice(request.user):
        return HttpResponseForbidden("Sem permissão para editar clientes.")

    profile = get_object_or_404(ClientProfile.objects.select_related("user"), id=client_id)

    client_user = profile.user
    if request.method == "POST":
        post_data = request.POST.copy()
        email = (post_data.get("email") or "").strip()
        email_error = ""
        require_complete = bool(getattr(profile, "require_complete_profile", False))
        if require_complete and not email:
            email_error = "Campo de preenchimento obrigatório"
        elif email:
            email = email.strip().lower()
            if " " in email or "@" not in email:
                email_error = "Indica um email válido."
            else:
                local, domain = email.split("@", 1)
                if "." not in domain:
                    email_error = "Indica um email válido."
                else:
                    exists = User.objects.filter(email__iexact=email)
                    if client_user:
                        exists = exists.exclude(pk=client_user.pk)
                    if exists.exists():
                        email_error = "Este email já está registado."
        if post_data.get("postal_code_1") or post_data.get("postal_code_2"):
            cp1 = (post_data.get("postal_code_1") or "").strip()
            cp2 = (post_data.get("postal_code_2") or "").strip()
            post_data["postal_code"] = f"{cp1}-{cp2}" if cp1 and cp2 else ""
        form = ClientProfileForm(post_data, request.FILES, instance=profile)
        if form.is_valid() and not email_error:
            p = form.save(commit=False)
            if not p.city and p.locality:
                p.city = p.locality
            p.updated_by = request.user
            p.save()
            if client_user and email and client_user.email != email:
                client_user.email = email
                client_user.save(update_fields=["email"])
            messages.success(request, "Dados do cliente atualizados com sucesso.")
            return redirect("professional_clients")
    else:
        form = ClientProfileForm(instance=profile)
        email_error = ""

    return render(
        request,
        "core/professional_client_edit.html",
        {
            "form": form,
            "client_profile": profile,
            "client_email": client_user.email if client_user else "",
            "email_error": email_error,
        },
    )


def professional_customer_form_view(request, client_id=None):
    prof = _get_professional_or_403(request.user)
    if not can_view_all_calendar(request.user) and not can_access_backoffice(request.user) and prof is None:
        return HttpResponseForbidden("Acesso restrito a profissionais.")

    selected_date = (request.GET.get("date") or request.POST.get("date") or "").strip()
    selected_time = (request.GET.get("time") or request.POST.get("time") or "").strip()
    selected_service_id = (request.GET.get("service_id") or request.POST.get("service_id") or "").strip()
    selected_professional_id = (request.GET.get("professional_id") or request.POST.get("professional_id") or "").strip()
    week = (request.GET.get("week") or request.POST.get("week") or "").strip()
    status = (request.GET.get("status") or request.POST.get("status") or "").strip()
    q = (request.GET.get("q") or request.POST.get("q") or "").strip()
    return_to = (request.GET.get("return_to") or request.POST.get("return_to") or "").strip()
    return_to_quick_modal = return_to == "calendar_quick_modal"

    is_edit = client_id is not None
    client_profile = None
    client_user = None
    clinical_record = None
    if is_edit:
        if not can_access_backoffice(request.user):
            return HttpResponseForbidden("Sem permissão para editar clientes.")
        client_profile = get_object_or_404(ClientProfile.objects.select_related("user"), id=client_id)
        client_user = client_profile.user
        clinical_record, _ = ClinicalRecord.objects.get_or_create(
            client=client_profile,
            defaults={"updated_by": request.user},
        )

    prefill_profile_id = ""
    prefill_profile = None
    if not is_edit:
        prefill_profile_id = (request.GET.get("prefill_profile_id") or request.POST.get("prefill_profile_id") or "").strip()
        if prefill_profile_id.isdigit():
            prefill_profile = ClientProfile.objects.filter(id=int(prefill_profile_id), user__isnull=True).first()

    back_params = {}
    if selected_date:
        back_params["date"] = selected_date
    if selected_time:
        back_params["time"] = selected_time
    if selected_service_id:
        back_params["service_id"] = selected_service_id
    if selected_professional_id:
        back_params["professional_id"] = selected_professional_id
    if week:
        back_params["week"] = week
    if status:
        back_params["status"] = status
    if q:
        back_params["q"] = q
    back_to_clients_url = reverse("professional_clients")
    if back_params:
        back_to_clients_url = f"{back_to_clients_url}?{urlencode(back_params)}"
    active_tab = "profile"
    user_create_errors = {}
    user_create_data = {
        "username": "",
        "password": "",
        "password_confirm": "",
    }

    if request.method == "POST":
        post_data = request.POST.copy()
        if post_data.get("postal_code_1") or post_data.get("postal_code_2"):
            cp1 = (post_data.get("postal_code_1") or "").strip()
            cp2 = (post_data.get("postal_code_2") or "").strip()
            post_data["postal_code"] = f"{cp1}-{cp2}" if cp1 and cp2 else ""
        existing_profile = client_profile if is_edit else prefill_profile
        form = StaffClientCreateForm(
            post_data,
            request.FILES,
            existing_user=client_user,
            existing_profile=existing_profile,
        )
        action = (post_data.get("action") or "").strip()
        handled_create_user = is_edit and action == "create_user_account"

        if handled_create_user:
            active_tab = "password"
            user_create_data = {
                "username": (post_data.get("username") or "").strip(),
                "password": (post_data.get("password") or "").strip(),
                "password_confirm": (post_data.get("password_confirm") or "").strip(),
            }
            can_upgrade_linked_user = bool(client_user and not client_user.has_usable_password())
            if client_user and not can_upgrade_linked_user:
                user_create_errors["non_field"] = "Este cliente já tem utilizador associado."
            else:
                username_input = user_create_data["username"]
                email = (post_data.get("email") or "").strip().lower()
                password = user_create_data["password"]
                password_confirm = user_create_data["password_confirm"]
                existing_user = client_user if can_upgrade_linked_user else None

                if not email:
                    user_create_errors["email"] = "Para criar utilizador, o email é obrigatório."
                else:
                    try:
                        validate_email(email)
                    except DjangoValidationError:
                        user_create_errors["email"] = "Indica um email válido."
                    else:
                        email_qs = User.objects.filter(email__iexact=email)
                        if existing_user:
                            email_qs = email_qs.exclude(pk=existing_user.pk)
                        if email_qs.exists():
                            user_create_errors["email"] = "Este email já está registado."

                if not username_input:
                    user_create_errors["username"] = "Indica um username."
                else:
                    username_qs = User.objects.filter(username__iexact=username_input)
                    if existing_user:
                        username_qs = username_qs.exclude(pk=existing_user.pk)
                    if username_qs.exists():
                        user_create_errors["username"] = "Este username já está registado."

                if not password:
                    user_create_errors["password"] = "Indica uma password."
                if not password_confirm:
                    user_create_errors["password_confirm"] = "Confirma a password."
                if password and password_confirm and password != password_confirm:
                    user_create_errors["password_confirm"] = "As passwords não coincidem."

                if password and password_confirm and password == password_confirm and "password_confirm" not in user_create_errors:
                    temp_user = User(
                        username=username_input or "",
                        email=email or "",
                        first_name=(client_profile.full_name or "").strip(),
                    )
                    try:
                        validate_password(password, user=temp_user)
                    except DjangoValidationError as exc:
                        user_create_errors["password"] = " ".join(exc.messages)

                if not user_create_errors:
                    if existing_user:
                        user = existing_user
                        user.username = username_input
                        user.email = email
                        user.first_name = (client_profile.full_name or "").strip()
                        user.set_password(password)
                        user.save(update_fields=["username", "email", "first_name", "password"])
                    else:
                        user = User.objects.create_user(
                            username=username_input,
                            email=email,
                            password=password,
                        )
                        user.first_name = (client_profile.full_name or "").strip()
                        user.save(update_fields=["first_name"])
                    group, _ = Group.objects.get_or_create(name="Cliente")
                    user.groups.add(group)

                    if client_profile.user_id != user.id:
                        client_profile.user = user
                        client_profile.updated_by = request.user
                        client_profile.save(update_fields=["user", "updated_by"])
                    client_user = user

                    portal_url = (getattr(settings, "CLIENT_PORTAL_URL", "") or "").strip() or "https://marcacoes.fisio-up.pt"
                    if not portal_url.startswith(("http://", "https://")):
                        portal_url = f"https://{portal_url.lstrip('/')}"

                    sent = send_templated_email(
                        to_email=email,
                        subject="Criação de acesso à plataforma de marcações Fisio-UP",
                        template_html="emails/client_user_created_by_clinic.html",
                        template_txt="emails/client_user_created_by_clinic.txt",
                        context={
                            "client_name": client_profile.full_name or user.username,
                            "username": username_input,
                            "password": password,
                            "portal_url": portal_url,
                        },
                        event="client_user_created",
                    )
                    if sent:
                        messages.success(request, "Utilizador criado e email enviado ao cliente.")
                    else:
                        messages.warning(request, "Utilizador criado, mas não foi possível enviar o email.")
                    return redirect(request.get_full_path())

        if not handled_create_user and form.is_valid():
            full_name = form.cleaned_data["full_name"]
            nif = form.cleaned_data["nif"]
            username_input = form.cleaned_data.get("username", "")
            email = form.cleaned_data["email"]
            password = form.cleaned_data.get("password") or ""
            phone = form.cleaned_data.get("phone", "")
            address_line1 = form.cleaned_data.get("address_line1", "")
            address_line2 = form.cleaned_data.get("address_line2", "")
            postal_code = form.cleaned_data.get("postal_code", "")
            postal_designation = form.cleaned_data.get("postal_designation", "")
            city = form.cleaned_data.get("city", "")
            district = form.cleaned_data.get("district", "")
            county = form.cleaned_data.get("county", "")
            locality = form.cleaned_data.get("locality", "")
            country = form.cleaned_data.get("country", "")
            partner = form.cleaned_data.get("partner")
            discount_type = form.cleaned_data.get("discount_type") or "none"
            discount_percent = form.cleaned_data.get("discount_percent")
            discount_amount = form.cleaned_data.get("discount_amount")
            discount_label = form.cleaned_data.get("discount_label", "")
            profile_photo = form.cleaned_data.get("profile_photo")
            clinical_allergies = form.cleaned_data.get("clinical_allergies", "")
            clinical_conditions = form.cleaned_data.get("clinical_conditions", "")
            clinical_notes = form.cleaned_data.get("clinical_notes", "")

            if is_edit:
                if nif and ClientProfile.objects.filter(nif=nif).exclude(pk=client_profile.pk).exists():
                    form.add_error("nif", "Já existe outro cliente com este NIF.")
                target_profile = client_profile
            else:
                target_profile = None
                if nif:
                    target_profile = ClientProfile.objects.filter(nif=nif).first()
                    if target_profile and target_profile.user_id:
                        form.add_error("nif", "Já existe uma conta associada a este NIF.")
                if (
                    (selected_date or selected_time or selected_service_id or selected_professional_id or week)
                    and not password
                    and not return_to_quick_modal
                ):
                    form.add_error(None, "Para marcar diretamente é necessário definir uma password.")

            if not form.errors:
                user = client_user or (target_profile.user if target_profile else None)
                if password:
                    if not user:
                        username_base = (username_input or nif).strip()
                        if not username_base:
                            username_base = f"cliente{timezone.now().strftime('%Y%m%d%H%M%S')}"
                        username = username_base
                        suffix = 1
                        while User.objects.filter(username__iexact=username).exists():
                            suffix += 1
                            username = f"{username_base}{suffix}"
                        user = User.objects.create_user(
                            username=username,
                            email=email or "",
                            password=password,
                        )
                        group, _ = Group.objects.get_or_create(name="Cliente")
                        user.groups.add(group)
                    else:
                        user.set_password(password)

                if user:
                    updated_fields = []
                    if username_input and user.username != username_input:
                        user.username = username_input
                        updated_fields.append("username")
                    if email and user.email != email:
                        user.email = email
                        updated_fields.append("email")
                    if user.first_name != full_name:
                        user.first_name = full_name
                        updated_fields.append("first_name")
                    if password:
                        updated_fields.append("password")
                    if updated_fields:
                        user.save()

                if not target_profile:
                    target_profile = ClientProfile(created_by=request.user)

                previous_pricing_signature = (
                    target_profile.partner_id,
                    target_profile.discount_type,
                    target_profile.discount_percent,
                    target_profile.discount_amount,
                    target_profile.discount_label,
                ) if target_profile.pk else None
                next_discount_percent = discount_percent if discount_type == "percent" else None
                next_discount_amount = discount_amount if discount_type == "fixed" else None
                next_pricing_signature = (
                    partner.id if partner else None,
                    discount_type,
                    next_discount_percent,
                    next_discount_amount,
                    discount_label,
                )
                pricing_fields_changed = previous_pricing_signature != next_pricing_signature

                target_profile.full_name = full_name
                target_profile.nif = nif
                target_profile.phone = phone
                target_profile.address_line1 = address_line1
                target_profile.address_line2 = address_line2
                target_profile.postal_code = postal_code
                target_profile.postal_designation = postal_designation
                target_profile.city = city
                target_profile.district = district
                target_profile.county = county
                target_profile.locality = locality
                target_profile.country = country
                target_profile.partner = partner
                target_profile.discount_type = discount_type
                target_profile.discount_percent = next_discount_percent
                target_profile.discount_amount = next_discount_amount
                target_profile.discount_label = discount_label
                if profile_photo:
                    target_profile.profile_photo = profile_photo
                if user:
                    target_profile.user = user
                target_profile.registration_status = "approved"
                target_profile.require_complete_profile = True
                target_profile.updated_by = request.user
                target_profile.save()
                if pricing_fields_changed:
                    recalculate_upcoming_appointment_prices(target_profile)

                record, _ = ClinicalRecord.objects.get_or_create(
                    client=target_profile,
                    defaults={"updated_by": request.user},
                )
                record.allergies = clinical_allergies or ""
                record.conditions = clinical_conditions or ""
                record.notes = clinical_notes or ""
                record.updated_by = request.user
                record.save()

                if not is_edit and (selected_date or selected_time or selected_service_id or selected_professional_id or week):
                    if return_to_quick_modal:
                        params = {"quick_open": "1", "return_to": "calendar_quick_modal"}
                        if selected_date:
                            params["date"] = selected_date
                        if selected_time:
                            params["time"] = selected_time
                        if selected_service_id:
                            params["service_id"] = selected_service_id
                        if selected_professional_id:
                            params["professional_id"] = selected_professional_id
                        if week:
                            params["week"] = week
                        if status:
                            params["status"] = status
                        if q:
                            params["q"] = q
                        if target_profile and target_profile.id:
                            params["quick_client_profile_id"] = target_profile.id
                        if user and user.id:
                            params["quick_client_user_id"] = user.id
                        if target_profile and target_profile.full_name:
                            params["quick_client_label"] = target_profile.full_name
                        return redirect(f"{reverse('professional_calendar')}?{urlencode(params)}")

                if not is_edit and user and (selected_date or selected_time or selected_service_id or selected_professional_id or week):
                    params = {"client_id": user.id}
                    if selected_date:
                        params["date"] = selected_date
                    if selected_time:
                        params["time"] = selected_time
                    if selected_service_id:
                        params["service_id"] = selected_service_id
                    if selected_professional_id:
                        params["professional_id"] = selected_professional_id
                    if week:
                        params["week"] = week
                    if status:
                        params["status"] = status
                    if q:
                        params["q"] = q
                    return redirect(f"{reverse('professional_book')}?{urlencode(params)}")

                messages.success(
                    request,
                    "Cliente atualizado com sucesso." if is_edit else "Cliente criado com sucesso.",
                )
                if is_edit and return_to and not return_to_quick_modal:
                    return redirect(return_to)
                return redirect(back_to_clients_url)
        elif not handled_create_user:
            if form.errors.get("password") or form.errors.get("password_confirm") or form.errors.get("username"):
                active_tab = "password"
            elif (
                form.errors.get("partner")
                or form.errors.get("discount_type")
                or form.errors.get("discount_percent")
                or form.errors.get("discount_amount")
                or form.errors.get("discount_label")
            ):
                active_tab = "partners_discounts"
    else:
        initial = {}
        if is_edit and client_profile:
            initial = {
                "full_name": client_profile.full_name,
                "nif": client_profile.nif,
                "phone": client_profile.phone,
                "email": client_user.email if client_user else "",
                "username": client_user.username if client_user else "",
                "address_line1": client_profile.address_line1,
                "address_line2": client_profile.address_line2,
                "postal_code": client_profile.postal_code,
                "postal_designation": client_profile.postal_designation,
                "city": client_profile.city,
                "district": client_profile.district,
                "county": client_profile.county,
                "locality": client_profile.locality,
                "country": client_profile.country,
                "partner": client_profile.partner_id,
                "discount_type": client_profile.discount_type,
                "discount_percent": client_profile.discount_percent,
                "discount_amount": client_profile.discount_amount,
                "discount_label": client_profile.discount_label,
            }
            if clinical_record:
                initial["clinical_allergies"] = clinical_record.allergies
                initial["clinical_conditions"] = clinical_record.conditions
                initial["clinical_notes"] = clinical_record.notes
        elif prefill_profile:
            initial = {
                "full_name": prefill_profile.full_name,
                "nif": prefill_profile.nif,
                "phone": prefill_profile.phone,
                "email": prefill_profile.user.email if prefill_profile.user else "",
                "username": prefill_profile.user.username if prefill_profile.user else "",
                "address_line1": prefill_profile.address_line1,
                "address_line2": prefill_profile.address_line2,
                "postal_code": prefill_profile.postal_code,
                "postal_designation": prefill_profile.postal_designation,
                "city": prefill_profile.city,
                "district": prefill_profile.district,
                "county": prefill_profile.county,
                "locality": prefill_profile.locality,
                "country": prefill_profile.country,
                "partner": prefill_profile.partner_id,
                "discount_type": prefill_profile.discount_type,
                "discount_percent": prefill_profile.discount_percent,
                "discount_amount": prefill_profile.discount_amount,
                "discount_label": prefill_profile.discount_label,
            }
        existing_profile = client_profile if is_edit else prefill_profile
        form = StaffClientCreateForm(
            initial=initial,
            existing_user=client_user,
            existing_profile=existing_profile,
        )

    client_has_user = bool(client_user and client_user.has_usable_password())

    return render(
        request,
        "core/prof_customer_form.html",
        {
            "form": form,
            "active_tab": active_tab,
            "selected_date": selected_date,
            "selected_time": selected_time,
            "selected_service_id": selected_service_id,
            "selected_professional_id": selected_professional_id,
            "week": week,
            "prefill_profile_id": prefill_profile.id if prefill_profile else "",
            "status": status,
            "q": q,
            "return_to": return_to,
            "back_to_clients_url": back_to_clients_url,
            "movements": [],
            "history_items": [],
            "is_edit": is_edit,
            "client_id": client_id or "",
            "client_profile": client_profile,
            "client_user": client_user,
            "client_has_user": client_has_user,
            "user_create_errors": user_create_errors,
            "user_create_data": user_create_data,
            "clinical_record": clinical_record,
        },
    )


def professional_create_client_view(request):
    return professional_customer_form_view(request)


def professional_profile_view(request):
    prof = Professional.objects.filter(user=request.user).first()
    if not prof and not request.user.is_staff:
        return HttpResponseForbidden("Acesso restrito a profissionais.")
    if not prof:
        return HttpResponseForbidden("Profissional não encontrado.")

    password_form = PasswordChangeForm(user=request.user)
    password_form.fields["old_password"].widget.attrs.pop("autofocus", None)
    active_tab = "profile"

    if request.method == "POST":
        if request.POST.get("action") == "change_password":
            active_tab = "password"
            password_form = PasswordChangeForm(user=request.user, data=request.POST)
            password_form.fields["old_password"].widget.attrs.pop("autofocus", None)
            if password_form.is_valid():
                password_form.save()
                messages.success(request, "Password atualizada com sucesso.")
                return redirect("professional_profile")
            form = ProfessionalProfileForm(user=request.user, professional=prof)
        else:
            form = ProfessionalProfileForm(request.POST, request.FILES, user=request.user, professional=prof)
            if form.is_valid():
                form.save(user=request.user, professional=prof)
                messages.success(request, "Perfil atualizado com sucesso.")
                return redirect("professional_profile")
            for name in form.errors:
                if name in form.fields:
                    field = form.fields[name]
                    css_class = field.widget.attrs.get("class", "")
                    if "is-invalid" not in css_class:
                        field.widget.attrs["class"] = f"{css_class} is-invalid".strip()
    else:
        form = ProfessionalProfileForm(user=request.user, professional=prof)

    return render(
        request,
        "core/professional_profile.html",
        {
            "form": form,
            "professional": prof,
            "password_form": password_form,
            "active_tab": active_tab,
        },
    )


def client_record_view(request, client_id):
    """
    Ficha do cliente (apenas profissionais).
    URL usa o ID do ClientProfile (não é o User id).
    """
    client = get_object_or_404(ClientProfile, id=client_id)

    clinical, _ = ClinicalRecord.objects.get_or_create(
        client=client,
        defaults={"updated_by": request.user},
    )

    # tenta descobrir o profissional pelo user logado
    logged_prof = Professional.objects.filter(user=request.user).first()

    professionals = Professional.objects.select_related("user").all().order_by("user__username")
    services = Service.objects.all().order_by("name")
    message = ""

    selected_service_id = (request.GET.get("service_id") or "").strip()
    selected_professional_id = (request.GET.get("professional_id") or "").strip()
    selected_date = (request.GET.get("date") or "").strip()

    slots = []

    # ✅ Filtra profissionais pelo serviço escolhido (para o dropdown)
    if selected_service_id:
        professionals = professionals.filter(services__id=selected_service_id).distinct()

    # ✅ Se o profissional selecionado não estiver na lista filtrada, limpa seleção
    if selected_professional_id and not professionals.filter(id=selected_professional_id).exists():
        selected_professional_id = ""   

    # Calcular slots (apenas se houver prof + serviço + data)
    if active_prof and selected_service_id and selected_date:
        try:
            service_obj = get_object_or_404(Service, id=selected_service_id)
            date_obj = datetime.strptime(selected_date, "%Y-%m-%d").date()
            slots = _get_slots(active_prof, date_obj, step_minutes=service_obj.duration_minutes)
        except Exception:
            message = "Erro a calcular horários. Confirma serviço e data."

    if request.method == "POST":
        action = request.POST.get("action", "")

        # Se não houver active_prof (ex: staff sem Professional), tenta vir do POST
        if active_prof is None:
            pid = request.POST.get("professional_id", "").strip()
            if pid:
                active_prof = get_object_or_404(Professional, id=pid)

        if action == "add_treatment":
            service_id = request.POST.get("service_id", "").strip()
            date_str = request.POST.get("date", "").strip()
            time_str = request.POST.get("time", "").strip()
            notes = request.POST.get("notes", "").strip()

            if active_prof is None:
                message = "Tens de escolher um profissional."
            elif not (service_id and date_str and time_str):
                message = "Serviço, data e hora são obrigatórios."
            else:
                service_obj = get_object_or_404(Service, id=service_id)
                date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
                now_t = timezone.localtime().time()
                today = timezone.localdate()

                if date_obj < today:
                    message = "Não podes marcar no passado."
                elif date_obj == today:
                    time_obj = datetime.strptime(time_str, "%H:%M").time()
                    if time_obj <= now_t:
                        message = "Este horário já passou."

                if message:
                    valid_slots = _get_slots(active_prof, date_obj, step_minutes=service_obj.duration_minutes)
                    selected_service_id = service_id
                    selected_date = date_str
                    selected_professional_id = str(active_prof.id)
                    slots = valid_slots
                else:
                    valid_slots = _get_slots(active_prof, date_obj, step_minutes=service_obj.duration_minutes)
                    if time_str not in valid_slots:
                        message = "Hora inválida ou já ocupada. Escolhe uma das horas disponíveis."
                        selected_service_id = service_id
                        selected_date = date_str
                        selected_professional_id = str(active_prof.id)
                        slots = valid_slots
                    else:
                        time_obj = datetime.strptime(time_str, "%H:%M").time()

                        treatment = TreatmentRecord.objects.create(
                            client=client,
                            professional=active_prof,
                            appointment=None,
                            service_name=service_obj.name,
                            date=date_obj,
                            time=time_obj,
                            notes=notes,
                            created_by=request.user,
                            updated_by=request.user,
                        )
                        log_audit_event(
                            category="treatment_record",
                            action="create",
                            request=request,
                            instance=treatment,
                            source="client_record",
                            message="Registo de tratamento criado.",
                            after=_treatment_record_audit_snapshot(treatment),
                        )

                        return redirect(
                            request.path + f"?professional_id={active_prof.id}&service_id={service_id}&date={date_str}"
                        )

        elif action == "update_notes":
            tr_id = request.POST.get("treatment_id")
            notes = request.POST.get("notes", "").strip()

            treatment = get_object_or_404(TreatmentRecord, id=tr_id, client=client)
            before = _treatment_record_audit_snapshot(treatment)
            treatment.notes = notes
            treatment.updated_by = request.user
            treatment.save()
            log_audit_event(
                category="treatment_record",
                action="update_notes",
                request=request,
                instance=treatment,
                source="client_record",
                message="Notas do tratamento atualizadas.",
                before=before,
                after=_treatment_record_audit_snapshot(treatment),
            )

            if active_prof:
                return redirect(request.path + f"?professional_id={active_prof.id}")
            return redirect(request.path)
        elif action == "update_clinical":
            before = _clinical_record_audit_snapshot(clinical)
            clinical.allergies = (request.POST.get("allergies") or "").strip()
            clinical.conditions = (request.POST.get("conditions") or "").strip()
            clinical.notes = (request.POST.get("notes") or "").strip()
            clinical.updated_by = request.user
            clinical.save()
            log_audit_event(
                category="clinical_record",
                action="update",
                request=request,
                instance=clinical,
                source="client_record",
                message="Ficha clínica atualizada.",
                before=before,
                after=_clinical_record_audit_snapshot(clinical),
            )
            if active_prof:
                return redirect(request.path + f"?professional_id={active_prof.id}&service_id={selected_service_id}&date={selected_date}")
            return redirect(request.path)

    return render(
        request,
        "core/client_record.html",
        {
            "client": client,
            "clinical": clinical,
            "treatments": treatments,
            "services": services,
            "professionals": professionals,
            "selected_professional_id": selected_professional_id or (str(active_prof.id) if active_prof else ""),
            "slots": slots,
            "selected_service_id": selected_service_id,
            "selected_date": selected_date,
            "message": message,
        },
    )
