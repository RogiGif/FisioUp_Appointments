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
from core.utils.revenue import (
    get_revenue_queryset,
    compute_trend,
    month_range,
    week_range,
    day_range,
    month_start,
)
from core.services.subcontracting import sync_subcontractor_payout
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
from core.views.common import apply_bulk_appointment_action

@login_required
def mark_notifications_read_view(request):
    request.session["notifications_last_read"] = timezone.localtime().isoformat()
    next_url = (request.POST.get("next") or request.GET.get("next") or "").strip()
    if not next_url:
        next_url = request.META.get("HTTP_REFERER") or reverse("client_calendar")
    if not url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        next_url = reverse("client_calendar")
    return redirect(next_url)


@login_required
def professional_notifications_view(request):
    is_admin = is_admin_role(request.user)
    professional = (
        Professional.objects
        .select_related("user")
        .filter(user=request.user)
        .first()
    )
    if not (professional or is_admin):
        return HttpResponseForbidden("Acesso apenas para profissionais/admin.")

    now = timezone.localtime()
    today = now.date()
    now_t = now.time()

    base_qs = (
        Appointment.objects
        .select_related("client", "client__client_profile", "service", "professional", "professional__user")
        .exclude(status=Appointment.STATUS_CANCELLED)
    )
    if professional and not is_admin:
        base_qs = base_qs.filter(professional=professional)

    if request.method == "POST":
        action = (request.POST.get("bulk_action") or "").strip()
        selected_ids = []
        for raw in request.POST.getlist("appointment_ids"):
            try:
                selected_ids.append(int(raw))
            except (TypeError, ValueError):
                continue

        valid_actions = {
            "mark_completed_and_paid_selected",
            "mark_no_show_selected",
            "mark_completed_selected",
            "mark_in_debt_selected",
        }
        if action not in valid_actions:
            messages.error(request, "Ação inválida.")
            return redirect("professional_notifications")
        if not selected_ids:
            messages.error(request, "Seleciona pelo menos uma marcação.")
            return redirect("professional_notifications")

        selected_qs = (
            base_qs.filter(id__in=selected_ids)
            .select_related("client", "service", "professional", "professional__user")
            .order_by("date", "time", "id")
        )

        result = apply_bulk_appointment_action(
            appointments=selected_qs,
            action=action,
            actor=request.user,
            today=today,
            now_t=now_t,
            audit_source="notificacoes",
        )
        settings_obj = clinic_settings()
        for transition in result["status_transitions"]:
            sync_subcontractor_payout(transition["appointment"], actor=request.user)

        if settings_obj.notify_client_on_clinic_changes:
            for transition in result["status_transitions"]:
                appt = transition["appointment"]
                old_status = transition["old_status"]
                new_status = transition["new_status"]
                if old_status == Appointment.STATUS_PENDING and new_status == Appointment.STATUS_SCHEDULED:
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

        skipped_note = ""
        if result["skipped_future"] or result["skipped_locked"] or result["skipped_unpaid"]:
            skipped_note = (
                " ("
                f"{result['skipped_future']} no futuro, "
                f"{result['skipped_locked']} em estados fechados, "
                f"{result['skipped_unpaid']} sem pagamento ignoradas"
                ")"
            )
        if action == "mark_completed_and_paid_selected":
            messages.success(
                request,
                (
                    f"Marcadas {result['status_changed']} marcações como concluídas e "
                    f"{result['paid_changed']} como pagas{skipped_note}."
                ),
            )
        elif action == "mark_no_show_selected":
            messages.success(request, f"Marcadas {result['status_changed']} marcações como falta{skipped_note}.")
        elif action == "mark_completed_selected":
            messages.success(request, f"Marcadas {result['status_changed']} marcações como concluídas{skipped_note}.")
        elif action == "mark_in_debt_selected":
            messages.success(request, f"Marcadas {result['status_changed']} marcações como em dívida{skipped_note}.")
        else:
            messages.success(request, f"Atualizadas {result['status_changed']} marcações{skipped_note}.")
        return redirect("professional_notifications")

    pending_items = list(
        base_qs.filter(
            status=Appointment.STATUS_PENDING,
        ).filter(
            Q(date__gt=today) | Q(date=today, time__gte=now_t)
        ).order_by("date", "time", "id")
    )

    review_items = list(
        base_qs.exclude(status=Appointment.STATUS_IN_DEBT).filter(
            Q(date__lt=today) | Q(date=today, time__lt=now_t)
        ).filter(
            status__in=[
                Appointment.STATUS_PENDING,
                Appointment.STATUS_SCHEDULED,
                Appointment.STATUS_AWAITING_VALIDATION,
            ]
        ).order_by("-date", "-time", "-id")
    )
    review_status_counts = {
        "awaiting_validation": sum(1 for appt in review_items if appt.status == Appointment.STATUS_AWAITING_VALIDATION),
        "scheduled": sum(1 for appt in review_items if appt.status == Appointment.STATUS_SCHEDULED),
        "pending_confirmation": sum(1 for appt in review_items if appt.status == Appointment.STATUS_PENDING),
    }

    return render(
        request,
        "core/professional_notifications.html",
        {
            "professional": professional,
            "show_all_professionals": is_admin,
            "pending_items": pending_items,
            "review_items": review_items,
            "pending_count": len(pending_items),
            "review_count": len(review_items),
            "review_status_counts": review_status_counts,
        },
    )
