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
from django.contrib.auth.forms import PasswordChangeForm, SetPasswordForm
from django.contrib.auth import views as auth_views
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Q, Count, Sum, Exists, OuterRef, F
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
    ClinicEmailSettingsForm,
    WeeklyScheduleForm,
    WeeklyWorkingBlockFormSet,
    WeeklyBreakBlockFormSet,
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
    WeeklySchedule,
    WeeklyWorkingBlock,
    WeeklyBreakBlock,
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
    Product,
    StockMovement,
    ContentPost,
    SubcontractorPaymentLine,
    ClinicSettings,
)

from core.views.common import *


def test_duralux(request):
    return render(request, "core/base_duralux.html")


def backoffice_dashboard_view(request):
    today = timezone.localdate()
    revenue_qs = get_revenue_queryset(request.user)
    can_view_all = can_view_all_calendar(request.user)
    prof = None
    if not can_view_all:
        prof = Professional.objects.filter(user=request.user).first()

    appt_metrics_qs = Appointment.objects.all()
    agenda_base_qs = Appointment.objects.select_related("client", "service", "professional", "professional__user")
    if prof:
        appt_metrics_qs = appt_metrics_qs.filter(professional=prof)
        agenda_base_qs = agenda_base_qs.filter(professional=prof)

    period = (request.GET.get("period") or "month").strip().lower()
    if period not in {"day", "week", "month"}:
        period = "month"

    today_start, today_end = day_range(today)
    yesterday_start, yesterday_end = day_range(today - timedelta(days=1))
    week_start_date, week_end_date = week_range(today)
    prev_week_start = week_start_date - timedelta(days=7)
    prev_week_end = week_start_date
    month_start_date, month_end_date = month_range(today)
    prev_month_end = month_start_date
    prev_month_start = month_start(prev_month_end - timedelta(days=1))

    if period == "day":
        period_start, period_end = today_start, today_end
        prev_period_start, prev_period_end = yesterday_start, yesterday_end
    elif period == "week":
        period_start, period_end = week_start_date, week_end_date
        prev_period_start, prev_period_end = prev_week_start, prev_week_end
    else:
        period_start, period_end = month_start_date, month_end_date
        prev_period_start, prev_period_end = prev_month_start, prev_month_end

    cache_scope = "all" if can_view_all else f"prof-{prof.id if prof else request.user.id}"
    cache_key = f"dashboard:metrics:v2:{cache_scope}:{today.isoformat()}:{period}"
    metrics = cache.get(cache_key)
    if metrics is None:
        # Cache only counters/charts for a short period. Agenda list stays uncached.
        revenue_totals = revenue_qs.aggregate(
            revenue_today=Coalesce(
                Sum("final_price", filter=Q(date__gte=today_start, date__lt=today_end)),
                Decimal("0.00"),
            ),
            revenue_yesterday=Coalesce(
                Sum("final_price", filter=Q(date__gte=yesterday_start, date__lt=yesterday_end)),
                Decimal("0.00"),
            ),
            revenue_period=Coalesce(
                Sum("final_price", filter=Q(date__gte=period_start, date__lt=period_end)),
                Decimal("0.00"),
            ),
            revenue_prev_period=Coalesce(
                Sum("final_price", filter=Q(date__gte=prev_period_start, date__lt=prev_period_end)),
                Decimal("0.00"),
            ),
        )
        revenue_today = revenue_totals["revenue_today"] or Decimal("0.00")
        revenue_yesterday = revenue_totals["revenue_yesterday"] or Decimal("0.00")
        revenue_period = revenue_totals["revenue_period"] or Decimal("0.00")
        revenue_prev_period = revenue_totals["revenue_prev_period"] or Decimal("0.00")

        appt_totals = appt_metrics_qs.aggregate(
            appointments_today=Count("id", filter=Q(date=today)),
            appointments_yesterday=Count("id", filter=Q(date=today - timedelta(days=1))),
            appointments_period=Count("id", filter=Q(date__gte=period_start, date__lt=period_end)),
            scheduled_count=Count(
                "id",
                filter=Q(
                    date__gte=period_start,
                    date__lt=period_end,
                    status=Appointment.STATUS_SCHEDULED,
                ),
            ),
            completed_count=Count(
                "id",
                filter=Q(
                    date__gte=period_start,
                    date__lt=period_end,
                    status=Appointment.STATUS_COMPLETED,
                ),
            ),
            cancelled_count=Count(
                "id",
                filter=Q(
                    date__gte=period_start,
                    date__lt=period_end,
                    status=Appointment.STATUS_CANCELLED,
                ),
            ),
            prev_total=Count("id", filter=Q(date__gte=prev_period_start, date__lt=prev_period_end)),
            prev_completed=Count(
                "id",
                filter=Q(
                    date__gte=prev_period_start,
                    date__lt=prev_period_end,
                    status=Appointment.STATUS_COMPLETED,
                ),
            ),
            weekly_appointments_count=Count(
                "id",
                filter=Q(date__gte=week_start_date, date__lt=week_end_date)
                & ~Q(status=Appointment.STATUS_CANCELLED),
            ),
            monthly_appointments_count=Count(
                "id",
                filter=Q(date__gte=month_start_date, date__lt=month_end_date)
                & ~Q(status=Appointment.STATUS_CANCELLED),
            ),
        )
        appointments_today = appt_totals["appointments_today"] or 0
        appointments_yesterday = appt_totals["appointments_yesterday"] or 0
        appointments_period = appt_totals["appointments_period"] or 0
        scheduled_count = appt_totals["scheduled_count"] or 0
        completed_count = appt_totals["completed_count"] or 0
        cancelled_count = appt_totals["cancelled_count"] or 0
        prev_total = appt_totals["prev_total"] or 0
        prev_completed = appt_totals["prev_completed"] or 0
        weekly_appointments_count = appt_totals["weekly_appointments_count"] or 0
        monthly_appointments_count = appt_totals["monthly_appointments_count"] or 0

        def _rate(completed: int, total: int) -> Decimal:
            if not total:
                return Decimal("0.0")
            return (Decimal(completed) / Decimal(total)) * Decimal("100")

        completed_rate = _rate(completed_count, appointments_period)
        prev_completed_rate = _rate(prev_completed, prev_total)
        if prev_completed_rate > 0:
            completed_rate_delta = ((completed_rate - prev_completed_rate) / prev_completed_rate) * Decimal("100")
            completed_rate_delta_display = f"{completed_rate_delta:.1f}%"
            if completed_rate_delta > 0:
                completed_rate_delta_display = f"+{completed_rate_delta_display}"
        else:
            completed_rate_delta_display = "—"

        if period == "day":
            delta_label = "dia anterior"
        elif period == "week":
            delta_label = "semana anterior"
        else:
            delta_label = "mês anterior"

        total_pct = 100
        scheduled_pct = round((scheduled_count / appointments_period) * 100) if appointments_period else 0
        completed_pct = round((completed_count / appointments_period) * 100) if appointments_period else 0
        cancelled_pct = round((cancelled_count / appointments_period) * 100) if appointments_period else 0

        chart_labels = []
        chart_values = []
        if period == "day":
            hourly = {h: 0 for h in range(24)}
            for time_value in appt_metrics_qs.filter(date=today).values_list("time", flat=True):
                if time_value:
                    hourly[time_value.hour] += 1
            for hour in range(24):
                chart_labels.append(f"{hour:02d}:00")
                chart_values.append(hourly[hour])
        else:
            # Single grouped query avoids daily count loops.
            daily_counts = dict(
                appt_metrics_qs
                .filter(date__gte=period_start, date__lt=period_end)
                .values("date")
                .annotate(total=Count("id"))
                .values_list("date", "total")
            )
            day = period_start
            while day < period_end:
                chart_labels.append(day.strftime("%d %b"))
                chart_values.append(daily_counts.get(day, 0))
                day += timedelta(days=1)

        client_qs = ClientProfile.objects.all()
        if prof:
            client_qs = client_qs.filter(appointments__professional=prof).distinct()
        period_start_dt = timezone.make_aware(datetime.combine(period_start, datetime.min.time()))
        period_end_dt = timezone.make_aware(datetime.combine(period_end, datetime.min.time()))
        prev_start_dt = timezone.make_aware(datetime.combine(prev_period_start, datetime.min.time()))
        prev_end_dt = timezone.make_aware(datetime.combine(prev_period_end, datetime.min.time()))
        today_start_dt = timezone.make_aware(datetime.combine(today, datetime.min.time()))
        today_end_dt = timezone.make_aware(datetime.combine(today + timedelta(days=1), datetime.min.time()))

        client_totals = client_qs.aggregate(
            new_clients_today=Count("id", filter=Q(created_at__gte=today_start_dt, created_at__lt=today_end_dt)),
            new_clients_period=Count("id", filter=Q(created_at__gte=period_start_dt, created_at__lt=period_end_dt)),
            new_clients_prev=Count("id", filter=Q(created_at__gte=prev_start_dt, created_at__lt=prev_end_dt)),
        )
        new_clients_today = client_totals["new_clients_today"] or 0
        new_clients_period = client_totals["new_clients_period"] or 0
        new_clients_prev = client_totals["new_clients_prev"] or 0

        new_clients_trend = compute_trend(Decimal(new_clients_period), Decimal(new_clients_prev))
        revenue_trend_today = compute_trend(revenue_today, revenue_yesterday)
        revenue_trend_period = compute_trend(revenue_period, revenue_prev_period)
        appointments_trend = compute_trend(Decimal(appointments_today), Decimal(appointments_yesterday))

        appointments_by_service = list(
            appt_metrics_qs
            .filter(date__gte=period_start, date__lt=period_end)
            .exclude(status=Appointment.STATUS_CANCELLED)
            .values("service__name")
            .annotate(total=Count("id"))
            .order_by("-total")
        )

        weekly_counts_by_date = dict(
            appt_metrics_qs
            .filter(date__gte=week_start_date, date__lt=week_end_date)
            .exclude(status=Appointment.STATUS_CANCELLED)
            .values("date")
            .annotate(total=Count("id"))
            .values_list("date", "total")
        )
        week_days = [week_start_date + timedelta(days=i) for i in range(7)]
        weekly_chart_series = [weekly_counts_by_date.get(day, 0) for day in week_days]

        month_counts_by_date = dict(
            appt_metrics_qs
            .filter(date__gte=month_start_date, date__lt=month_end_date)
            .exclude(status=Appointment.STATUS_CANCELLED)
            .values("date")
            .annotate(total=Count("id"))
            .values_list("date", "total")
        )
        month_weeks = []
        current = month_start_date
        while current < month_end_date:
            week_end = min(current + timedelta(days=7), month_end_date)
            bucket_total = sum(
                count
                for day, count in month_counts_by_date.items()
                if current <= day < week_end
            )
            month_weeks.append(bucket_total)
            current = week_end

        stock_alert_count = (
            Product.objects
            .filter(is_active=True)
            .annotate(
                stock=Coalesce(
                    Sum("movements__quantity_base", filter=Q(movements__is_void=False)),
                    Decimal("0.00"),
                )
            )
            .filter(stock__lte=F("min_stock_alert"))
            .count()
        )

        metrics = {
            "revenue_today": revenue_today,
            "revenue_trend_today": revenue_trend_today,
            "appointments_today": appointments_today,
            "appointments_trend": appointments_trend,
            "new_clients_period": new_clients_period,
            "new_clients_trend": new_clients_trend,
            "new_clients_today": new_clients_today,
            "revenue_period": revenue_period,
            "revenue_trend_period": revenue_trend_period,
            "appointments_period": appointments_period,
            "appointments_by_service": appointments_by_service,
            "scheduled_count": scheduled_count,
            "completed_count": completed_count,
            "cancelled_count": cancelled_count,
            "completed_rate_delta_display": completed_rate_delta_display,
            "delta_label": delta_label,
            "total_pct": total_pct,
            "scheduled_pct": scheduled_pct,
            "completed_pct": completed_pct,
            "cancelled_pct": cancelled_pct,
            "chart_labels": chart_labels,
            "chart_values": chart_values,
            "weekly_appointments_count": weekly_appointments_count,
            "monthly_appointments_count": monthly_appointments_count,
            "stock_alert_count": stock_alert_count,
            "weekly_chart_series": weekly_chart_series,
            "monthly_chart_series": month_weeks,
        }
        cache.set(cache_key, metrics, 120)

    agenda_qs = agenda_base_qs.filter(date=today).order_by("time")
    agenda_page_size = 10
    agenda_page_obj = Paginator(agenda_qs, agenda_page_size).get_page(request.GET.get("agenda_page") or 1)
    agenda_today = []
    for appt in agenda_page_obj.object_list:
        agenda_today.append(
            {
                "id": appt.id,
                "time": appt.time,
                "client": appt.client.get_full_name() or appt.client.username,
                "service": appt.service.name if appt.service else "-",
                "professional": appt.professional.user.get_full_name() or appt.professional.user.username,
                "status": appt.get_status_display(),
                "status_value": appt.status,
                "open_url": reverse("professional_appointment_detail", args=[appt.id]),
            }
        )

    return render(
        request,
        "core/backoffice/dashboard.html",
        {
            "today": today,
            "is_admin": can_view_all,
            "revenue_today": metrics["revenue_today"],
            "revenue_trend_today": metrics["revenue_trend_today"],
            "appointments_today": metrics["appointments_today"],
            "appointments_trend": metrics["appointments_trend"],
            "new_clients_period": metrics["new_clients_period"],
            "new_clients_trend": metrics["new_clients_trend"],
            "revenue_period": metrics["revenue_period"],
            "revenue_trend_period": metrics["revenue_trend_period"],
            "appointments_period": metrics["appointments_period"],
            "period": period,
            "agenda_today": agenda_today,
            "agenda_page_obj": agenda_page_obj,
            "appointments_by_service": metrics["appointments_by_service"],
            "scheduled_count": metrics["scheduled_count"],
            "completed_count": metrics["completed_count"],
            "cancelled_count": metrics["cancelled_count"],
            "completed_rate_delta_display": metrics["completed_rate_delta_display"],
            "delta_label": metrics["delta_label"],
            "total_pct": metrics["total_pct"],
            "scheduled_pct": metrics["scheduled_pct"],
            "completed_pct": metrics["completed_pct"],
            "cancelled_pct": metrics["cancelled_pct"],
            "chart_labels": metrics["chart_labels"],
            "chart_values": metrics["chart_values"],
            "kpi_revenue_today": metrics["revenue_today"],
            "kpi_appts_today": metrics["appointments_today"],
            "kpi_new_clients": metrics["new_clients_today"],
            "kpi_sales_total": metrics["revenue_period"],
            "today_agenda": agenda_today,
            "weekly_appointments_count": metrics["weekly_appointments_count"],
            "monthly_appointments_count": metrics["monthly_appointments_count"],
            "stock_alert_count": metrics["stock_alert_count"],
            "weekly_chart_series": json.dumps(metrics["weekly_chart_series"]),
            "monthly_chart_series": json.dumps(metrics["monthly_chart_series"]),
        },
    )


def backoffice_agenda_view(request):
    if not request.user.is_authenticated:
        return redirect(f"{reverse('login')}?next={request.get_full_path()}")

    view_all = can_view_all_calendar(request.user)
    prof = Professional.objects.filter(user=request.user).first()
    if not view_all and not prof:
        return HttpResponseForbidden("Acesso apenas para profissionais.")

    today = timezone.localdate()
    now_t = timezone.localtime().time()
    quick_modal_open = False
    quick_errors = []
    quick_slots = []
    quick_form = {
        "client_id": "",
        "client_label": "",
        "service_id": "",
        "professional_id": "",
        "date": "",
        "time": "",
        "symptomatology": "",
    }

    if request.method == "POST" and request.POST.get("action") == "quick_create":
        if not can_book_for_any_professional(request.user):
            return HttpResponseForbidden("Apenas receção/admin pode criar marcações aqui.")

        quick_form = {
            "client_id": (request.POST.get("client_id") or "").strip(),
            "service_id": (request.POST.get("service_id") or "").strip(),
            "professional_id": (request.POST.get("professional_id") or "").strip(),
            "date": (request.POST.get("date") or "").strip(),
            "time": (request.POST.get("time") or "").strip(),
            "symptomatology": (request.POST.get("symptomatology") or "").strip(),
            "client_label": (request.POST.get("client_label") or "").strip(),
        }

        return_to = _safe_return_to(request, request.POST.get("return_to")) or reverse("backoffice_agenda")

        if not quick_form["client_id"]:
            quick_errors.append("Seleciona um cliente.")
        if not quick_form["service_id"]:
            quick_errors.append("Seleciona um serviço.")
        if not quick_form["professional_id"]:
            quick_errors.append("Seleciona um profissional.")
        if not quick_form["date"]:
            quick_errors.append("Seleciona uma data.")
        if not quick_form["time"]:
            quick_errors.append("Seleciona uma hora.")

        client_profile = None
        client_user = None
        if quick_form["client_id"]:
            client_profile = ClientProfile.objects.select_related("user").filter(id=quick_form["client_id"]).first()
            if not client_profile:
                quick_errors.append("Cliente inválido.")
            else:
                client_user = client_profile.user
                quick_form["client_label"] = client_profile.full_name or (client_user.get_full_name() if client_user else "") or str(client_profile)
                if not client_user:
                    quick_errors.append("Este cliente ainda não tem utilizador associado.")

        service = None
        prof = None
        if quick_form["service_id"]:
            service = Service.objects.filter(id=quick_form["service_id"]).first()
            if not service:
                quick_errors.append("Serviço inválido.")

        if quick_form["professional_id"]:
            prof = Professional.objects.filter(id=quick_form["professional_id"]).first()
            if not prof:
                quick_errors.append("Profissional inválido.")

        date_obj = None
        time_obj = None
        if quick_form["date"]:
            try:
                date_obj = datetime.strptime(quick_form["date"], "%Y-%m-%d").date()
            except Exception:
                quick_errors.append("Data inválida.")
        if quick_form["time"]:
            try:
                time_obj = datetime.strptime(quick_form["time"], "%H:%M").time()
            except Exception:
                quick_errors.append("Hora inválida.")

        if service and prof:
            if not Professional.objects.filter(id=prof.id, services__id=service.id).exists():
                quick_errors.append("Profissional inválido para este serviço.")

        if service and prof and date_obj:
            if date_obj < today:
                quick_errors.append("Não podes marcar no passado.")
            elif date_obj == today and time_obj and time_obj <= timezone.localtime().time():
                quick_errors.append("Este horário já passou.")
            elif not professional_works_on_date(prof, date_obj):
                prof_days = professional_weekdays_labels(prof)
                quick_errors.append(
                    f"Este profissional não atende nesse dia. Atende: {', '.join(prof_days) or '—'}."
                )

        if service and prof and date_obj:
            quick_slots = _get_slots(prof, date_obj, step_minutes=service.duration_minutes)

        if service and prof and date_obj and time_obj and not quick_errors:
            if _is_slot_blocked(prof, date_obj, time_obj):
                quick_errors.append("Este horário está indisponível.")
            elif quick_form["time"] not in quick_slots:
                quick_errors.append("Esse horário já não está disponível.")

        if quick_errors:
            quick_modal_open = True
        else:
            try:
                with transaction.atomic():
                    pricing = compute_pricing(service, client_profile)
                    appt = Appointment.objects.create(
                        client=client_user,
                        professional=prof,
                        service=service,
                        date=date_obj,
                        time=time_obj,
                        symptomatology=quick_form["symptomatology"],
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
                log_appt(AppointmentLog.ACTION_CREATED, appt, request.user, note="Criada no backoffice")
                messages.success(request, "Marcação criada com sucesso.")
                return redirect(return_to)
            except IntegrityError:
                quick_errors.append("Esse horário já não está disponível.")
                quick_modal_open = True
    date_str = (request.GET.get("date") or "").strip()
    tab = (request.GET.get("tab") or "all").strip()
    if tab not in ("all", "pending", "review"):
        tab = "all"
    view_mode = (request.GET.get("view_mode") or "day").strip()
    professional_id = (request.GET.get("professional_id") or "").strip()
    service_id = (request.GET.get("service_id") or "").strip()
    status = (request.GET.get("status") or "").strip()
    kind = (request.GET.get("type") or "all").strip()
    q = (request.GET.get("q") or "").strip()
    client_id = (request.GET.get("client_id") or "").strip()
    per_page = request.GET.get("per_page") or "10"

    try:
        per_page = int(per_page)
    except (TypeError, ValueError):
        per_page = 10
    if per_page not in (10, 25, 50):
        per_page = 10

    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else today
    except Exception:
        date_obj = today

    if view_mode == "week":
        start_date = date_obj - timedelta(days=date_obj.weekday())
        end_date = start_date + timedelta(days=6)
    else:
        start_date = date_obj
        end_date = date_obj

    if not view_all:
        professional_id = str(prof.id)

    appointments = Appointment.objects.select_related(
        "client",
        "client__client_profile",
        "service",
        "professional",
        "professional__user",
    ).prefetch_related(
        "consumptions__product",
    ).filter(date__range=(start_date, end_date))

    if professional_id:
        appointments = appointments.filter(professional_id=professional_id)
    if service_id:
        appointments = appointments.filter(service_id=service_id)
    if client_id:
        appointments = appointments.filter(client__client_profile__id=client_id)
    if q:
        appointments = apply_terms_filter(
            appointments,
            q,
            [
                "client__username__icontains",
                "client__first_name__icontains",
                "client__last_name__icontains",
                "client__client_profile__full_name__icontains",
                "client__client_profile__phone__icontains",
                "client__client_profile__nif__icontains",
            ],
        )

    appointments_base = appointments

    group_sessions = GroupSession.objects.select_related(
        "service",
        "professional",
        "professional__user",
    ).filter(date__range=(start_date, end_date))

    if professional_id:
        group_sessions = group_sessions.filter(professional_id=professional_id)
    if service_id:
        group_sessions = group_sessions.filter(service_id=service_id)

    group_sessions_base = group_sessions

    if status and tab == "all":
        appointments = appointments.filter(status=status)
        group_sessions = group_sessions.filter(status=status)

    if tab == "pending":
        appointments = appointments.filter(
            status=Appointment.STATUS_PENDING,
        ).filter(
            Q(date__gt=today) | Q(date=today, time__gte=now_t)
        )
        kind = "appointment"
        status = ""
        group_sessions = GroupSession.objects.none()
    elif tab == "review":
        appointments = appointments.exclude(status=Appointment.STATUS_CANCELLED).filter(
            Q(date__lt=today) | Q(date=today, time__lt=now_t)
        ).filter(
            Q(status=Appointment.STATUS_SCHEDULED)
            | Q(summary__isnull=True)
            | Q(summary="")
            | Q(is_paid=False)
        )
        kind = "appointment"
        status = ""
        group_sessions = GroupSession.objects.none()

    # Counts for tabs (always based on appointment filters)
    pending_count = appointments_base.filter(
        status=Appointment.STATUS_PENDING,
    ).filter(
        Q(date__gt=today) | Q(date=today, time__gte=now_t)
    ).count()
    review_count = appointments_base.exclude(status=Appointment.STATUS_CANCELLED).filter(
        Q(date__lt=today) | Q(date=today, time__lt=now_t)
    ).filter(
        Q(status=Appointment.STATUS_SCHEDULED)
        | Q(summary__isnull=True)
        | Q(summary="")
        | Q(is_paid=False)
    ).count()
    if kind == "appointment":
        all_count = appointments.count()
    elif kind == "group":
        all_count = group_sessions.count()
    else:
        all_count = appointments.count() + group_sessions.count()

    if kind == "appointment":
        group_sessions = GroupSession.objects.none()
    elif kind == "group":
        appointments = Appointment.objects.none()

    group_sessions = group_sessions.annotate(
        active_count=Count("enrolments", filter=Q(enrolments__status__in=group_booked_statuses()))
    )

    return_to = request.get_full_path()
    items = []
    def format_decimal(value):
        try:
            raw = f"{Decimal(value):f}"
        except Exception:
            return str(value)
        raw = raw.rstrip("0").rstrip(".")
        return raw if raw else "0"

    for appt in appointments:
        client_profile = getattr(appt.client, "client_profile", None)
        client_name = (
            (client_profile.full_name if client_profile and client_profile.full_name else "")
            or appt.client.get_full_name()
            or appt.client.username
        )
        consumptions = list(appt.consumptions.all())
        if consumptions:
            parts = []
            for cons in consumptions:
                product = cons.product
                if not product:
                    continue
                unit = product.get_unit_base_display()
                qty = format_decimal(cons.quantity_base)
                parts.append(f"{product.name} ({qty} {unit})")
            consumptions_label = ", ".join(parts) if parts else "—"
        else:
            consumptions_label = "—"
        items.append(
            AgendaItem(
                kind="appointment",
                date=appt.date,
                time=appt.time,
                service_name=appt.service.name if appt.service else "-",
                professional_name=appt.professional.user.get_full_name() or appt.professional.user.username,
                client_label=client_name,
                consumptions_label=consumptions_label,
                status_label=appt.get_status_display(),
                status_raw=appt.status,
                price_label=str(appt.final_price) if appt.final_price is not None else "—",
                open_url=f"{reverse('professional_appointment_detail', args=[appt.id])}?return_to={urlencode({'return_to': return_to})[10:]}",
                cancel_url=f"{reverse('backoffice_cancel_appointment', args=[appt.id])}?return_to={urlencode({'return_to': return_to})[10:]}",
                complete_url=f"{reverse('backoffice_complete_appointment', args=[appt.id])}?return_to={urlencode({'return_to': return_to})[10:]}",
                reschedule_url=f"{reverse('reschedule_appointment', args=[appt.id])}?return_to={urlencode({'return_to': return_to})[10:]}",
            )
        )

    for session in group_sessions:
        professional_name = "—"
        if session.professional:
            professional_name = session.professional.user.get_full_name() or session.professional.user.username
        items.append(
            AgendaItem(
                kind="group",
                date=session.date,
                time=session.time,
                service_name=session.service.name if session.service else "-",
                professional_name=professional_name,
                client_label=f"{session.active_count}/{session.capacity_value} inscritos",
                consumptions_label="—",
                status_label=session.get_status_display(),
                status_raw=session.status,
                price_label="—",
                open_url=reverse("group_session_detail_admin", args=[session.id]),
                cancel_url=None,
                complete_url=None,
                reschedule_url=None,
            )
        )

    items.sort(key=lambda item: (item.date, item.time, item.kind))
    paginator = Paginator(items, per_page)
    page_obj = paginator.get_page(request.GET.get("page") or 1)

    return render(
        request,
        "backoffice/agenda.html",
        {
            "items": page_obj.object_list,
            "page_obj": page_obj,
            "paginator": paginator,
            "today": today,
            "date": date_obj,
            "view_mode": view_mode,
            "professional_id": professional_id,
            "service_id": service_id,
            "status": status,
            "tab": tab,
            "type": kind,
            "tab_counts": {
                "all": all_count,
                "pending": pending_count,
                "review": review_count,
            },
            "q": q,
            "per_page": per_page,
            "professionals": Professional.objects.select_related("user").order_by("user__username") if view_all else [prof],
            "services": Service.objects.order_by("name"),
            "return_to": return_to,
            "can_quick_create": can_book_for_any_professional(request.user),
            "quick_modal_open": quick_modal_open,
            "quick_errors": quick_errors,
            "quick_form": quick_form,
            "quick_slots": quick_slots,
        },
    )


def backoffice_faturacao_view(request):
    today = timezone.localdate()
    per_page = request.GET.get("per_page") or "10"
    try:
        per_page = int(per_page)
    except (TypeError, ValueError):
        per_page = 10
    if per_page not in (5, 10, 15, 25, 50):
        per_page = 10

    start_param = (request.GET.get("start") or "").strip()
    end_param = (request.GET.get("end") or "").strip()
    professional_id = (request.GET.get("professional_id") or "").strip()
    service_id = (request.GET.get("service_id") or "").strip()

    if start_param:
        try:
            start_date = datetime.strptime(start_param, "%Y-%m-%d").date()
        except ValueError:
            start_date = today - timedelta(days=29)
    else:
        start_date = today - timedelta(days=29)

    if end_param:
        try:
            end_date = datetime.strptime(end_param, "%Y-%m-%d").date()
        except ValueError:
            end_date = today
    else:
        end_date = today

    revenue_qs = get_revenue_queryset(request.user)
    if professional_id and can_view_all_calendar(request.user):
        revenue_qs = revenue_qs.filter(professional_id=professional_id)
    if service_id:
        revenue_qs = revenue_qs.filter(service_id=service_id)

    revenue_qs = revenue_qs.filter(date__gte=start_date, date__lte=end_date)

    total_period = (
        revenue_qs.aggregate(total=Coalesce(Sum("final_price"), Decimal("0.00")))
        .get("total")
        or Decimal("0.00")
    )
    total_count = revenue_qs.count()
    days_span = (end_date - start_date).days + 1
    avg_per_day = total_period / Decimal(days_span) if days_span > 0 else Decimal("0.00")

    daily_rows = (
        revenue_qs.values("date")
        .annotate(total=Coalesce(Sum("final_price"), Decimal("0.00")), count=Count("id"))
        .order_by("-date")
    )

    if request.GET.get("export") == "1":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = "attachment; filename=faturacao.csv"
        writer = csv.writer(response)
        writer.writerow(["Data", "Total", "Marcações"])
        for row in daily_rows:
            writer.writerow([row["date"].strftime("%Y-%m-%d"), row["total"], row["count"]])
        return response

    paginator = Paginator(daily_rows, per_page)
    page_obj = paginator.get_page(request.GET.get("page") or 1)

    return render(
        request,
        "backoffice/faturacao.html",
        {
            "page_obj": page_obj,
            "paginator": paginator,
            "per_page": per_page,
            "start": start_date.strftime("%Y-%m-%d"),
            "end": end_date.strftime("%Y-%m-%d"),
            "professional_id": professional_id,
            "service_id": service_id,
            "total_period": total_period,
            "avg_per_day": avg_per_day,
            "total_count": total_count,
            "professionals": Professional.objects.select_related("user").order_by("user__username"),
            "services": Service.objects.order_by("name"),
            "return_to": request.get_full_path(),
        },
    )


def _parse_date_param(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _subcontractor_lines_queryset(request):
    qs = (
        SubcontractorPaymentLine.objects
        .select_related("professional", "professional__user", "client", "service", "appointment")
    )
    start_param = (request.GET.get("start") or "").strip()
    end_param = (request.GET.get("end") or "").strip()
    professional_id = (request.GET.get("professional_id") or "").strip()
    service_id = (request.GET.get("service_id") or "").strip()
    status = (request.GET.get("status") or "").strip()

    start_date = _parse_date_param(start_param)
    end_date = _parse_date_param(end_param)
    if start_date:
        qs = qs.filter(appointment_date__gte=start_date)
    if end_date:
        qs = qs.filter(appointment_date__lte=end_date)
    if professional_id:
        qs = qs.filter(professional_id=professional_id)
    if service_id:
        qs = qs.filter(service_id=service_id)
    if status in {
        SubcontractorPaymentLine.STATUS_UNPAID,
        SubcontractorPaymentLine.STATUS_PAID,
        SubcontractorPaymentLine.STATUS_VOID,
    }:
        qs = qs.filter(status=status)

    return qs, {
        "start": start_param,
        "end": end_param,
        "professional_id": professional_id,
        "service_id": service_id,
        "status": status,
    }


def backoffice_subcontractors_view(request):
    if not can_access_backoffice(request.user):
        return HttpResponseForbidden("Acesso apenas para backoffice.")

    qs, filters = _subcontractor_lines_queryset(request)

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        ids = request.POST.getlist("line_ids")
        if not ids:
            messages.error(request, "Seleciona pelo menos uma linha.")
            return redirect(request.get_full_path())
        selected = SubcontractorPaymentLine.objects.filter(id__in=ids)
        if action == "mark_paid":
            now = timezone.now()
            selected.update(
                status=SubcontractorPaymentLine.STATUS_PAID,
                paid_at=now,
                paid_by=request.user,
            )
            messages.success(request, "Linhas marcadas como pagas.")
        elif action == "mark_unpaid":
            selected.update(
                status=SubcontractorPaymentLine.STATUS_UNPAID,
                paid_at=None,
                paid_by=None,
            )
            messages.success(request, "Linhas marcadas como em aberto.")
        else:
            messages.error(request, "Ação inválida.")
        return redirect(request.get_full_path())

    per_page = request.GET.get("per_page") or "10"
    try:
        per_page = int(per_page)
    except (TypeError, ValueError):
        per_page = 10
    per_page_options = [10, 25, 50]
    if per_page not in per_page_options:
        per_page = 10

    totals_base = qs.exclude(status=SubcontractorPaymentLine.STATUS_VOID)
    total_paid = (
        totals_base.filter(status=SubcontractorPaymentLine.STATUS_PAID)
        .aggregate(total=Coalesce(Sum("payable_amount"), Decimal("0.00")))
        .get("total")
        or Decimal("0.00")
    )
    total_unpaid = (
        totals_base.filter(status=SubcontractorPaymentLine.STATUS_UNPAID)
        .aggregate(total=Coalesce(Sum("payable_amount"), Decimal("0.00")))
        .get("total")
        or Decimal("0.00")
    )

    paginator = Paginator(qs.order_by("-appointment_date", "-appointment_time"), per_page)
    page_obj = paginator.get_page(request.GET.get("page") or 1)
    pagination_params = request.GET.copy()
    pagination_params.pop("page", None)
    export_qs = pagination_params.urlencode()

    return render(
        request,
        "backoffice/subcontractors_payments_list.html",
        {
            "lines": page_obj.object_list,
            "page_obj": page_obj,
            "paginator": paginator,
            "per_page": per_page,
            "per_page_options": per_page_options,
            "filters": filters,
            "export_qs": export_qs,
            "pagination_qs": export_qs,
            "total_paid": total_paid,
            "total_unpaid": total_unpaid,
            "total_open": total_unpaid,
            "professionals": Professional.objects.select_related("user").order_by("user__username"),
            "services": Service.objects.order_by("name"),
            "status_choices": SubcontractorPaymentLine.STATUS_CHOICES,
            "return_to": request.get_full_path(),
        },
    )


def backoffice_subcontractors_export_view(request):
    if not can_access_backoffice(request.user):
        return HttpResponseForbidden("Acesso apenas para backoffice.")

    qs, filters = _subcontractor_lines_queryset(request)
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = "attachment; filename=subcontratados.csv"
    writer = csv.writer(response)
    writer.writerow(
        [
            "Data",
            "Hora",
            "Utente",
            "Serviço",
            "Profissional",
            "Preço final",
            "Percentagem",
            "Valor a pagar",
            "Pago",
            "Pago em",
        ]
    )
    for line in qs.order_by("appointment_date", "appointment_time"):
        writer.writerow(
            [
                line.appointment_date.strftime("%Y-%m-%d"),
                line.appointment_time.strftime("%H:%M") if line.appointment_time else "",
                line.client.full_name if line.client else "",
                line.service.name if line.service else "",
                line.professional.user.get_full_name() if line.professional else "",
                line.gross_amount,
                line.percentage,
                line.payable_amount,
                "sim" if line.status == SubcontractorPaymentLine.STATUS_PAID else "não",
                line.paid_at.strftime("%Y-%m-%d %H:%M") if line.paid_at else "",
            ]
        )
    return response


def backoffice_clients_quick_view(request):
    return redirect("professional_clients")
    q = (request.GET.get("q") or "").strip()
    per_page = request.GET.get("per_page") or "10"
    try:
        per_page = int(per_page)
    except (TypeError, ValueError):
        per_page = 10
    if per_page not in (10, 25, 50):
        per_page = 10

    qs = ClientProfile.objects.select_related("user").order_by("full_name")
    if q:
        qs = apply_terms_filter(
            qs,
            q,
            [
                "full_name__icontains",
                "nif__icontains",
                "phone__icontains",
                "user__username__icontains",
            ],
        )

    paginator = Paginator(qs, per_page)
    page_obj = paginator.get_page(request.GET.get("page") or 1)

    return render(
        request,
        "backoffice/clients_quick.html",
        {
            "clients": page_obj.object_list,
            "page_obj": page_obj,
            "paginator": paginator,
            "q": q,
            "per_page": per_page,
            "return_to": request.get_full_path(),
        },
    )


def backoffice_api_clients_search(request):
    if not can_book_for_any_professional(request.user):
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


def backoffice_api_professionals_by_service(request):
    if not can_book_for_any_professional(request.user):
        return HttpResponseForbidden("Acesso negado.")
    service_id = (request.GET.get("service_id") or "").strip()
    if not service_id:
        return JsonResponse({"results": []})
    qs = (
        Professional.objects.select_related("user")
        .filter(services__id=service_id)
        .distinct()
        .order_by("user__username")
    )
    results = [
        {"id": p.id, "label": p.user.get_full_name() or p.user.username}
        for p in qs
    ]
    return JsonResponse({"results": results})


def backoffice_api_slots(request):
    if not can_book_for_any_professional(request.user):
        return HttpResponseForbidden("Acesso negado.")
    service_id = (request.GET.get("service_id") or "").strip()
    professional_id = (request.GET.get("professional_id") or "").strip()
    date_str = (request.GET.get("date") or "").strip()

    if not (service_id and professional_id and date_str):
        return JsonResponse({"ok": False, "slots": [], "message": "Dados incompletos."})

    service = Service.objects.filter(id=service_id).first()
    prof = Professional.objects.filter(id=professional_id).first()
    if not service or not prof:
        return JsonResponse({"ok": False, "slots": [], "message": "Dados inválidos."})

    if not Professional.objects.filter(id=prof.id, services__id=service.id).exists():
        return JsonResponse({"ok": False, "slots": [], "message": "Profissional inválido para este serviço."})

    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        return JsonResponse({"ok": False, "slots": [], "message": "Data inválida."})

    today = timezone.localdate()
    if date_obj < today:
        return JsonResponse({"ok": False, "slots": [], "message": "Não podes marcar no passado."})

    if not professional_works_on_date(prof, date_obj):
        days = professional_weekdays_labels(prof)
        return JsonResponse(
            {
                "ok": False,
                "slots": [],
                "message": f"Este profissional não atende nesse dia. Atende: {', '.join(days) or '—'}.",
                "days": days,
            }
        )

    slots = _get_slots(prof, date_obj, step_minutes=service.duration_minutes)
    if not slots:
        return JsonResponse({"ok": False, "slots": [], "message": "Sem horários disponíveis."})
    return JsonResponse({"ok": True, "slots": slots, "message": "", "days": professional_weekdays_labels(prof)})


def healthcheck_view(request):
    from django.db import connection

    status = "ok"
    db_ok = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        status = "error"
        db_ok = False

    return JsonResponse({"status": status, "db": db_ok})


def test_duralux_view(request):
    return render(request, "core/test_duralux.html")


def backoffice_cancel_appointment_view(request, appointment_id):
    if not can_view_all_calendar(request.user):
        return HttpResponseForbidden("Acesso apenas para receção/admin.")

    appt = get_object_or_404(Appointment, id=appointment_id)
    if appt.status == "completed":
        return HttpResponseForbidden("Não podes cancelar uma marcação concluída.")

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
        )
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
                        "manage_url": request.build_absolute_uri(reverse("my_appointments")),
                    },
                    event="cancel_clinic",
                )
            else:
                log_email_skip("cancel_clinic", "Marcação cancelada", "Cliente sem email", "")

    messages.success(request, "Marcação cancelada.")
    return_to = _safe_return_to(request, request.GET.get("return_to"))
    if return_to:
        return redirect(return_to)
    return redirect("backoffice_agenda")


def backoffice_complete_appointment_view(request, appointment_id):
    if not can_view_all_calendar(request.user):
        return HttpResponseForbidden("Acesso apenas para receção/admin.")

    appt = get_object_or_404(Appointment, id=appointment_id)
    return_to = _safe_return_to(request, request.GET.get("return_to"))
    params = {}
    if return_to:
        params["return_to"] = return_to
    target = reverse("professional_appointment_detail", args=[appt.id])
    if params:
        return redirect(f"{target}?{urlencode(params)}")
    return redirect(target)


@dataclass
class AgendaItem:
    kind: str
    date: datetime.date
    time: datetime.time
    service_name: str
    professional_name: str
    client_label: str
    consumptions_label: str
    status_label: str
    status_raw: str
    price_label: str
    open_url: str
    cancel_url: str | None
    complete_url: str | None
    reschedule_url: str | None


def backoffice_professionals_list_view(request):
    q = (request.GET.get("q") or "").strip()
    employment = (request.GET.get("employment") or "").strip()
    per_page = request.GET.get("per_page") or "5"
    try:
        per_page = int(per_page)
    except (TypeError, ValueError):
        per_page = 5
    if per_page not in (5, 10, 15, 25, 50):
        per_page = 5

    qs = Professional.objects.select_related("user").prefetch_related("services").order_by(
        "user__username"
    )
    if q:
        qs = apply_terms_filter(
            qs,
            q,
            [
                "user__first_name__icontains",
                "user__last_name__icontains",
                "user__username__icontains",
                "speciality__icontains",
            ],
        )
    if employment == "independent":
        qs = qs.filter(is_independent=True)
    elif employment == "employee":
        qs = qs.filter(is_independent=False)

    paginator = Paginator(qs, per_page)
    page_obj = paginator.get_page(request.GET.get("page") or 1)

    query_params = {}
    if q:
        query_params["q"] = q
    if employment:
        query_params["employment"] = employment
    if per_page:
        query_params["per_page"] = per_page

    return render(
        request,
        "backoffice/professionals_list.html",
        {
            "professionals": page_obj.object_list,
            "page_obj": page_obj,
            "paginator": paginator,
            "q": q,
            "employment": employment,
            "per_page": per_page,
            "query_prefix": urlencode(query_params),
            "return_to": request.get_full_path(),
        },
    )


def _send_professional_welcome_email(request, professional):
    user = professional.user
    if not user or not user.email:
        log_email_skip("professional_welcome", "Acesso à plataforma", "Profissional sem email", "")
        return False, "Profissional sem email."

    login_url = request.build_absolute_uri(reverse("login"))
    context = {
        "professional": professional,
        "user": user,
        "login_url": login_url,
    }
    sent = send_templated_email(
        to_email=user.email,
        subject="Acesso à plataforma de marcações Fisio-UP",
        template_html="emails/professional_welcome.html",
        template_txt="emails/professional_welcome.txt",
        context=context,
        event="professional_welcome",
    )
    if sent:
        return True, "Email enviado."
    return False, "Falha ao enviar email."


def backoffice_professional_create_view(request):
    return_to = _safe_return_to(request, request.POST.get("return_to") or request.GET.get("return_to"))
    if request.method == "POST":
        form = BackofficeProfessionalForm(request.POST, request.FILES)
        form.fields.pop("user", None)
        if form.is_valid():
            professional = form.save()
            send_email = bool(request.POST.get("send_welcome_email"))
            if send_email:
                ok, msg = _send_professional_welcome_email(request, professional)
                if ok:
                    messages.success(request, msg)
                else:
                    messages.warning(request, msg)
            messages.success(request, "Profissional criado com sucesso.")
            if return_to:
                return redirect(return_to)
            return redirect("backoffice_professionals")
    else:
        form = BackofficeProfessionalForm()
    return render(
        request,
        "backoffice/professional_form.html",
        {"form": form, "title": "Novo profissional", "return_to": return_to, "is_edit": False},
    )


def backoffice_professional_edit_view(request, professional_id):
    professional = get_object_or_404(Professional, id=professional_id)
    return_to = _safe_return_to(request, request.POST.get("return_to") or request.GET.get("return_to"))
    active_tab = "profile"
    password_form = SetPasswordForm(user=professional.user)
    for field in password_form.fields.values():
        field.widget.attrs.setdefault("class", "form-control")
    if request.method == "POST":
        if request.POST.get("action") == "set_password":
            active_tab = "password"
            password_form = SetPasswordForm(user=professional.user, data=request.POST)
            for field in password_form.fields.values():
                field.widget.attrs.setdefault("class", "form-control")
            if password_form.is_valid():
                password_form.save()
                messages.success(request, "Password atualizada com sucesso.")
                redirect_url = reverse("backoffice_professional_edit", args=[professional.id])
                if return_to:
                    redirect_url = f"{redirect_url}?{urlencode({'return_to': return_to})}"
                return redirect(redirect_url)
            form = BackofficeProfessionalForm(instance=professional)
        else:
            form = BackofficeProfessionalForm(request.POST, request.FILES, instance=professional)
            if form.is_valid():
                form.save()
                messages.success(request, "Profissional atualizado.")
                if return_to:
                    return redirect(return_to)
                return redirect("backoffice_professionals")
    else:
        form = BackofficeProfessionalForm(instance=professional)
    return render(
        request,
        "backoffice/professional_form.html",
        {
            "form": form,
            "title": "Editar profissional",
            "return_to": return_to,
            "is_edit": True,
            "password_form": password_form,
            "active_tab": active_tab,
        },
    )


@backoffice_required
def backoffice_weekly_schedules_list_view(request):
    if not is_admin_role(request.user):
        return HttpResponseForbidden("Acesso reservado a administradores.")

    professionals = Professional.objects.select_related("user").order_by("user__username")
    schedules = WeeklySchedule.objects.select_related("professional").all()
    schedule_map = {s.professional_id: s for s in schedules}

    block_counts = {
        row["weekly_schedule__professional_id"]: row["total"]
        for row in WeeklyWorkingBlock.objects.values("weekly_schedule__professional_id").annotate(total=Count("id"))
    }

    rows = []
    for prof in professionals:
        schedule = schedule_map.get(prof.id)
        rows.append(
            {
                "professional": prof,
                "schedule": schedule,
                "has_schedule": bool(schedule),
                "is_active": bool(schedule and schedule.is_active),
                "block_count": block_counts.get(prof.id, 0),
            }
        )

    return render(
        request,
        "backoffice/weekly_schedules_list.html",
        {
            "rows": rows,
            "return_to": request.get_full_path(),
        },
    )


@backoffice_required
def backoffice_weekly_schedule_edit_view(request, professional_id):
    if not is_admin_role(request.user):
        return HttpResponseForbidden("Acesso reservado a administradores.")

    professional = get_object_or_404(Professional, id=professional_id)
    professionals = Professional.objects.select_related("user").order_by("user__username")
    schedule, _ = WeeklySchedule.objects.get_or_create(
        professional=professional,
        defaults={"timezone": "Europe/Lisbon", "is_active": True},
    )

    if request.method == "POST":
        schedule_form = WeeklyScheduleForm(request.POST, instance=schedule)
        work_formset = WeeklyWorkingBlockFormSet(request.POST, instance=schedule, prefix="work")
        break_formset = WeeklyBreakBlockFormSet(request.POST, instance=schedule, prefix="break")
        if schedule_form.is_valid() and work_formset.is_valid() and break_formset.is_valid():
            schedule_form.save()
            work_formset.save()
            break_formset.save()
            messages.success(request, "Horário semanal atualizado.")
            return redirect("backoffice_weekly_schedules")
    else:
        schedule_form = WeeklyScheduleForm(instance=schedule)
        work_formset = WeeklyWorkingBlockFormSet(instance=schedule, prefix="work")
        break_formset = WeeklyBreakBlockFormSet(instance=schedule, prefix="break")

    weekdays = [
        (0, "Segunda-feira"),
        (1, "Terça-feira"),
        (2, "Quarta-feira"),
        (3, "Quinta-feira"),
        (4, "Sexta-feira"),
        (5, "Sábado"),
        (6, "Domingo"),
    ]

    def _weekday_from_form(form):
        if form.instance and getattr(form.instance, "weekday", None) is not None:
            return form.instance.weekday
        raw = form.data.get(f"{form.prefix}-weekday")
        if raw not in (None, ""):
            try:
                return int(raw)
            except ValueError:
                return None
        initial = form.initial.get("weekday")
        if initial is not None:
            return initial
        return None

    work_by_day = {day: [] for day, _ in weekdays}
    for form in work_formset.forms:
        day = _weekday_from_form(form)
        if day in work_by_day:
            work_by_day[day].append(form)

    break_by_day = {day: [] for day, _ in weekdays}
    for form in break_formset.forms:
        day = _weekday_from_form(form)
        if day in break_by_day:
            break_by_day[day].append(form)

    day_rows = [
        {
            "day": day,
            "label": label,
            "work_forms": work_by_day.get(day, []),
            "break_forms": break_by_day.get(day, []),
        }
        for day, label in weekdays
    ]

    return render(
        request,
        "backoffice/weekly_schedule_edit.html",
        {
            "professional": professional,
            "professionals": professionals,
            "schedule_form": schedule_form,
            "work_formset": work_formset,
            "break_formset": break_formset,
            "day_rows": day_rows,
            "return_to": request.get_full_path(),
        },
    )


def backoffice_partners_list_view(request):
    q = (request.GET.get("q") or "").strip()
    per_page = request.GET.get("per_page") or "5"
    try:
        per_page = int(per_page)
    except (TypeError, ValueError):
        per_page = 5
    if per_page not in (5, 10, 15, 25, 50):
        per_page = 5

    qs = Partner.objects.all().order_by("name")
    if q:
        qs = apply_terms_filter(qs, q, ["name__icontains"])

    paginator = Paginator(qs, per_page)
    page_obj = paginator.get_page(request.GET.get("page") or 1)

    return render(
        request,
        "backoffice/partners_list.html",
        {
            "partners": page_obj.object_list,
            "page_obj": page_obj,
            "paginator": paginator,
            "q": q,
            "per_page": per_page,
            "return_to": request.get_full_path(),
        },
    )


def backoffice_partner_create_view(request):
    return_to = _safe_return_to(request, request.POST.get("return_to") or request.GET.get("return_to"))
    if request.method == "POST":
        form = BackofficePartnerForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Parceria criada com sucesso.")
            if return_to:
                return redirect(return_to)
            return redirect("backoffice_partners")
    else:
        form = BackofficePartnerForm()
    return render(
        request,
        "backoffice/partner_form.html",
        {"form": form, "title": "Nova parceria", "return_to": return_to},
    )


def backoffice_partner_edit_view(request, partner_id):
    partner = get_object_or_404(Partner, id=partner_id)
    return_to = _safe_return_to(request, request.POST.get("return_to") or request.GET.get("return_to"))
    if request.method == "POST":
        form = BackofficePartnerForm(request.POST, instance=partner)
        if form.is_valid():
            form.save()
            messages.success(request, "Parceria atualizada.")
            if return_to:
                return redirect(return_to)
            return redirect("backoffice_partners")
    else:
        form = BackofficePartnerForm(instance=partner)
    return render(
        request,
        "backoffice/partner_form.html",
        {"form": form, "title": "Editar parceria", "return_to": return_to},
    )


def backoffice_partner_prices_view(request):
    partners = Partner.objects.order_by("name")
    partner_id = request.GET.get("partner_id") or request.POST.get("partner_id") or ""
    selected_partner = Partner.objects.filter(id=partner_id).first() if partner_id else None
    services = Service.objects.order_by("name")

    if request.method == "POST" and selected_partner:
        partner_discount_type = selected_partner.discount_type or "none"
        for service in services:
            prefix = f"service_{service.id}"
            pricing_mode = request.POST.get(f"{prefix}_pricing_mode") or "single"
            price = (request.POST.get(f"{prefix}_price") or "").strip()
            price_first = (request.POST.get(f"{prefix}_price_first") or "").strip()
            price_followup = (request.POST.get(f"{prefix}_price_followup") or "").strip()
            discount_value_raw = (request.POST.get(f"{prefix}_discount") or "").strip()

            if not price and not price_first and not price_followup and not discount_value_raw:
                continue

            def _to_decimal(val):
                try:
                    return Decimal(val.replace(",", "."))
                except Exception:
                    return None

            price_val = _to_decimal(price)
            price_first_val = _to_decimal(price_first)
            price_followup_val = _to_decimal(price_followup)
            discount_val = _to_decimal(discount_value_raw) if discount_value_raw else None

            obj, _ = PartnerServicePrice.objects.get_or_create(
                partner=selected_partner,
                service=service,
                defaults={"price": price_val or Decimal("0.00")},
            )
            obj.pricing_mode = pricing_mode
            if pricing_mode == "single":
                if price_val is not None:
                    obj.price = price_val
                obj.price_first = None
                obj.price_followup = None
            else:
                if price_first_val is not None:
                    obj.price_first = price_first_val
                if price_followup_val is not None:
                    obj.price_followup = price_followup_val
                if obj.price is None:
                    obj.price = Decimal("0.00")

            if discount_val is not None:
                obj.discount_type = partner_discount_type
                if partner_discount_type == "percent":
                    obj.discount_percent = discount_val
                    obj.discount_amount = None
                elif partner_discount_type == "fixed":
                    obj.discount_amount = discount_val
                    obj.discount_percent = None
                else:
                    obj.discount_type = "none"
                    obj.discount_percent = None
                    obj.discount_amount = None
            else:
                obj.discount_type = "none"
                obj.discount_percent = None
                obj.discount_amount = None

            obj.full_clean()
            obj.save()

        messages.success(request, "Preços atualizados.")
        return redirect(f"{reverse('backoffice_partner_prices')}?partner_id={selected_partner.id}")

    service_rows = []
    if selected_partner:
        price_map = {
            psp.service_id: psp
            for psp in PartnerServicePrice.objects.filter(partner=selected_partner)
        }
        for service in services:
            service_rows.append({"service": service, "psp": price_map.get(service.id)})

    return render(
        request,
        "backoffice/partner_prices.html",
        {
            "partners": partners,
            "selected_partner": selected_partner,
            "services": services,
            "service_rows": service_rows,
        },
    )


@backoffice_required
def backoffice_settings_email_view(request):
    if not is_admin_role(request.user):
        return HttpResponseForbidden("Acesso reservado a administradores.")

    settings_obj = ClinicSettings.get_solo()
    form = ClinicEmailSettingsForm(request.POST or None, instance=settings_obj)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Definições de email atualizadas.")
        return redirect("backoffice_settings_email")

    return render(
        request,
        "backoffice/settings_email.html",
        {"form": form},
    )


def backoffice_clients_list_view(request):
    q = (request.GET.get("q") or "").strip()
    per_page = request.GET.get("per_page") or "5"
    try:
        per_page = int(per_page)
    except (TypeError, ValueError):
        per_page = 5
    if per_page not in (5, 10, 15, 25, 50):
        per_page = 5

    qs = ClientProfile.objects.select_related("user", "partner").order_by("full_name")
    if q:
        qs = apply_terms_filter(
            qs,
            q,
            [
                "full_name__icontains",
                "nif__icontains",
                "phone__icontains",
                "user__username__icontains",
            ],
        )

    paginator = Paginator(qs, per_page)
    page_obj = paginator.get_page(request.GET.get("page") or 1)

    return render(
        request,
        "backoffice/clients_list.html",
        {
            "clients": page_obj.object_list,
            "page_obj": page_obj,
            "paginator": paginator,
            "q": q,
            "per_page": per_page,
            "return_to": request.get_full_path(),
        },
    )


def backoffice_client_edit_view(request, client_id):
    profile = get_object_or_404(ClientProfile.objects.select_related("user"), id=client_id)
    client_user = profile.user
    return_to = _safe_return_to(request, request.POST.get("return_to") or request.GET.get("return_to"))
    if request.method == "POST":
        form = BackofficeClientProfileForm(request.POST, instance=profile)
        email = (request.POST.get("email") or "").strip().lower()
        email_error = ""
        if email:
            if " " in email or "@" not in email or "." not in email.split("@", 1)[1]:
                email_error = "Indica um email válido."
            else:
                exists = User.objects.filter(email__iexact=email)
                if client_user:
                    exists = exists.exclude(pk=client_user.pk)
                if exists.exists():
                    email_error = "Este email já está registado."

        if form.is_valid() and not email_error:
            form.save()
            if client_user:
                client_user.email = email
                client_user.save(update_fields=["email"])
            messages.success(request, "Cliente atualizado.")
            if return_to:
                return redirect(return_to)
            return redirect("backoffice_clients")
    else:
        form = BackofficeClientProfileForm(instance=profile)
        if client_user:
            form.fields["email"].initial = client_user.email
    return render(
        request,
        "backoffice/client_form.html",
        {
            "form": form,
            "title": "Editar cliente",
            "email_error": email_error if request.method == "POST" else "",
            "return_to": return_to,
        },
    )


def client_import_view(request):
    if not can_access_backoffice(request.user):
        return HttpResponseForbidden("Acesso apenas para backoffice.")

    def nif_is_valid(value: str) -> bool:
        value = "".join(ch for ch in (value or "") if ch.isdigit())
        if len(value) != 9:
            return False
        digits = [int(d) for d in value]
        total = sum(d * (9 - i) for i, d in enumerate(digits[:8]))
        check = 11 - (total % 11)
        check = 0 if check >= 10 else check
        return check == digits[8]

    selected_ids = set(request.session.get("import_selected_ids", []))
    batch_id = request.session.get("client_import_batch_id")

    def _norm(value: str) -> str:
        value = (value or "").strip().lower()
        value = "".join(
            ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch)
        )
        value = value.replace(" ", "").replace("-", "").replace("_", "")
        return value

    def get_field(row, header_map, *names):
        for name in names:
            key = header_map.get(_norm(name))
            if key and key in row:
                return (row.get(key) or "").strip()
        return ""

    def parse_csv_to_batch(upload, validate_nif):
        raw = upload.read()
        content = ""
        for enc in ("utf-8-sig", "utf-8", "cp1252", "iso-8859-1"):
            try:
                content = raw.decode(enc)
                break
            except Exception:
                continue
        if not content:
            raise ValueError("Erro a ler o ficheiro. Usa UTF-8 ou ANSI (Windows-1252).")

        sample = content[:2048]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;")
        except Exception:
            dialect = csv.get_dialect("excel")

        lines = [line for line in content.splitlines() if line.strip()]
        if not lines:
            raise ValueError("CSV vazio.")

        start_idx = 0
        for i, line in enumerate(lines[:10]):
            if "nome" in line.lower() and ("contribuinte" in line.lower() or "nif" in line.lower()):
                start_idx = i
                break
        cleaned = "\n".join(lines[start_idx:])
        reader = csv.DictReader(io.StringIO(cleaned), dialect=dialect)

        header_map = {}
        if reader.fieldnames:
            for h in reader.fieldnames:
                header_map[_norm(h)] = h

        rows = [r for r in reader if any((v or "").strip() for v in r.values())]
        nif_counts = {}
        parsed = []
        for row in rows:
            nif_raw = get_field(row, header_map, "nif", "vat", "nif_cliente", "tax_id", "contribuinte")
            nif = "".join(ch for ch in nif_raw if ch.isdigit())
            if nif:
                nif_counts[nif] = nif_counts.get(nif, 0) + 1
            parsed.append((row, nif_raw, nif))

        batch = ClientImportBatch.objects.create(
            uploaded_by=request.user,
            original_filename=upload.name,
            validate_nif=validate_nif,
        )
        bulk = []
        for idx, (row, nif_raw, nif) in enumerate(parsed, start=1):
            is_valid_nif = bool(nif) and nif_is_valid(nif)
            full_name = get_field(row, header_map, "nome", "name", "full_name")
            phone = get_field(row, header_map, "telefone", "phone", "mobile", "telemovel")
            email = get_field(row, header_map, "email", "e-mail")
            address = get_field(row, header_map, "morada", "address", "address_line1")
            postal_code = get_field(row, header_map, "codigo postal", "codigo_postal", "postal_code", "cp", "zip")
            city = get_field(row, header_map, "localidade", "city")
            county = get_field(row, header_map, "concelho", "county")
            district = get_field(row, header_map, "distrito", "district")
            duplicate_in_file = bool(nif) and nif_counts.get(nif, 0) > 1
            exists_in_db = bool(nif) and ClientProfile.objects.filter(nif=nif).exists()
            missing_email = not bool(email)
            bulk.append(
                ClientImportRow(
                    batch=batch,
                    row_key=idx,
                    full_name=full_name or "",
                    nif=nif or nif_raw or "",
                    phone=phone or "",
                    email=email or "",
                    address_line1=address or "",
                    postal_code=postal_code or "",
                    city=city or "",
                    county=county or "",
                    district=district or "",
                    valid_vat=is_valid_nif if validate_nif else bool(nif),
                    missing_email=missing_email,
                    duplicate_in_file=duplicate_in_file,
                    exists_in_db=exists_in_db,
                )
            )
        ClientImportRow.objects.bulk_create(bulk, batch_size=500)
        return batch

    if request.method == "POST":
        blocked, retry_after = check_rate_limit(
            request,
            name="backoffice_import_ip_minute",
            limit=20,
            window=60,
            by_ip=True,
        )
        if blocked:
            messages.error(request, "Demasiadas tentativas. Tenta novamente em alguns minutos.")
            response = redirect("client_import")
            response.status_code = 429
            response["Retry-After"] = str(retry_after)
            return response
        action = (request.POST.get("action") or "").strip()
        validate_nif = (request.POST.get("validate_nif") or "0") == "1"
        if action == "preview":
            upload = request.FILES.get("csv_file")
            if not upload:
                messages.error(request, "Seleciona um ficheiro CSV.")
                return redirect("client_import")
            try:
                batch = parse_csv_to_batch(upload, validate_nif)
            except ValueError as exc:
                messages.error(request, str(exc))
                return redirect("client_import")
            request.session["client_import_batch_id"] = batch.id
            request.session["import_selected_ids"] = []
            return redirect("client_import")

        batch_id = request.POST.get("batch_id") or batch_id
        if not batch_id:
            return redirect("client_import")
        batch = ClientImportBatch.objects.filter(id=batch_id).first()
        if not batch:
            return redirect("client_import")

        q = (request.POST.get("q") or "").strip()
        only_missing_email = request.POST.get("only_missing_email") == "1"
        only_valid_vat = request.POST.get("only_valid_vat") == "1"
        only_duplicates = request.POST.get("only_duplicates") == "1"
        page_size = request.POST.get("page_size") or "5"
        redirect_qs = urlencode(
            {
                "q": q,
                "page_size": page_size,
                "only_missing_email": "1" if only_missing_email else "0",
                "only_valid_vat": "1" if only_valid_vat else "0",
                "only_duplicates": "1" if only_duplicates else "0",
            }
        )

        rows_qs = ClientImportRow.objects.filter(batch=batch)
        if q:
            rows_qs = apply_terms_filter(
                rows_qs,
                q,
                ["full_name__icontains", "nif__icontains", "phone__icontains"],
            )
        if only_missing_email:
            rows_qs = rows_qs.filter(missing_email=True)
        if only_valid_vat:
            rows_qs = rows_qs.filter(valid_vat=True)
        if only_duplicates:
            rows_qs = rows_qs.filter(Q(duplicate_in_file=True) | Q(exists_in_db=True))

        if action == "clear":
            request.session["import_selected_ids"] = []
            return redirect(f"{reverse('client_import')}?{redirect_qs}")

        if action == "select_all_filtered":
            all_ids = list(rows_qs.values_list("id", flat=True))
            selected_ids = set(all_ids)
            request.session["import_selected_ids"] = list(selected_ids)
            return redirect(f"{reverse('client_import')}?{redirect_qs}")

        if action == "select_page" or action == "update_selection":
            row_ids_on_page = request.POST.get("row_ids_on_page", "")
            row_ids_on_page = [r for r in row_ids_on_page.split(",") if r]
            checked = request.POST.getlist("row_id")
            selected_ids = set(selected_ids)
            for rid in row_ids_on_page:
                selected_ids.discard(int(rid))
            for rid in checked:
                selected_ids.add(int(rid))
            request.session["import_selected_ids"] = list(selected_ids)
            return redirect(f"{reverse('client_import')}?{redirect_qs}")

        def import_rows(qs):
            created = 0
            updated = 0
            skipped = 0
            errors = 0
            for row in qs:
                try:
                    nif = "".join(ch for ch in (row.nif or "") if ch.isdigit())
                    if not nif:
                        skipped += 1
                        continue
                    if batch.validate_nif and not row.valid_vat:
                        skipped += 1
                        continue
                    profile = ClientProfile.objects.filter(nif=nif).first()
                    if profile:
                        changed = False
                        if row.full_name and not profile.full_name:
                            profile.full_name = row.full_name
                            changed = True
                        if row.phone and not profile.phone:
                            profile.phone = row.phone
                            changed = True
                        if row.address_line1 and not profile.address_line1:
                            profile.address_line1 = row.address_line1
                            changed = True
                        if row.postal_code and not profile.postal_code:
                            profile.postal_code = row.postal_code
                            changed = True
                        if row.city and not profile.city:
                            profile.city = row.city
                            changed = True
                        if row.county and not profile.county:
                            profile.county = row.county
                            changed = True
                        if row.district and not profile.district:
                            profile.district = row.district
                            changed = True
                        if row.email and profile.user and not profile.user.email:
                            profile.user.email = row.email
                            profile.user.save(update_fields=["email"])
                        if changed:
                            profile.save()
                            updated += 1
                        else:
                            skipped += 1
                    else:
                        ClientProfile.objects.create(
                            user=None,
                            full_name=row.full_name or "—",
                            nif=nif,
                            phone=row.phone or "",
                            address_line1=row.address_line1 or "",
                            postal_code=row.postal_code or "",
                            city=row.city or "",
                            county=row.county or "",
                            district=row.district or "",
                        )
                        created += 1
                except Exception:
                    errors += 1
            log = ClientImportLog.objects.create(
                created_by=request.user,
                file_name=batch.original_filename,
                created_count=created,
                updated_count=updated,
                skipped_count=skipped,
                error_count=errors,
                summary=f"Criados {created}, atualizados {updated}, ignorados {skipped}, erros {errors}",
            )
            return log, created, updated, skipped, errors

        if action == "import_selected":
            row_ids_on_page = request.POST.get("row_ids_on_page", "")
            row_ids_on_page = [r for r in row_ids_on_page.split(",") if r]
            checked = request.POST.getlist("row_id")
            selected_ids = set(selected_ids)
            for rid in row_ids_on_page:
                selected_ids.discard(int(rid))
            for rid in checked:
                selected_ids.add(int(rid))
            request.session["import_selected_ids"] = list(selected_ids)
            ids = list(selected_ids)
            if not ids:
                messages.error(request, "Seleciona pelo menos uma linha para importar.")
                return redirect("client_import")
            rows = ClientImportRow.objects.filter(batch=batch, id__in=ids)
            _, created, updated, skipped, errors = import_rows(rows)
            messages.success(
                request,
                f"Importação concluída: {created} criados, {updated} atualizados, {skipped} ignorados, {errors} erros.",
            )
            return redirect("client_import")

        if action == "import_filtered":
            if not rows_qs.exists():
                messages.error(request, "Não há resultados filtrados para importar.")
                return redirect("client_import")
            _, created, updated, skipped, errors = import_rows(rows_qs)
            messages.success(
                request,
                f"Importação concluída: {created} criados, {updated} atualizados, {skipped} ignorados, {errors} erros.",
            )
            return redirect("client_import")

    logs = ClientImportLog.objects.all()[:10]
    batch = ClientImportBatch.objects.filter(id=batch_id).first() if batch_id else None
    q = (request.GET.get("q") or "").strip()
    only_missing_email = request.GET.get("only_missing_email") == "1"
    only_valid_vat = request.GET.get("only_valid_vat") == "1"
    only_duplicates = request.GET.get("only_duplicates") == "1"
    page_size = request.GET.get("page_size") or "5"
    try:
        page_size = int(page_size)
    except Exception:
        page_size = 5
    if page_size not in (5, 10, 15, 25):
        page_size = 5

    rows_page = None
    total_filtered = 0
    if batch:
        rows_qs = ClientImportRow.objects.filter(batch=batch)
        if q:
            rows_qs = apply_terms_filter(
                rows_qs,
                q,
                ["full_name__icontains", "nif__icontains", "phone__icontains"],
            )
        if only_missing_email:
            rows_qs = rows_qs.filter(missing_email=True)
        if only_valid_vat:
            rows_qs = rows_qs.filter(valid_vat=True)
        if only_duplicates:
            rows_qs = rows_qs.filter(Q(duplicate_in_file=True) | Q(exists_in_db=True))
        total_filtered = rows_qs.count()
        rows_page = Paginator(rows_qs, page_size).get_page(request.GET.get("page") or 1)

    return render(
        request,
        "core/client_import.html",
        {
            "logs": logs,
            "batch": batch,
            "rows_page": rows_page,
            "q": q,
            "only_missing_email": only_missing_email,
            "only_valid_vat": only_valid_vat,
            "only_duplicates": only_duplicates,
            "page_size": page_size,
            "page_sizes": [5, 10, 15, 25],
            "selected_count": len(selected_ids),
            "selected_ids": selected_ids,
            "total_filtered": total_filtered,
        },
    )
