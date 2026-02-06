from datetime import datetime, timedelta, time as dtime
from decimal import Decimal
from collections import defaultdict
from dataclasses import dataclass
from uuid import uuid4
import json
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
from .decorators import professional_required, backoffice_required
from .permissions import can_view_all_calendar, can_book_for_any_professional, can_access_backoffice
from .ratelimit import check_rate_limit, rate_limited_response, is_json_request, rate_limit
from .emails import send_templated_email, clinic_email, clinic_settings, log_email_skip
from .forms import (
    RegisterForm,
    ClientProfileForm,
    ProfessionalProfileForm,
    StaffClientCreateForm,
    BackofficeServiceForm,
    BackofficePartnerForm,
    BackofficeClientProfileForm,
    BackofficeAvailabilityForm,
)
from .utils.pricing import compute_pricing
from .utils.revenue import (
    get_revenue_queryset,
    compute_trend,
    month_range,
    week_range,
    day_range,
    month_start,
)
from .models import (
    Professional,
    Availability,
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
import csv
import io
import unicodedata

from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.views.decorators.http import require_POST, require_http_methods, require_GET

@login_required
def test_duralux(request):
    return render(request, "core/base_duralux.html")
#-------
# Logs
#------

def log_appt(action, appt, actor, *, old_date=None, old_time=None, new_date=None, new_time=None,
             old_status=None, new_status=None, note=""):
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




# -------
# Helper central
#--------

def can_modify_appointment(user, appointment):
    if can_view_all_calendar(user):
        return True

    prof = Professional.objects.filter(user=user).first()
    if prof:
        return appointment.professional_id == prof.id

    return appointment.client_id == user.id


def _availability_manager(prof):
    # tenta nomes comuns
    for name in ("availabilities", "availability_set", "availability"):
        if hasattr(prof, name):
            return getattr(prof, name)
    # fallback: descobre automaticamente
    for rel in prof._meta.related_objects:
        if rel.related_model.__name__ == "Availability":
            return getattr(prof, rel.name)
    return None

# ✅ Helper: dias de atendimento do profissional (PT)
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
    mgr = _availability_manager(prof)
    if not mgr:
        return []

    weekdays = (
        mgr.values_list("weekday", flat=True)
        .distinct()
        .order_by("weekday")
    )
    return [map_pt.get(w, str(w)) for w in weekdays]


def professional_works_on_date(prof, date_obj):
    mgr = _availability_manager(prof)
    if not mgr:
        return False
    return mgr.filter(weekday=date_obj.weekday()).exists()

# -------
# Helpers Estados Marcação
# --------

def _status_label(value: str) -> str:
    labels = {
        "scheduled": "Agendada",
        "completed": "Concluída",
        "cancelled": "Cancelada",
    }
    return labels.get(value or "", value or "-")

# -----------------------
# Helpers horários
# -----------------------

def _time_range(start: dtime, end: dtime, step_minutes: int):
    current = datetime.combine(datetime.today().date(), start)
    end_dt = datetime.combine(datetime.today().date(), end)
    step = timedelta(minutes=step_minutes)
    while current < end_dt:
        yield current.time().replace(second=0, microsecond=0)
        current += step

def _get_slots(prof: Professional, date_obj, step_minutes: int):
    today = timezone.localdate()
    now_t = timezone.localtime().time()
    if date_obj < today:
        return []

    weekday = date_obj.weekday()
    avails = Availability.objects.filter(professional=prof, weekday=weekday)

    taken = set(
        Appointment.objects.filter(professional=prof, date=date_obj)
        .values_list("time", flat=True)
    )
    blocked = set(
        BlockedSlot.objects.filter(professional=prof, date=date_obj)
        .values_list("time", flat=True)
    )

    slots = []
    seen = set()
    for a in avails:
        for t in _time_range(a.start_time, a.end_time, step_minutes=step_minutes):
            if date_obj == today and t <= now_t:
                continue
            if t not in taken and t not in blocked:
                time_str = t.strftime("%H:%M")
                if time_str not in seen:
                    seen.add(time_str)
                    slots.append(time_str)
    return slots


def _is_slot_blocked(prof: Professional, date_obj, time_obj) -> bool:
    return BlockedSlot.objects.filter(
        professional=prof,
        date=date_obj,
        time=time_obj,
    ).exists()


def _build_series_dates(start_date, count, freq, prof=None):
    dates = []
    current = start_date
    while len(dates) < count:
        should_add = True
        if freq == "weekdays" and current.weekday() >= 5:
            should_add = False
        if prof and not professional_works_on_date(prof, current):
            should_add = False

        if should_add:
            dates.append(current)

        if freq == "weekly":
            current = current + timedelta(days=7)
        else:
            current = current + timedelta(days=1)
    return dates

# -----------------------
# Views públicas (home/login/logout/registar)
# -----------------------

def home_view(request):
    """
    Home "premium":
    - Se user logado e for cliente: mostra próximas marcações e alerta de perfil incompleto
    - Se user for staff: mostra stats rápidas (hoje / pendentes / concluídas)
    """
    ctx = {}

    if request.user.is_authenticated:
        ctx["is_staff"] = request.user.is_staff

        # Nome para o "Bem-vindo"
        display_name = (request.user.first_name or request.user.username or "").strip()
        ctx["display_name"] = display_name or "utilizador"

        # -------------------------
        # Cliente (tem ClientProfile?)
        # -------------------------
        profile = None
        try:
            profile = request.user.client_profile
        except Exception:
            profile = None

        ctx["has_profile"] = bool(profile)

        # Perfil incompleto (igual ao book_view)
        ctx["profile_incomplete"] = False
        if profile:
            required_fields = ["full_name", "phone", "address_line1", "postal_code"]
            missing_basic = any(not getattr(profile, f, None) for f in required_fields)
            missing_location = not (profile.locality or profile.city)
            ctx["profile_incomplete"] = missing_basic or missing_location

        # Próximas marcações do cliente
        client_upcoming = []
        next_appt = None

        if profile:
            today = timezone.localdate()
            now_t = timezone.localtime().time()

            qs = (
                Appointment.objects
                .select_related("service", "professional", "professional__user")
                .filter(client=request.user)
                .exclude(status__in=["completed", "cancelled"])  # ignora canceladas também
                .order_by("date", "time", "id")
            )

            # filtra manualmente "hoje >= agora" para não apanhar horas já passadas
            client_upcoming = []
            for a in qs:
                # se não houver hora (TimeField null), assume que é "válido" para mostrar
                if a.date > today:
                    a.status_label = _status_label(a.status)
                    client_upcoming.append(a)
                elif a.date == today:
                    if not a.time or a.time >= now_t:
                        a.status_label = _status_label(a.status)
                        client_upcoming.append(a)

            next_appt = client_upcoming[0] if client_upcoming else None

        # Guarda no context para a HOME conseguir renderizar lista + contador
        ctx["client_upcoming"] = client_upcoming[:5]  # mostra só as próximas 5
        ctx["next_appointment"] = next_appt
        ctx["client_upcoming_count"] = len(client_upcoming)

        # -------------------------
        # Staff / Profissional (stats rápidas + agenda)
        # -------------------------
        is_professional = Professional.objects.filter(user=request.user).exists()
        if can_view_all_calendar(request.user) or is_professional:
            today = timezone.localdate()
            prof = Professional.objects.filter(user=request.user).first() if is_professional else None
            period = (request.GET.get("period") or "day").lower()
            if period not in {"day", "week", "month"}:
                period = "day"

            if period == "week":
                period_start = today - timedelta(days=today.weekday())
                period_end = period_start + timedelta(days=6)
                period_label = "esta semana"
            elif period == "month":
                period_start = today.replace(day=1)
                if period_start.month == 12:
                    next_month = period_start.replace(year=period_start.year + 1, month=1, day=1)
                else:
                    next_month = period_start.replace(month=period_start.month + 1, day=1)
                period_end = next_month - timedelta(days=1)
                period_label = "este mês"
            else:
                period_start = today
                period_end = today
                period_label = "hoje"

            appts_range = (
                Appointment.objects
                .filter(date__range=(period_start, period_end))
                .select_related("client", "service", "professional", "professional__user")
                .order_by("time", "id")
            )
            if prof:
                appts_range = appts_range.filter(professional=prof)

            appts_today = appts_range.filter(date=today)

            total = appts_range.count()
            scheduled = appts_range.filter(status="scheduled").count()
            completed = appts_range.filter(status="completed").count()
            cancelled = appts_range.filter(status="cancelled").count()

            base_params = request.GET.copy()
            base_params.pop("period", None)

            def period_qs(value: str) -> str:
                params = base_params.copy()
                params["period"] = value
                return params.urlencode()

            ctx["period"] = period
            ctx["period_label"] = period_label
            ctx["period_links"] = {
                "day": period_qs("day"),
                "week": period_qs("week"),
                "month": period_qs("month"),
            }

            ctx["today_total"] = total
            ctx["today_scheduled"] = scheduled
            ctx["today_completed"] = completed
            ctx["today_cancelled"] = cancelled
            ctx["today_appointments"] = list(appts_today[:5])

            def _pct(val):
                return int(round((val / total) * 100)) if total else 0

            ctx["today_status_breakdown"] = [
                {"label": "Agendadas", "count": scheduled, "pct": _pct(scheduled), "color": "#29ABE2"},
                {"label": "Concluídas", "count": completed, "pct": _pct(completed), "color": "#2ECC71"},
                {"label": "Canceladas", "count": cancelled, "pct": _pct(cancelled), "color": "#F39C12"},
            ]

            if can_view_all_calendar(request.user):
                ctx["staff_upcoming_total"] = (
                    Appointment.objects
                    .filter(date__gte=today)
                    .exclude(status__in=["completed", "cancelled"])
                    .count()
                )

    return render(request, "core/home.html", ctx)


def content_list_view(request):
    kind = (request.GET.get("kind") or "").strip()
    now = timezone.now()
    qs = ContentPost.objects.filter(status="published", published_at__lte=now)
    kinds = [k for k, _ in ContentPost.KIND_CHOICES]
    if kind and kind in kinds:
        qs = qs.filter(kind=kind)
    qs = qs.select_related("author").order_by("-is_featured", "-published_at", "-created_at")
    paginator = Paginator(qs, 6)
    page_obj = paginator.get_page(request.GET.get("page") or 1)
    return render(
        request,
        "core/content_list.html",
        {
            "posts": page_obj.object_list,
            "page_obj": page_obj,
            "paginator": paginator,
            "kind": kind,
            "kinds": ContentPost.KIND_CHOICES,
        },
    )


def content_detail_view(request, slug):
    now = timezone.now()
    post = get_object_or_404(
        ContentPost,
        slug=slug,
        status="published",
        published_at__lte=now,
    )
    return render(request, "core/content_detail.html", {"post": post})

def login_view(request):
    message = ""

    # Se vier next do GET/POST, respeitamos (ex: quando tenta aceder a página protegida)
    next_url = (request.POST.get("next") or request.GET.get("next") or "").strip()

    if request.method == "POST":
        blocked_min, retry_min = check_rate_limit(
            request,
            name="login_ip_minute",
            limit=5,
            window=60,
            by_ip=True,
        )
        blocked_hour, retry_hour = check_rate_limit(
            request,
            name="login_ip_hour",
            limit=20,
            window=3600,
            by_ip=True,
        )
        if blocked_min or blocked_hour:
            if is_json_request(request):
                return rate_limited_response(
                    request,
                    "Demasiadas tentativas. Tenta novamente em alguns minutos.",
                    max(retry_min, retry_hour),
                )
            message = "Demasiadas tentativas. Tenta novamente em alguns minutos."
            response = render(request, "core/login.html", {"message": message, "next": next_url}, status=429)
            response["Retry-After"] = str(max(retry_min, retry_hour))
            return response

        email = request.POST.get("username", "").strip()
        password = request.POST.get("password", "").strip()

        user = authenticate(request, username=email, password=password)
        if user is not None:
            login(request, user)

            # Se o utilizador vinha de um sítio específico, vai para lá
            if next_url:
                return redirect(next_url)

            # Caso contrário, escolhe landing page por role
            is_professional = Professional.objects.filter(user=user).exists()
            if user.is_staff or is_professional:
                return redirect("professional_calendar")
            return redirect("home")

        message = "Credenciais inválidas."

    # Para GET: manda o next para o template (pode vir vazio)
    return render(request, "core/login.html", {"message": message, "next": next_url})


def logout_view(request):
    logout(request)
    return redirect("/login/")


class PasswordResetRateLimitedView(auth_views.PasswordResetView):
    def post(self, request, *args, **kwargs):
        email = (request.POST.get("email") or "").strip().lower()
        blocked_ip, retry_ip = check_rate_limit(
            request,
            name="password_reset_ip_hour",
            limit=3,
            window=3600,
            by_ip=True,
        )
        blocked_email, retry_email = check_rate_limit(
            request,
            name="password_reset_email_hour",
            limit=3,
            window=3600,
            by_ip=False,
            by_value=email,
        )
        if blocked_ip or blocked_email:
            retry_after = max(retry_ip, retry_email)
            messages.error(request, "Demasiadas tentativas. Tenta novamente em alguns minutos.")
            form = self.get_form()
            context = self.get_context_data(form=form)
            response = self.render_to_response(context, status=429)
            response["Retry-After"] = str(retry_after)
            return response
        return super().post(request, *args, **kwargs)


class PasswordResetConfirmRateLimitedView(auth_views.PasswordResetConfirmView):
    def post(self, request, *args, **kwargs):
        blocked, retry_after = check_rate_limit(
            request,
            name="password_reset_confirm_ip_minute",
            limit=10,
            window=60,
            by_ip=True,
        )
        if blocked:
            messages.error(request, "Demasiadas tentativas. Tenta novamente em alguns minutos.")
            form = self.get_form()
            context = self.get_context_data(form=form)
            response = self.render_to_response(context, status=429)
            response["Retry-After"] = str(retry_after)
            return response
        return super().post(request, *args, **kwargs)

def register_view(request):
    if request.method == "POST":
        blocked, retry_after = check_rate_limit(
            request,
            name="register_ip_hour",
            limit=5,
            window=3600,
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
            response = render(request, "core/register.html", {"form": RegisterForm(request.POST)}, status=429)
            response["Retry-After"] = str(retry_after)
            return response

        form = RegisterForm(request.POST)
        if form.is_valid():
            full_name = form.cleaned_data["full_name"]
            nif = form.cleaned_data["nif"]
            email = form.cleaned_data["email"]
            password = form.cleaned_data["password1"]
            phone = form.cleaned_data.get("phone", "")
            address_line1 = form.cleaned_data.get("address_line1", "")
            district = form.cleaned_data.get("district", "")
            county = form.cleaned_data.get("county", "")
            locality = form.cleaned_data.get("locality", "")
            postal_code = form.cleaned_data.get("postal_code", "")
            terms_accepted = form.cleaned_data.get("terms_accepted", False)
            rgpd_accepted = form.cleaned_data.get("rgpd_accepted", False)
            city = locality

            settings_obj = clinic_settings()
            profile = ClientProfile.objects.filter(nif=nif).first()
            existing_user = profile.user if profile and profile.user_id else None
            can_claim = bool(existing_user and (not existing_user.has_usable_password()))
            if profile and profile.user_id and not can_claim:
                if profile.registration_status == "pending":
                    form.add_error(None, "O teu pedido já está pendente de validação.")
                else:
                    form.add_error(None, "Já existe uma conta associada a este NIF. Faz login ou recupera a palavra-passe.")
            else:
                username = nif
                if User.objects.filter(username=username).exists():
                    suffix = 1
                    while User.objects.filter(username=f"{nif}{suffix}").exists():
                        suffix += 1
                    username = f"{nif}{suffix}"
                if can_claim:
                    user = existing_user
                    user.email = email
                    user.username = email
                    user.set_password(password)
                    user.first_name = full_name
                    user.is_active = False
                    user.save()
                else:
                    user = User.objects.create_user(username=username, email=email, password=password)
                    user.first_name = full_name
                    user.is_active = False
                    user.save()

                group, _ = Group.objects.get_or_create(name="Cliente")
                user.groups.add(group)

                if profile:
                    if not profile.full_name:
                        profile.full_name = full_name
                    if phone and not profile.phone:
                        profile.phone = phone
                    if address_line1 and not profile.address_line1:
                        profile.address_line1 = address_line1
                    if district and not profile.district:
                        profile.district = district
                    if county and not profile.county:
                        profile.county = county
                    if locality and not profile.locality:
                        profile.locality = locality
                    if city and not profile.city:
                        profile.city = city
                    if postal_code and not profile.postal_code:
                        profile.postal_code = postal_code
                    profile.user = user
                    profile.terms_accepted = terms_accepted
                    profile.rgpd_accepted = rgpd_accepted
                    profile.registration_status = "pending"
                    profile.registration_requested_at = timezone.now()
                    profile.require_complete_profile = True
                    profile.updated_by = user
                    profile.save()
                else:
                    ClientProfile.objects.create(
                        user=user,
                        full_name=full_name,
                        phone=phone,
                        nif=nif,
                        address_line1=address_line1,
                        district=district,
                        county=county,
                        locality=locality,
                        city=city,
                        postal_code=postal_code,
                        terms_accepted=terms_accepted,
                        rgpd_accepted=rgpd_accepted,
                        registration_status="pending",
                        registration_requested_at=timezone.now(),
                        require_complete_profile=True,
                        created_by=user,
                        updated_by=user,
                    )

                if settings_obj.notify_admin_on_pending_registration:
                    clinic_to = settings_obj.clinic_email or clinic_email()
                    if clinic_to:
                        send_templated_email(
                            clinic_to,
                            f"Novo pedido de registo pendente — {settings_obj.clinic_name}",
                            "emails/clinic_appointment_event.html",
                            "emails/clinic_appointment_event.txt",
                            {
                                "event_type": "pending_registration",
                                "event_title": "Novo pedido de registo pendente",
                                "client_name": full_name,
                                "client_phone": phone,
                                "service_name": "-",
                                "professional_name": "-",
                                "old_date": "",
                                "old_time": "",
                                "new_date": "",
                                "new_time": "",
                                "cancelled_at": "",
                                "actor": "Cliente",
                                "admin_url": request.build_absolute_uri("/admin/"),
                            },
                            event="pending_registration",
                        )

                messages.success(request, "O teu pedido foi submetido. A clínica irá validar os teus dados.")
                return redirect("/login/")
    else:
        form = RegisterForm()

    return render(request, "core/register.html", {"form": form})

def _get_professional_or_403(user):
    """Devolve Professional do user ou None se não for profissional (admin/rececao não precisam)."""
    if can_view_all_calendar(user):
        return None  # admin não precisa de prof fixo
    if hasattr(user, "professional") and user.professional:
        return user.professional
    return None

@login_required(login_url="/login/")
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

    if can_view_all_calendar(request.user):
        # Admin/receção vê todos os clientes (inclui importados sem user)
        qs = ClientProfile.objects.select_related("user").all()
    else:
        # Apenas clientes (exclui staff/superuser e profissionais)
        eligible_users = (
            User.objects
            .filter(is_staff=False, is_superuser=False, professional__isnull=True)
            .exclude(groups__name__in=["Profissionais", "ADMIN"])
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
            .filter(user__in=eligible_users)
        )

    if q:
        qs = qs.filter(
            Q(full_name__icontains=q) |
            Q(user__username__icontains=q) |
            Q(phone__icontains=q)
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

    page_links = [
        {"number": n, "qs": qs_with(page=n), "is_current": n == page_obj.number}
        for n in paginator.page_range
    ]
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
            "selected_date": selected_date,
            "selected_time": selected_time,
            "selected_service_id": selected_service_id,
            "selected_professional_id": selected_professional_id,
            "occupied_professional_id": occupied_professional_id,
            "occupied_date": occupied_date,
            "occupied_time": occupied_time,
            "week": week,
        },
    )

from .models import AppointmentLog  # garante que tens isto no topo do views.py

@login_required(login_url="/login/")
def professional_customer_detail_view(request, client_id):
    prof = _get_professional_or_403(request.user)
    if not can_view_all_calendar(request.user) and prof is None:
        return HttpResponseForbidden("Acesso restrito a profissionais.")

    profile = get_object_or_404(ClientProfile.objects.select_related("user"), id=client_id)

    if not can_view_all_calendar(request.user):
        user_id = profile.user_id
        allowed = user_id and Appointment.objects.filter(professional=prof, client_id=user_id).exists()
        if not allowed:
            return HttpResponseForbidden("Não tens acesso a este cliente.")

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
        today = timezone.localdate()
        upcoming_appts = appt_qs.filter(date__gte=today).count()

    history_items = []
    if profile.user_id:
        logs_qs = AppointmentLog.objects.filter(appointment__client_id=profile.user_id)
        if not can_view_all_calendar(request.user):
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

    return render(
        request,
        "core/prof_customer_view.html",
        {
            "client_profile": profile,
            "client_user": client_user,
            "clinical_record": record,
            "movements": [],
            "history_items": history_items,
            "total_appts": total_appts,
            "upcoming_appts": upcoming_appts,
        },
    )

@login_required(login_url="/login/")
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

    # Permissão para profissional
    if not can_view_all_calendar(request.user):
        user_id = profile.user_id
        allowed = user_id and Appointment.objects.filter(professional=prof, client_id=user_id).exists()
        if not allowed:
            return HttpResponseForbidden("Não tens acesso a este cliente.")

    # Registo clínico (assumindo 1 por cliente)
    record, _ = ClinicalRecord.objects.get_or_create(
        client=profile,
        defaults={"updated_by": request.user},
    )

    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "update_clinical":
            record.conditions = (request.POST.get("conditions") or "").strip()
            record.notes = (request.POST.get("notes") or "").strip()
            record.updated_by = request.user
            record.save()
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


@login_required(login_url="/login/")
def professional_edit_client_profile_view(request, client_id):
    prof = _get_professional_or_403(request.user)
    if not can_view_all_calendar(request.user) and prof is None:
        return HttpResponseForbidden("Acesso restrito a profissionais.")

    profile = get_object_or_404(ClientProfile.objects.select_related("user"), id=client_id)

    if not can_view_all_calendar(request.user):
        user_id = profile.user_id
        allowed = user_id and Appointment.objects.filter(professional=prof, client_id=user_id).exists()
        if not allowed:
            return HttpResponseForbidden("Não tens acesso a este cliente.")

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

def _monday_of_week(d):
    return d - timedelta(days=d.weekday())

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


def _serialize_calendar_events(qs, service_colors):
    events = []
    for appt in qs:
        service = appt.service
        duration = getattr(service, "duration_minutes", None) or 60
        start_dt = datetime.combine(appt.date, appt.time or dtime.min)
        end_dt = start_dt + timedelta(minutes=duration)
        service_color = service_colors.get(str(service.id)) if service else "#5485e4"
        if appt.status == Appointment.STATUS_CANCELLED:
            color = "#d13b4c"
        elif appt.status == Appointment.STATUS_COMPLETED:
            color = "#25b865"
        else:
            color = service_color

        title_service = service.name if service else "Serviço"
        client_name = appt.client.get_full_name() or appt.client.username
        prof_name = appt.professional.user.get_full_name() or appt.professional.user.username
        status_label = appt.get_status_display()
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
                    "professional_id": appt.professional_id,
                    "status": status_label,
                },
            }
        )
    return events


def professional_calendar_view(request):
    """
    Calendário semanal do profissional:
    - Profissional vê apenas as suas marcações
    - Admin vê todas as marcações
    - Navegação semana anterior / seguinte
    """
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
        response = render(request, "core/professional_calendar.html", {"is_admin": False}, status=429)
        response["Retry-After"] = str(retry_after)
        return response

    is_admin = can_view_all_calendar(request.user)
    professional = None
    if not is_admin:
        professional = _get_professional_or_403(request.user)
        if professional is None:
            return HttpResponseForbidden("Acesso apenas para profissionais.")
    else:
        professional = Professional.objects.filter(user=request.user).first()

    today = timezone.localdate()
    now_t = timezone.localtime().time()
    base_date = today

    week_param = (request.GET.get("week") or "").strip()
    if week_param:
        try:
            base_date = datetime.strptime(week_param, "%Y-%m-%d").date()
        except ValueError:
            base_date = today
    elif not is_admin and professional:
        prof_avails = Availability.objects.filter(professional=professional)
        if prof_avails.exists():
            weekdays = list(prof_avails.values_list("weekday", flat=True).distinct())
            last_weekday = max(weekdays)
            if today.weekday() == last_weekday:
                today_avails = prof_avails.filter(weekday=today.weekday())
                if today_avails.exists():
                    last_end = max(a.end_time for a in today_avails)
                    if now_t >= last_end:
                        base_date = today + timedelta(days=7)

    # auto-concluir marcações no passado (só scheduled)
    past_qs = Appointment.objects.filter(
        Q(date__lt=today) | Q(date=today, time__lt=now_t),
        status=Appointment.STATUS_SCHEDULED,
    )
    if not is_admin and professional:
        past_qs = past_qs.filter(professional=professional)
    past_qs.update(status=Appointment.STATUS_COMPLETED)

    services_with_colors, service_colors = _calendar_service_colors()
    professionals = Professional.objects.select_related("user").order_by("user__username")

    week_start = _monday_of_week(base_date)
    week_end = week_start + timedelta(days=6)
    qs = Appointment.objects.select_related(
        "client", "service", "professional", "professional__user", "client__client_profile"
    ).filter(date__range=(week_start, week_end))
    if not is_admin and professional:
        qs = qs.filter(professional=professional)

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
        "eventsUrl": reverse("professional_calendar_events"),
        "bookingUrl": reverse("professional_book"),
    }

    ctx = {
        "professional": professional,
        "is_admin": is_admin,
        "week_start": week_start,
        "services": services_with_colors,
        "professionals": professionals,
        "calendar_data": calendar_data,
    }
    return render(request, "core/professional_calendar.html", ctx)


@require_GET
@login_required(login_url="/login/")
def professional_calendar_events_view(request):
    is_admin = can_view_all_calendar(request.user)
    professional = None
    if not is_admin:
        professional = _get_professional_or_403(request.user)
        if professional is None:
            return JsonResponse({"events": []}, status=403)
    else:
        professional = Professional.objects.filter(user=request.user).first()

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
        "client", "service", "professional", "professional__user", "client__client_profile"
    ).filter(date__range=(start, end))
    if not is_admin and professional:
        qs = qs.filter(professional=professional)

    service_ids = request.GET.getlist("service_id")
    if not service_ids:
        service_ids = [s for s in (request.GET.get("service_ids") or "").split(",") if s]
    if service_ids:
        qs = qs.filter(service_id__in=service_ids)

    selected_professional_id = (request.GET.get("professional_id") or "").strip()
    view_all = (request.GET.get("view_all") or "").strip()
    if is_admin and selected_professional_id and not view_all:
        qs = qs.filter(professional_id=selected_professional_id)

    selected_status = (request.GET.get("status") or "").strip()
    if selected_status:
        qs = qs.filter(status=selected_status)

    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(
            Q(client__username__icontains=q)
            | Q(client__client_profile__full_name__icontains=q)
            | Q(client__client_profile__phone__icontains=q)
        )

    events = _serialize_calendar_events(qs.order_by("date", "time", "id"), service_colors)
    return JsonResponse({"events": events})


@login_required(login_url="/login/")
@require_POST
def toggle_blocked_slot_view(request):
    prof = _get_professional_or_403(request.user)
    is_staff = request.user.is_staff
    can_view_all = can_view_all_calendar(request.user)

    professional_id = (request.POST.get("professional_id") or "").strip()
    date_str = (request.POST.get("date") or "").strip()
    time_str = (request.POST.get("time") or "").strip()
    week = (request.POST.get("week") or "").strip()

    if not date_str or not time_str:
        return HttpResponseForbidden("Dados inválidos.")

    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
        time_obj = datetime.strptime(time_str, "%H:%M").time()
    except ValueError:
        return HttpResponseForbidden("Dados inválidos.")

    if can_view_all:
        if not professional_id:
            return HttpResponseForbidden("Profissional obrigatório.")
        target_prof = get_object_or_404(Professional, id=professional_id)
    else:
        if prof is None:
            return HttpResponseForbidden("Acesso restrito a profissionais.")
        target_prof = prof

    existing = BlockedSlot.objects.filter(
        professional=target_prof,
        date=date_obj,
        time=time_obj,
    ).first()

    if existing:
        if not (is_staff or existing.created_by_id == request.user.id):
            return HttpResponseForbidden("Não tens permissão para remover este bloqueio.")
        existing.delete()
        messages.success(request, "Bloqueio removido.")
    else:
        if not (is_staff or target_prof.user_id == request.user.id):
            return HttpResponseForbidden("Não tens permissão para criar bloqueios.")
        has_appt = Appointment.objects.filter(
            professional=target_prof,
            date=date_obj,
            time=time_obj,
        ).exists()
        if has_appt:
            messages.error(request, "Já existe uma marcação nesse horário.")
        else:
            BlockedSlot.objects.create(
                professional=target_prof,
                date=date_obj,
                time=time_obj,
                created_by=request.user,
            )
            messages.success(request, "Horário bloqueado.")

    if week:
        return redirect(f"/prof/calendario/?week={week}")
    return redirect("professional_calendar")

@login_required(login_url="/login/")
def professional_customer_form_view(request, client_id=None):
    prof = _get_professional_or_403(request.user)
    if not can_view_all_calendar(request.user) and prof is None:
        return HttpResponseForbidden("Acesso restrito a profissionais.")

    selected_date = (request.GET.get("date") or request.POST.get("date") or "").strip()
    selected_time = (request.GET.get("time") or request.POST.get("time") or "").strip()
    selected_service_id = (request.GET.get("service_id") or request.POST.get("service_id") or "").strip()
    selected_professional_id = (request.GET.get("professional_id") or request.POST.get("professional_id") or "").strip()
    week = (request.GET.get("week") or request.POST.get("week") or "").strip()
    status = (request.GET.get("status") or request.POST.get("status") or "").strip()
    q = (request.GET.get("q") or request.POST.get("q") or "").strip()

    is_edit = client_id is not None
    client_profile = None
    client_user = None
    clinical_record = None
    if is_edit:
        client_profile = get_object_or_404(ClientProfile.objects.select_related("user"), id=client_id)
        if not can_view_all_calendar(request.user):
            user_id = client_profile.user_id
            allowed = user_id and Appointment.objects.filter(professional=prof, client_id=user_id).exists()
            if not allowed:
                return HttpResponseForbidden("Não tens acesso a este cliente.")
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

    if request.method == "POST":
        post_data = request.POST.copy()
        if post_data.get("postal_code_1") or post_data.get("postal_code_2"):
            cp1 = (post_data.get("postal_code_1") or "").strip()
            cp2 = (post_data.get("postal_code_2") or "").strip()
            post_data["postal_code"] = f"{cp1}-{cp2}" if cp1 and cp2 else ""
        form = StaffClientCreateForm(post_data, request.FILES, existing_user=client_user)
        if form.is_valid():
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
                if ClientProfile.objects.filter(nif=nif).exclude(pk=client_profile.pk).exists():
                    form.add_error("nif", "Já existe outro cliente com este NIF.")
                target_profile = client_profile
            else:
                target_profile = ClientProfile.objects.filter(nif=nif).first()
                if target_profile and target_profile.user_id:
                    form.add_error("nif", "Já existe uma conta associada a este NIF.")
                if (selected_date or selected_time or selected_service_id or selected_professional_id or week) and not password:
                    form.add_error(None, "Para marcar diretamente é necessário definir uma password.")

            if not form.errors:
                user = client_user or (target_profile.user if target_profile else None)
                if password:
                    if not user:
                        username_base = (username_input or nif).strip()
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
                target_profile.discount_percent = discount_percent if discount_type == "percent" else None
                target_profile.discount_amount = discount_amount if discount_type == "fixed" else None
                target_profile.discount_label = discount_label
                if profile_photo:
                    target_profile.profile_photo = profile_photo
                if user:
                    target_profile.user = user
                target_profile.registration_status = "approved"
                target_profile.require_complete_profile = True
                target_profile.updated_by = request.user
                target_profile.save()

                record, _ = ClinicalRecord.objects.get_or_create(
                    client=target_profile,
                    defaults={"updated_by": request.user},
                )
                record.allergies = clinical_allergies or ""
                record.conditions = clinical_conditions or ""
                record.notes = clinical_notes or ""
                record.updated_by = request.user
                record.save()

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
                return redirect(back_to_clients_url)
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
        form = StaffClientCreateForm(initial=initial, existing_user=client_user)

    return render(
        request,
        "core/prof_customer_form.html",
        {
            "form": form,
            "selected_date": selected_date,
            "selected_time": selected_time,
            "selected_service_id": selected_service_id,
            "selected_professional_id": selected_professional_id,
            "week": week,
            "prefill_profile_id": prefill_profile.id if prefill_profile else "",
            "status": status,
            "q": q,
            "back_to_clients_url": back_to_clients_url,
            "movements": [],
            "history_items": [],
            "is_edit": is_edit,
            "client_id": client_id or "",
            "clinical_record": clinical_record,
        },
    )


@login_required(login_url="/login/")
def professional_create_client_view(request):
    return professional_customer_form_view(request)

@login_required(login_url="/login/")
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

    return render(
        request,
        "core/professional_appointment_detail.html",
        {
            "appointment": appt,
            "week": week,
        },
    )

from django.views.decorators.http import require_http_methods



@require_POST
@login_required(login_url="/login/")
def professional_cancel_appointment_view(request, appointment_id):
    if not can_view_all_calendar(request.user) and not Professional.objects.filter(user=request.user).exists():
        return HttpResponseForbidden("Acesso apenas para profissionais.")

    if can_view_all_calendar(request.user):
        appt = get_object_or_404(Appointment, id=appointment_id)
    else:
        professional = get_object_or_404(Professional, user=request.user)
        appt = get_object_or_404(Appointment, id=appointment_id, professional=professional)

    if appt.status == "completed":
        return HttpResponseForbidden("Não podes cancelar uma marcação concluída.")

    # se houver choice "cancelled", usa; senão apaga (fallback)
    status_field = Appointment._meta.get_field("status")
    choices = [c[0] for c in (status_field.choices or [])]
    if "cancelled" in choices:
        appt.status = "cancelled"
        appt.save(update_fields=["status"])
    else:
        appt.delete()

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
    week = request.GET.get("week", "")
    if week:
        return redirect(f"/prof/calendario/?week={week}")
    return redirect("professional_calendar")

@require_POST
@login_required(login_url="/login/")
def professional_complete_appointment_view(request, appointment_id):
    if not can_view_all_calendar(request.user) and not Professional.objects.filter(user=request.user).exists():
        return HttpResponseForbidden("Acesso apenas para profissionais.")

    if can_view_all_calendar(request.user):
        appt = get_object_or_404(Appointment, id=appointment_id)
    else:
        professional = get_object_or_404(Professional, user=request.user)
        appt = get_object_or_404(Appointment, id=appointment_id, professional=professional)

    status_field = Appointment._meta.get_field("status")
    choices = [c[0] for c in (status_field.choices or [])]
    if "completed" in choices:
        appt.status = "completed"
        appt.save(update_fields=["status"])
        messages.success(request, "Marcação marcada como concluída.")
    else:
        return HttpResponseForbidden("O modelo não suporta status 'completed'.")

    week = request.GET.get("week", "")
    if week:
        return redirect(f"/prof/calendario/?week={week}")
    return redirect("professional_calendar")

# -----------------------
# Perfil (cliente)
# -----------------------

@login_required(login_url="/login/")
def profile_view(request):
    try:
        profile = request.user.client_profile
    except ClientProfile.DoesNotExist:
        profile = ClientProfile.objects.create(
            user=request.user,
            full_name=request.user.first_name or request.user.username,
            created_by=request.user,
            updated_by=request.user,
            require_complete_profile=True,
        )

    ClinicalRecord.objects.get_or_create(
        client=profile,
        defaults={"updated_by": request.user},
    )

    password_form = PasswordChangeForm(user=request.user)
    # Evita foco automático no campo de password ao entrar no perfil
    password_form.fields["old_password"].widget.attrs.pop("autofocus", None)
    email_error = ""
    if request.method == "POST":
        if request.POST.get("action") == "change_password":
            password_form = PasswordChangeForm(user=request.user, data=request.POST)
            password_form.fields["old_password"].widget.attrs.pop("autofocus", None)
            if password_form.is_valid():
                password_form.save()
                messages.success(request, "Password atualizada com sucesso.")
                return redirect("profile")
        post_data = request.POST.copy()
        email = (post_data.get("email") or "").strip().lower()
        if profile.require_complete_profile and not email:
            email_error = "Campo de preenchimento obrigatório"
        elif email:
            if " " in email or "@" not in email:
                email_error = "Indica um email válido."
            else:
                local, domain = email.split("@", 1)
                if "." not in domain:
                    email_error = "Indica um email válido."
                else:
                    exists = User.objects.filter(email__iexact=email).exclude(pk=request.user.pk).exists()
                    if exists:
                        email_error = "Este email já está registado."
        if post_data.get("postal_code_1") or post_data.get("postal_code_2"):
            cp1 = (post_data.get("postal_code_1") or "").strip()
            cp2 = (post_data.get("postal_code_2") or "").strip()
            post_data["postal_code"] = f"{cp1}-{cp2}" if cp1 and cp2 else ""
        form = ClientProfileForm(post_data, request.FILES, instance=profile)
        # Autofocus no primeiro campo em falta
        _apply_profile_autofocus(form, profile)
        if form.is_valid() and not email_error:
            p = form.save(commit=False)
            if not p.city and p.locality:
                p.city = p.locality
            p.updated_by = request.user
            p.save()
            if email:
                request.user.email = email
                request.user.username = email
                request.user.save(update_fields=["email", "username"])
            next_url = request.GET.get("next") or "/marcar/"
            return redirect(next_url)
    else:
        form = ClientProfileForm(instance=profile)
        # Autofocus no primeiro campo em falta
        _apply_profile_autofocus(form, profile)

    return render(
        request,
        "core/profile.html",
        {"form": form, "password_form": password_form, "email_error": email_error},
    )


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

@login_required(login_url="/login/")
def professional_profile_view(request):
    prof = Professional.objects.filter(user=request.user).first()
    if not prof and not request.user.is_staff:
        return HttpResponseForbidden("Acesso restrito a profissionais.")
    if not prof:
        return HttpResponseForbidden("Profissional não encontrado.")

    if request.method == "POST":
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
        },
    )

# -----------------------
# Marcar (cliente)
# -----------------------

@login_required(login_url="/login/")
def book_view(request):
    try:
        profile = request.user.client_profile
    except ClientProfile.DoesNotExist:
        return redirect("/perfil/?next=/marcar/")

    ClinicalRecord.objects.get_or_create(
        client=profile,
        defaults={"updated_by": request.user},
    )

    required_fields = ["full_name", "phone", "address_line1", "postal_code"]
    missing_basic = any(not getattr(profile, f) for f in required_fields)
    missing_location = not (profile.locality or profile.city)
    if missing_basic or missing_location:
        return redirect("/perfil/?next=/marcar/")

    today = timezone.localdate()

    # Mensagens (separadas!)
    message = ""       # erros / validações / slots
    info_message = ""  # info do profissional (dias)

    # Seleções via GET (para UI)
    selected_service_id = (request.GET.get("service_id") or "").strip()
    selected_professional_id = (request.GET.get("professional_id") or "").strip()
    selected_date = (request.GET.get("date") or "").strip()
    rate_status = 200
    retry_after = 0

    # Se o serviço for de turma, redireciona para lista de sessões
    if selected_service_id:
        service_obj = Service.objects.filter(id=selected_service_id).first()
        if service_obj and service_obj.service_type == "group":
            return redirect("group_sessions_list", service_id=service_obj.id)

    # Querysets base
    services = Service.objects.all().order_by("name")
    professionals_qs = Professional.objects.select_related("user").all().order_by("user__username")

    # Filtrar profissionais pelo serviço escolhido
    if selected_service_id:
        professionals_qs = professionals_qs.filter(services__id=selected_service_id).distinct()

    # ✅ Se só houver 1 profissional para o serviço, auto-seleciona (apenas se ainda não escolheste nenhum)
    if selected_service_id and not selected_professional_id:
        only_one = list(professionals_qs[:2])
        if len(only_one) == 1:
            selected_professional_id = str(only_one[0].id)

            # ✅ força o URL a ficar consistente
            params = f"?service_id={selected_service_id}&professional_id={selected_professional_id}"
            if selected_date:
                params += f"&date={selected_date}"
            return redirect(request.path + params)

    # ✅ Se o profissional selecionado não pertence ao queryset filtrado -> limpa
    if selected_professional_id and not professionals_qs.filter(id=selected_professional_id).exists():
        selected_professional_id = ""

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
            notes = (request.POST.get("notes") or "").strip()

            # manter seleções no render em caso de erro
            selected_service_id = service_id
            selected_professional_id = professional_id
            selected_date = date_str

            # refaz queryset filtrado pelo serviço (para dropdown não ficar vazio)
            professionals_qs = Professional.objects.select_related("user").all().order_by("user__username")
            if selected_service_id:
                professionals_qs = professionals_qs.filter(services__id=selected_service_id).distinct()

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

                if date_obj < today:
                    message = "Não podes marcar consultas no passado."
                    slots = []
                elif date_obj == today and time_obj <= now_t:
                    message = "Este horário já passou."
                    slots = _get_slots(prof, date_obj, step_minutes=service.duration_minutes)
                elif _is_slot_blocked(prof, date_obj, time_obj):
                    message = "Este horário está indisponível."
                    slots = _get_slots(prof, date_obj, step_minutes=service.duration_minutes)
                elif not professional_works_on_date(prof, date_obj):
                    message = f"Este profissional não atende nesse dia. Atende: {', '.join(prof_days) or '—'}."
                    slots = []
                else:
                    slots_now = _get_slots(prof, date_obj, step_minutes=service.duration_minutes)
                    if time_str not in slots_now:
                        message = "Esse horário já não está disponível. Atualiza a página."
                        slots = slots_now
                    else:
                        try:
                            with transaction.atomic():
                                client_profile = getattr(request.user, "client_profile", None)
                                pricing = compute_pricing(service, client_profile)
                                appt = Appointment.objects.create(
                                    client=request.user,
                                    professional=prof,
                                    service=service,
                                    date=date_obj,
                                    time=time_obj,
                                    notes=notes,
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
                                )

                            settings_obj = clinic_settings()
                            clinic_to = settings_obj.clinic_email or clinic_email()
                            if settings_obj.notify_clinic_on_new_booking and clinic_to:
                                send_templated_email(
                                    clinic_to,
                                    f"Nova marcação — {service.name} — {appt.date} {appt.time}",
                                    "emails/clinic_appointment_event.html",
                                    "emails/clinic_appointment_event.txt",
                                    {
                                        "event_type": "created",
                                        "event_title": "Nova marcação",
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
                                    send_templated_email(
                                        prof_email,
                                        f"Nova marcação — {service.name} — {appt.date} {appt.time}",
                                        "emails/clinic_appointment_event.html",
                                        "emails/clinic_appointment_event.txt",
                                        {
                                            "event_type": "created",
                                            "event_title": "Nova marcação",
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
                                    f"Marcação confirmada — {service.name} em {appt.date} {appt.time}",
                                    "emails/appointment_confirmed.html",
                                    "emails/appointment_confirmed.txt",
                                    {
                                        "client_name": request.user.get_full_name() or request.user.username,
                                        "service_name": service.name,
                                        "professional_name": prof.user.get_full_name() or prof.user.username,
                                        "date": appt.date,
                                        "time": appt.time,
                                        "notes": notes,
                                        "manage_url": request.build_absolute_uri(reverse("my_appointments")),
                                    },
                                    event="new_booking",
                                )
                            else:
                                log_email_skip("new_booking", "Marcação confirmada", "Cliente sem email", "")

                            messages.success(request, "Marcação criada com sucesso.")
                            return redirect("my_appointments")

                        except IntegrityError:
                            message = "Esse horário acabou de ser reservado. Escolhe outro."
                            slots = _get_slots(prof, date_obj, step_minutes=service.duration_minutes)

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
                    slots = _get_slots(prof, date_obj, step_minutes=service.duration_minutes)
                    if not slots:
                        message = "Não há horários disponíveis para este dia."
                elif not professional_works_on_date(prof, date_obj):
                    prof_days = professional_weekdays_labels(prof)
                    message = f"Este profissional não atende nesse dia. Atende: {', '.join(prof_days) or '—'}."
                    slots = []
                else:
                    slots = _get_slots(prof, date_obj, step_minutes=service.duration_minutes)
                    if not slots:
                        message = "Não há horários disponíveis para este dia."

    upcoming_appointments = list(
        Appointment.objects
        .filter(client=request.user, date__gte=today)
        .exclude(status="completed")
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
            "professionals": professionals_qs,
            "services": services,
            "selected_service_id": selected_service_id,
            "selected_professional_id": selected_professional_id,
            "selected_date": selected_date,
            "slots": slots,
            "message": message,
            "info_message": info_message,
            "upcoming_appointments": upcoming_appointments,
            "today": today,
            "prof_days": prof_days,
            "price_preview": price_preview,
            "back_to_appointments_url": reverse("my_appointments"),
        },
        status=rate_status,
    )
    if rate_status == 429 and retry_after:
        response["Retry-After"] = str(retry_after)
    return response


@login_required(login_url="/login/")
@require_GET
def slots_api_view(request):
    service_id = (request.GET.get("service_id") or "").strip()
    professional_id = (request.GET.get("professional_id") or "").strip()
    date_str = (request.GET.get("date") or "").strip()

    if not (service_id and professional_id and date_str):
        return JsonResponse({"ok": False, "slots": [], "message": "Dados incompletos."})

    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        return JsonResponse({"ok": False, "slots": [], "message": "Data inválida."})

    today = timezone.localdate()
    if date_obj < today:
        return JsonResponse({"ok": False, "slots": [], "message": "Data no passado."})

    if not Professional.objects.filter(id=professional_id, services__id=service_id).exists():
        return JsonResponse({"ok": False, "slots": [], "message": "Profissional inválido para este serviço."})

    service = Service.objects.filter(id=service_id).first()
    prof = Professional.objects.filter(id=professional_id).first()
    if not service or not prof:
        return JsonResponse({"ok": False, "slots": [], "message": "Dados inválidos."})
    if service.service_type == "group":
        return JsonResponse({"ok": False, "slots": [], "message": "Serviço de turma não usa horários individuais."})

    if not professional_works_on_date(prof, date_obj):
        return JsonResponse({"ok": False, "slots": [], "message": "Este profissional não atende nesse dia."})

    slots = _get_slots(prof, date_obj, step_minutes=service.duration_minutes)
    if not slots:
        return JsonResponse({"ok": False, "slots": [], "message": "Sem horários disponíveis."})

    return JsonResponse({"ok": True, "slots": slots, "message": ""})


@login_required(login_url="/login/")
@require_http_methods(["GET", "POST"])
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
            clients_qs = clients_qs.filter(
                Q(full_name__icontains=client_query)
                | Q(user__username__icontains=client_query)
                | Q(phone__icontains=client_query)
                | Q(nif__icontains=client_query)
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
    start_date_str = (request.GET.get("start_date") or "").strip()
    count_str = (request.GET.get("count") or "").strip()
    freq = (request.GET.get("freq") or "").strip() or "weekly"
    preferred_professional_id = (request.GET.get("professional_id") or "").strip()

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
        notes_global = (request.POST.get("notes_global") or "").strip()

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
                    slots_now = _get_slots(prof, date_obj, step_minutes=service.duration_minutes)
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
                    notes=notes_global,
                    series_id=series_id,
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

        messages.success(request, f"Foram criadas {created} marcações.")
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


@login_required(login_url="/login/")
def group_sessions_list_view(request, service_id):
    service = get_object_or_404(Service, id=service_id)
    if service.service_type != "group":
        return redirect("book")

    today = timezone.localdate()
    now_t = timezone.localtime().time()

    GroupSession.objects.filter(
        date__lt=today,
        status=GroupSession.STATUS_SCHEDULED
    ).update(status=GroupSession.STATUS_COMPLETED)
    GroupSession.objects.filter(
        date=today,
        time__lt=now_t,
        status=GroupSession.STATUS_SCHEDULED
    ).update(status=GroupSession.STATUS_COMPLETED)

    sessions = (
        GroupSession.objects
        .select_related("service", "professional", "professional__user")
        .filter(service=service, status=GroupSession.STATUS_SCHEDULED)
        .order_by("date", "time")
    )

    sessions = [s for s in sessions if (s.date > today) or (s.date == today and s.time > now_t)]

    enrolled_ids = set(
        GroupEnrollment.objects.filter(client=request.user, status="active", session__service=service)
        .values_list("session_id", flat=True)
    )

    return render(
        request,
        "core/group_sessions_list.html",
        {
            "service": service,
            "sessions": sessions,
            "enrolled_ids": enrolled_ids,
        },
    )


@login_required(login_url="/login/")
@require_POST
def enroll_group_session_view(request, session_id):
    session = get_object_or_404(GroupSession.objects.select_related("service"), id=session_id)
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
        session = GroupSession.objects.select_for_update().get(id=session_id)
        existing = GroupEnrollment.objects.filter(session=session, client=request.user, status="active").exists()
        if existing:
            messages.info(request, "Já estás inscrito nesta sessão.")
            return redirect("group_sessions_list", service_id=session.service_id)

        if session.spots_left <= 0:
            messages.error(request, "Esta sessão já está cheia.")
            return redirect("group_sessions_list", service_id=session.service_id)

        GroupEnrollment.objects.create(session=session, client=request.user)

    messages.success(request, "Inscrição realizada com sucesso.")
    return redirect("group_sessions_list", service_id=session.service_id)


@login_required(login_url="/login/")
def my_group_sessions_view(request):
    today = timezone.localdate()
    now_t = timezone.localtime().time()

    GroupSession.objects.filter(
        date__lt=today,
        status=GroupSession.STATUS_SCHEDULED
    ).update(status=GroupSession.STATUS_COMPLETED)
    GroupSession.objects.filter(
        date=today,
        time__lt=now_t,
        status=GroupSession.STATUS_SCHEDULED
    ).update(status=GroupSession.STATUS_COMPLETED)

    enrolments = (
        GroupEnrollment.objects
        .select_related("session", "session__service", "session__professional", "session__professional__user")
        .filter(client=request.user, status="active")
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
        if is_future:
            if month_year:
                if s.date.year == month_year.year and s.date.month == month_year.month:
                    upcoming.append(e)
            else:
                upcoming.append(e)
        else:
            past.append(e)

    upcoming_page = Paginator(upcoming, 5).get_page(request.GET.get("page") or 1)
    upcoming_months = (
        GroupSession.objects
        .filter(enrolments__client=request.user, enrolments__status="active")
        .dates("date", "month", order="ASC")
    )

    return render(
        request,
        "core/my_group_sessions.html",
        {
            "upcoming": upcoming_page,
            "past": past,
            "upcoming_months": upcoming_months,
            "selected_month": month_value,
        },
    )


@login_required(login_url="/login/")
def group_sessions_admin_list_view(request):
    if not can_access_backoffice(request.user):
        return HttpResponseForbidden("Acesso apenas para backoffice.")

    prof = Professional.objects.filter(user=request.user).first()
    qs = GroupSession.objects.select_related("service", "professional", "professional__user").order_by("date", "time")

    if not can_view_all_calendar(request.user) and prof:
        qs = qs.filter(professional=prof)

    return render(
        request,
        "core/group_sessions_admin_list.html",
        {"sessions": qs, "return_to": request.get_full_path()},
    )


@login_required(login_url="/login/")
@require_http_methods(["GET", "POST"])
def create_group_sessions_bulk_view(request):
    if not can_access_backoffice(request.user):
        return HttpResponseForbidden("Acesso apenas para backoffice.")

    services = Service.objects.filter(service_type="group").order_by("name")
    professionals = Professional.objects.select_related("user").prefetch_related("services").order_by("user__username")
    message = ""

    if request.method == "POST":
        service_id = (request.POST.get("service_id") or "").strip()
        professional_id = (request.POST.get("professional_id") or "").strip()
        start_date_str = (request.POST.get("start_date") or "").strip()
        end_date_str = (request.POST.get("end_date") or "").strip()
        time_str = (request.POST.get("time") or "").strip()
        capacity_str = (request.POST.get("capacity") or "").strip()
        weekdays = request.POST.getlist("weekdays")

        if not (service_id and start_date_str and end_date_str and time_str and weekdays):
            message = "Preenche todos os campos obrigatórios."
        else:
            service = get_object_or_404(Service, id=service_id, service_type="group")
            professional = None
            if professional_id:
                professional = Professional.objects.filter(id=professional_id).first()
            try:
                start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
                end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
                time_obj = datetime.strptime(time_str, "%H:%M").time()
            except Exception:
                message = "Datas ou hora inválidas."
                start_date = end_date = None

            if not message:
                today = timezone.localdate()
                capacity = int(capacity_str) if capacity_str.isdigit() else None
                weekday_ints = {int(w) for w in weekdays}
                created = 0
                skipped = 0
                current = start_date
                while current <= end_date:
                    if current >= today and current.weekday() in weekday_ints:
                        exists = GroupSession.objects.filter(
                            service=service,
                            professional=professional,
                            date=current,
                            time=time_obj,
                        ).exists()
                        if not exists:
                            GroupSession.objects.create(
                                service=service,
                                professional=professional,
                                date=current,
                                time=time_obj,
                                capacity=capacity,
                            )
                            created += 1
                        else:
                            skipped += 1
                    current += timedelta(days=1)
                message = f"Criadas {created} sessões. Ignoradas {skipped} duplicadas."

    return render(
        request,
        "core/group_sessions_bulk_create.html",
        {
            "services": services,
            "professionals": professionals,
            "message": message,
        },
    )


@login_required(login_url="/login/")
def group_session_detail_admin_view(request, session_id):
    if not can_access_backoffice(request.user):
        return HttpResponseForbidden("Acesso apenas para backoffice.")

    session = get_object_or_404(
        GroupSession.objects.select_related("service", "professional", "professional__user"),
        id=session_id,
    )
    if not can_view_all_calendar(request.user):
        prof = Professional.objects.filter(user=request.user).first()
        if session.professional_id and prof and session.professional_id != prof.id:
            return HttpResponseForbidden("Sem acesso a esta sessão.")

    enrolments = (
        GroupEnrollment.objects
        .select_related("client", "client__client_profile")
        .filter(session=session, status="active")
        .order_by("-created_at")
    )

    return_to = _safe_return_to(request, request.GET.get("return_to"))

    return render(
        request,
        "core/group_session_detail_admin.html",
        {
            "session": session,
            "enrolments": enrolments,
            "return_to": return_to,
        },
    )


@login_required(login_url="/login/")
@backoffice_required
def backoffice_dashboard_view(request):
    today = timezone.localdate()
    now_t = timezone.localtime().time()
    revenue_qs = get_revenue_queryset(request.user)
    can_view_all = can_view_all_calendar(request.user)
    prof = None
    if not can_view_all:
        prof = Professional.objects.filter(user=request.user).first()

    appt_base_qs = Appointment.objects.select_related("client", "service", "professional", "professional__user")
    if prof:
        appt_base_qs = appt_base_qs.filter(professional=prof)

    def period_total(start_d, end_d):
        return (
            revenue_qs.filter(date__gte=start_d, date__lt=end_d)
            .aggregate(total=Coalesce(Sum("final_price"), Decimal("0.00")))
            .get("total")
            or Decimal("0.00")
        )

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

    revenue_today = period_total(today_start, today_end)
    revenue_yesterday = period_total(yesterday_start, yesterday_end)
    revenue_period = period_total(period_start, period_end)
    revenue_prev_period = period_total(prev_period_start, prev_period_end)

    revenue_trend_today = compute_trend(revenue_today, revenue_yesterday)
    revenue_trend_period = compute_trend(revenue_period, revenue_prev_period)

    appointments_today = appt_base_qs.filter(date=today).count()
    appointments_yesterday = appt_base_qs.filter(date=today - timedelta(days=1)).count()
    appointments_period = appt_base_qs.filter(date__gte=period_start, date__lt=period_end).count()
    appointments_trend = compute_trend(Decimal(appointments_today), Decimal(appointments_yesterday))

    period_qs = appt_base_qs.filter(date__gte=period_start, date__lt=period_end)
    scheduled_count = period_qs.filter(status=Appointment.STATUS_SCHEDULED).count()
    completed_count = period_qs.filter(status=Appointment.STATUS_COMPLETED).count()
    cancelled_count = period_qs.filter(status=Appointment.STATUS_CANCELLED).count()

    prev_period_qs = appt_base_qs.filter(date__gte=prev_period_start, date__lt=prev_period_end)
    prev_total = prev_period_qs.count()
    prev_completed = prev_period_qs.filter(status=Appointment.STATUS_COMPLETED).count()

    def _rate(completed: int, total: int) -> Decimal:
        if not total:
            return Decimal("0.0")
        return (Decimal(completed) / Decimal(total)) * Decimal("100")

    completed_rate = _rate(completed_count, appointments_period)
    prev_completed_rate = _rate(prev_completed, prev_total)
    if prev_completed_rate > 0:
        completed_rate_delta = ((completed_rate - prev_completed_rate) / prev_completed_rate) * Decimal("100")
        completed_rate_delta_display = f"{completed_rate_delta:.1f}%"
    else:
        completed_rate_delta = None
        completed_rate_delta_display = "—"

    if completed_rate_delta is not None and completed_rate_delta > 0:
        completed_rate_delta_display = f"+{completed_rate_delta_display}"

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
        for appt in period_qs.filter(date=today):
            if appt.time:
                hourly[appt.time.hour] += 1
        for h in range(24):
            chart_labels.append(f"{h:02d}:00")
            chart_values.append(hourly[h])
    else:
        day = period_start
        while day < period_end:
            chart_labels.append(day.strftime("%d %b"))
            chart_values.append(period_qs.filter(date=day).count())
            day += timedelta(days=1)

    client_qs = ClientProfile.objects.all()
    if prof:
        client_qs = client_qs.filter(appointments__professional=prof).distinct()
    period_start_dt = timezone.make_aware(datetime.combine(period_start, datetime.min.time()))
    period_end_dt = timezone.make_aware(datetime.combine(period_end, datetime.min.time()))
    prev_start_dt = timezone.make_aware(datetime.combine(prev_period_start, datetime.min.time()))
    prev_end_dt = timezone.make_aware(datetime.combine(prev_period_end, datetime.min.time()))

    new_clients_period = client_qs.filter(created_at__gte=period_start_dt, created_at__lt=period_end_dt).count()
    new_clients_prev = client_qs.filter(created_at__gte=prev_start_dt, created_at__lt=prev_end_dt).count()
    new_clients_trend = compute_trend(Decimal(new_clients_period), Decimal(new_clients_prev))
    today_start_dt = timezone.make_aware(datetime.combine(today, datetime.min.time()))
    today_end_dt = timezone.make_aware(datetime.combine(today + timedelta(days=1), datetime.min.time()))
    new_clients_today = client_qs.filter(created_at__gte=today_start_dt, created_at__lt=today_end_dt).count()

    agenda_today = []
    agenda_qs = appt_base_qs.filter(date=today).order_by("time")
    for appt in agenda_qs:
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

    overview_qs = appt_base_qs.filter(date__gte=period_start, date__lt=period_end).exclude(
        status=Appointment.STATUS_CANCELLED
    )
    appointments_by_service = (
        overview_qs.values("service__name")
        .annotate(total=Count("id"))
        .order_by("-total")
    )

    return render(
        request,
        "core/backoffice/dashboard.html",
        {
            "today": today,
            "revenue_today": revenue_today,
            "revenue_trend_today": revenue_trend_today,
            "appointments_today": appointments_today,
            "appointments_trend": appointments_trend,
            "new_clients_period": new_clients_period,
            "new_clients_trend": new_clients_trend,
            "revenue_period": revenue_period,
            "revenue_trend_period": revenue_trend_period,
            "appointments_period": appointments_period,
            "period": period,
            "agenda_today": agenda_today,
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
            "kpi_revenue_today": revenue_today,
            "kpi_appts_today": appointments_today,
            "kpi_new_clients": new_clients_today,
            "kpi_sales_total": revenue_period,
            "today_agenda": agenda_today,
        },
    )


@login_required(login_url="/login/")
@backoffice_required
def backoffice_agenda_view(request):
    today = timezone.localdate()
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
        "notes": "",
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
            "notes": (request.POST.get("notes") or "").strip(),
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
                        notes=quick_form["notes"],
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

    appointments = Appointment.objects.select_related(
        "client",
        "client__client_profile",
        "service",
        "professional",
        "professional__user",
    ).filter(date__range=(start_date, end_date))

    if professional_id:
        appointments = appointments.filter(professional_id=professional_id)
    if service_id:
        appointments = appointments.filter(service_id=service_id)
    if client_id:
        appointments = appointments.filter(client__client_profile__id=client_id)
    if status:
        appointments = appointments.filter(status=status)
    if q:
        appointments = appointments.filter(
            Q(client__username__icontains=q)
            | Q(client__first_name__icontains=q)
            | Q(client__last_name__icontains=q)
            | Q(client__client_profile__full_name__icontains=q)
            | Q(client__client_profile__phone__icontains=q)
            | Q(client__client_profile__nif__icontains=q)
        )

    group_sessions = GroupSession.objects.select_related(
        "service",
        "professional",
        "professional__user",
    ).filter(date__range=(start_date, end_date))

    if professional_id:
        group_sessions = group_sessions.filter(professional_id=professional_id)
    if service_id:
        group_sessions = group_sessions.filter(service_id=service_id)
    if status:
        group_sessions = group_sessions.filter(status=status)

    if kind == "appointment":
        group_sessions = GroupSession.objects.none()
    elif kind == "group":
        appointments = Appointment.objects.none()

    group_sessions = group_sessions.annotate(
        active_count=Count("enrolments", filter=Q(enrolments__status="active"))
    )

    return_to = request.get_full_path()
    items = []
    for appt in appointments:
        client_profile = getattr(appt.client, "client_profile", None)
        client_name = (
            (client_profile.full_name if client_profile and client_profile.full_name else "")
            or appt.client.get_full_name()
            or appt.client.username
        )
        items.append(
            AgendaItem(
                kind="appointment",
                date=appt.date,
                time=appt.time,
                service_name=appt.service.name if appt.service else "-",
                professional_name=appt.professional.user.get_full_name() or appt.professional.user.username,
                client_label=client_name,
                status_label=appt.get_status_display(),
                status_raw=appt.status,
                price_label=str(appt.final_price) if appt.final_price is not None else "—",
                open_url=reverse("professional_appointment_detail", args=[appt.id]),
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
            "type": kind,
            "q": q,
            "per_page": per_page,
            "professionals": Professional.objects.select_related("user").order_by("user__username"),
            "services": Service.objects.order_by("name"),
            "return_to": return_to,
            "can_quick_create": can_book_for_any_professional(request.user),
            "quick_modal_open": quick_modal_open,
            "quick_errors": quick_errors,
            "quick_form": quick_form,
            "quick_slots": quick_slots,
        },
    )


@login_required(login_url="/login/")
@backoffice_required
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


@login_required(login_url="/login/")
@backoffice_required
def backoffice_clients_quick_view(request):
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
        qs = qs.filter(
            Q(full_name__icontains=q)
            | Q(nif__icontains=q)
            | Q(phone__icontains=q)
            | Q(user__username__icontains=q)
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


@require_GET
@login_required(login_url="/login/")
@backoffice_required
def backoffice_api_clients_search(request):
    if not can_book_for_any_professional(request.user):
        return HttpResponseForbidden("Acesso negado.")
    q = (request.GET.get("q") or "").strip()
    if len(q) < 2:
        return JsonResponse({"results": []})

    qs = (
        ClientProfile.objects.select_related("user")
        .filter(
            Q(full_name__icontains=q)
            | Q(nif__icontains=q)
            | Q(phone__icontains=q)
            | Q(user__username__icontains=q)
        )
        .order_by("full_name")[:10]
    )

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


@require_GET
@login_required(login_url="/login/")
@backoffice_required
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


@require_GET
@login_required(login_url="/login/")
@backoffice_required
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


@require_GET
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


@require_POST
@login_required(login_url="/login/")
@backoffice_required
def backoffice_cancel_appointment_view(request, appointment_id):
    if not can_view_all_calendar(request.user):
        return HttpResponseForbidden("Acesso apenas para receção/admin.")

    appt = get_object_or_404(Appointment, id=appointment_id)
    if appt.status == "completed":
        return HttpResponseForbidden("Não podes cancelar uma marcação concluída.")

    status_field = Appointment._meta.get_field("status")
    choices = [c[0] for c in (status_field.choices or [])]
    if "cancelled" in choices:
        appt.status = "cancelled"
        appt.save(update_fields=["status"])
    else:
        appt.delete()

    messages.success(request, "Marcação cancelada.")
    return_to = _safe_return_to(request, request.GET.get("return_to"))
    if return_to:
        return redirect(return_to)
    return redirect("backoffice_agenda")


@require_POST
@login_required(login_url="/login/")
@backoffice_required
def backoffice_complete_appointment_view(request, appointment_id):
    if not can_view_all_calendar(request.user):
        return HttpResponseForbidden("Acesso apenas para receção/admin.")

    appt = get_object_or_404(Appointment, id=appointment_id)
    status_field = Appointment._meta.get_field("status")
    choices = [c[0] for c in (status_field.choices or [])]
    if "completed" in choices:
        appt.status = "completed"
        appt.save(update_fields=["status"])
        messages.success(request, "Marcação marcada como concluída.")
    else:
        return HttpResponseForbidden("O modelo não suporta status 'completed'.")

    return_to = _safe_return_to(request, request.GET.get("return_to"))
    if return_to:
        return redirect(return_to)
    return redirect("backoffice_agenda")


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


@dataclass
class AgendaItem:
    kind: str
    date: datetime.date
    time: datetime.time
    service_name: str
    professional_name: str
    client_label: str
    status_label: str
    status_raw: str
    price_label: str
    open_url: str
    cancel_url: str | None
    complete_url: str | None
    reschedule_url: str | None


@login_required(login_url="/login/")
@backoffice_required
def backoffice_services_list_view(request):
    q = (request.GET.get("q") or "").strip()
    per_page = request.GET.get("per_page") or "5"
    try:
        per_page = int(per_page)
    except (TypeError, ValueError):
        per_page = 5
    if per_page not in (5, 10, 15, 25, 50):
        per_page = 5

    qs = Service.objects.all().order_by("name")
    if q:
        qs = qs.filter(name__icontains=q)

    paginator = Paginator(qs, per_page)
    page_obj = paginator.get_page(request.GET.get("page") or 1)

    return render(
        request,
        "backoffice/services_list.html",
        {
            "services": page_obj.object_list,
            "page_obj": page_obj,
            "paginator": paginator,
            "q": q,
            "per_page": per_page,
            "return_to": request.get_full_path(),
        },
    )


@login_required(login_url="/login/")
@backoffice_required
def backoffice_service_create_view(request):
    return_to = _safe_return_to(request, request.POST.get("return_to") or request.GET.get("return_to"))
    if request.method == "POST":
        form = BackofficeServiceForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Serviço criado com sucesso.")
            if return_to:
                return redirect(return_to)
            return redirect("backoffice_services")
    else:
        form = BackofficeServiceForm()
    return render(
        request,
        "backoffice/service_form.html",
        {"form": form, "title": "Novo serviço", "return_to": return_to},
    )


@login_required(login_url="/login/")
@backoffice_required
def backoffice_service_edit_view(request, service_id):
    service = get_object_or_404(Service, id=service_id)
    return_to = _safe_return_to(request, request.POST.get("return_to") or request.GET.get("return_to"))
    if request.method == "POST":
        form = BackofficeServiceForm(request.POST, instance=service)
        if form.is_valid():
            form.save()
            messages.success(request, "Serviço atualizado.")
            if return_to:
                return redirect(return_to)
            return redirect("backoffice_services")
    else:
        form = BackofficeServiceForm(instance=service)
    return render(
        request,
        "backoffice/service_form.html",
        {"form": form, "title": "Editar serviço", "return_to": return_to},
    )


@login_required(login_url="/login/")
@backoffice_required
def backoffice_availabilities_list_view(request):
    q = (request.GET.get("q") or "").strip()
    per_page = request.GET.get("per_page") or "5"
    try:
        per_page = int(per_page)
    except (TypeError, ValueError):
        per_page = 5
    if per_page not in (5, 10, 15, 25, 50):
        per_page = 5

    professional_id = (request.GET.get("professional_id") or "").strip()
    qs = Availability.objects.select_related("professional", "professional__user").order_by(
        "professional__user__username", "weekday", "start_time"
    )
    if q:
        qs = qs.filter(
            Q(professional__user__first_name__icontains=q)
            | Q(professional__user__last_name__icontains=q)
            | Q(professional__user__username__icontains=q)
        )
    if professional_id:
        qs = qs.filter(professional_id=professional_id)

    paginator = Paginator(qs, per_page)
    page_obj = paginator.get_page(request.GET.get("page") or 1)
    professionals = Professional.objects.select_related("user").order_by("user__username")
    options = ['<option value="">Todos</option>']
    for prof in professionals:
        selected = "selected" if professional_id and str(professional_id) == str(prof.id) else ""
        label = prof.user.get_full_name() or prof.user.username
        options.append(f'<option value="{prof.id}" {selected}>{label}</option>')
    extra_fields = mark_safe(
        '<div class="col-12 col-md-4 col-lg-3">'
        '<label class="form-label">Profissional</label>'
        '<select class="form-select" name="professional_id" data-auto-submit>'
        f'{"".join(options)}'
        "</select></div>"
    )

    return render(
        request,
        "backoffice/availabilities_list.html",
        {
            "availabilities": page_obj.object_list,
            "page_obj": page_obj,
            "paginator": paginator,
            "q": q,
            "per_page": per_page,
            "professional_id": professional_id,
            "professionals": professionals,
            "extra_fields": extra_fields,
            "return_to": request.get_full_path(),
        },
    )


@login_required(login_url="/login/")
@backoffice_required
def backoffice_availability_create_view(request):
    return_to = _safe_return_to(request, request.POST.get("return_to") or request.GET.get("return_to"))
    if request.method == "POST":
        form = BackofficeAvailabilityForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Disponibilidade criada com sucesso.")
            return redirect(return_to or "backoffice_availabilities")
    else:
        form = BackofficeAvailabilityForm()
    return render(
        request,
        "backoffice/availability_form.html",
        {"form": form, "title": "Nova disponibilidade", "return_to": return_to},
    )


@login_required(login_url="/login/")
@backoffice_required
def backoffice_availability_edit_view(request, availability_id):
    availability = get_object_or_404(Availability, id=availability_id)
    return_to = _safe_return_to(request, request.POST.get("return_to") or request.GET.get("return_to"))
    if request.method == "POST":
        form = BackofficeAvailabilityForm(request.POST, instance=availability)
        if form.is_valid():
            form.save()
            messages.success(request, "Disponibilidade atualizada.")
            return redirect(return_to or "backoffice_availabilities")
    else:
        form = BackofficeAvailabilityForm(instance=availability)
    return render(
        request,
        "backoffice/availability_form.html",
        {"form": form, "title": "Editar disponibilidade", "return_to": return_to},
    )


@login_required(login_url="/login/")
@backoffice_required
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
        qs = qs.filter(name__icontains=q)

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


@login_required(login_url="/login/")
@backoffice_required
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


@login_required(login_url="/login/")
@backoffice_required
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


@login_required(login_url="/login/")
@backoffice_required
def backoffice_partner_prices_view(request):
    partners = Partner.objects.order_by("name")
    partner_id = request.GET.get("partner_id") or request.POST.get("partner_id") or ""
    selected_partner = Partner.objects.filter(id=partner_id).first() if partner_id else None
    services = Service.objects.order_by("name")

    if request.method == "POST" and selected_partner:
        for service in services:
            prefix = f"service_{service.id}"
            pricing_mode = request.POST.get(f"{prefix}_pricing_mode") or "single"
            price = (request.POST.get(f"{prefix}_price") or "").strip()
            price_first = (request.POST.get(f"{prefix}_price_first") or "").strip()
            price_followup = (request.POST.get(f"{prefix}_price_followup") or "").strip()

            if not price and not price_first and not price_followup:
                continue

            def _to_decimal(val):
                try:
                    return Decimal(val.replace(",", "."))
                except Exception:
                    return None

            price_val = _to_decimal(price)
            price_first_val = _to_decimal(price_first)
            price_followup_val = _to_decimal(price_followup)

            obj, _ = PartnerServicePrice.objects.get_or_create(
                partner=selected_partner,
                service=service,
                defaults={"price": price_val or Decimal("0.00")},
            )
            obj.pricing_mode = pricing_mode
            if pricing_mode == "single":
                obj.price = price_val if price_val is not None else obj.price
                obj.price_first = None
                obj.price_followup = None
            else:
                obj.price_first = price_first_val
                obj.price_followup = price_followup_val
                if obj.price is None:
                    obj.price = Decimal("0.00")
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


@login_required(login_url="/login/")
@backoffice_required
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
        qs = qs.filter(
            Q(full_name__icontains=q)
            | Q(nif__icontains=q)
            | Q(phone__icontains=q)
            | Q(user__username__icontains=q)
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


@login_required(login_url="/login/")
@backoffice_required
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




@login_required(login_url="/login/")
@require_http_methods(["GET", "POST"])
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
            rows_qs = rows_qs.filter(
                Q(full_name__icontains=q)
                | Q(nif__icontains=q)
                | Q(phone__icontains=q)
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
            rows_qs = rows_qs.filter(
                Q(full_name__icontains=q)
                | Q(nif__icontains=q)
                | Q(phone__icontains=q)
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

# -----------------------
# Ficha do cliente (profissional)
# -----------------------

@professional_required
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

                        TreatmentRecord.objects.create(
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

                        return redirect(
                            request.path + f"?professional_id={active_prof.id}&service_id={service_id}&date={date_str}"
                        )

        elif action == "update_notes":
            tr_id = request.POST.get("treatment_id")
            notes = request.POST.get("notes", "").strip()

            treatment = get_object_or_404(TreatmentRecord, id=tr_id, client=client)
            treatment.notes = notes
            treatment.updated_by = request.user
            treatment.save()

            if active_prof:
                return redirect(request.path + f"?professional_id={active_prof.id}")
            return redirect(request.path)
        elif action == "update_clinical":
            clinical.allergies = (request.POST.get("allergies") or "").strip()
            clinical.conditions = (request.POST.get("conditions") or "").strip()
            clinical.notes = (request.POST.get("notes") or "").strip()
            clinical.updated_by = request.user
            clinical.save()
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

# ---
# Marcações para profissionais
# ___

@login_required(login_url="/login/")
def professional_book_view(request):
    """
    Profissional cria marcação PARA um cliente.
    Fluxo:
    - entra por /prof/marcar/?client_id=ID
    - profissional é fixo (o user logado)
    """
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
        if selected_prof:
            services = selected_prof.services.all().order_by("name")
    else:
        services = prof.services.all().order_by("name")
    message = ""

    selected_service_id = request.GET.get("service_id") or ""
    selected_date = request.GET.get("date") or request.POST.get("date") or ""
    selected_time = request.GET.get("time") or request.POST.get("time") or ""
    week = request.GET.get("week") or request.POST.get("week") or ""
    status = request.GET.get("status") or request.POST.get("status") or ""
    q = request.GET.get("q") or request.POST.get("q") or ""
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



    if selected_service_id and selected_date and selected_prof:
        service = get_object_or_404(Service, id=selected_service_id)
        date_obj = datetime.strptime(selected_date, "%Y-%m-%d").date()
        slots = _get_slots(selected_prof, date_obj, step_minutes=service.duration_minutes)

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
                    "slots": slots,
                    "message": message,
                    "week": week,
                    "is_admin": can_book_any,
                    "service_map_json": json.dumps(service_map, ensure_ascii=True),
                },
                status=429,
            )
            response["Retry-After"] = str(retry_after)
            return response
        service_id = request.POST.get("service_id")
        date_str = request.POST.get("date")
        time_str = request.POST.get("time")
        professional_id = request.POST.get("professional_id") or ""
        notes = (request.POST.get("notes") or "").strip()

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
                valid_slots = _get_slots(selected_prof, date_obj, step_minutes=service.duration_minutes) if selected_prof else []
                selected_service_id = service_id
                selected_date = date_str
                selected_time = time_str
                slots = valid_slots
                selected_professional_name = selected_prof.user.get_full_name() or selected_prof.user.username if selected_prof else ""
                return render(
                    request,
                    "core/professional_book.html",
                    {
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
                        "slots": slots,
                        "message": message,
                        "week": week,
                        "is_admin": can_book_any,
                        "service_map_json": json.dumps(service_map, ensure_ascii=True),
                    },
                )

            time_obj = datetime.strptime(time_str, "%H:%M").time()
            if _is_slot_blocked(selected_prof, date_obj, time_obj):
                message = "Este horário está indisponível."
                service_map = {
                    str(p.id): [{"id": s.id, "name": s.name} for s in p.services.all().order_by("name")]
                    for p in professionals
                }
                valid_slots = _get_slots(selected_prof, date_obj, step_minutes=service.duration_minutes)
                selected_service_id = service_id
                selected_date = date_str
                selected_time = time_str
                slots = valid_slots
                selected_professional_name = selected_prof.user.get_full_name() or selected_prof.user.username if selected_prof else ""
                return render(
                    request,
                    "core/professional_book.html",
                    {
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
                        "slots": slots,
                        "message": message,
                        "week": week,
                        "is_admin": can_book_any,
                        "service_map_json": json.dumps(service_map, ensure_ascii=True),
                    },
                )

            valid_slots = _get_slots(selected_prof, date_obj, step_minutes=service.duration_minutes)
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
                        notes=notes,
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
                )
                settings_obj = clinic_settings()
                clinic_to = settings_obj.clinic_email or clinic_email()
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
                            "notes": notes,
                            "manage_url": request.build_absolute_uri(reverse("my_appointments")),
                        },
                        event="new_booking",
                    )
                else:
                    log_email_skip("new_booking", "Marcação confirmada", "Cliente sem email", "")
                messages.success(request, "Marcação criada para o cliente.")
                return redirect(back_to_calendar_url)

    selected_professional_name = ""
    if selected_prof:
        selected_professional_name = selected_prof.user.get_full_name() or selected_prof.user.username

    service_map = {
        str(p.id): [{"id": s.id, "name": s.name} for s in p.services.all().order_by("name")]
        for p in professionals
    }
    service_map_json = json.dumps(service_map, ensure_ascii=True)

    return render(
        request,
        "core/professional_book.html",
        {
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
            "slots": slots,
            "message": message,
            "week": week,
            "is_admin": can_book_any,
            "service_map_json": service_map_json,
            "back_to_calendar_url": back_to_calendar_url,
            "selection_in_past": selection_in_past,
            "status": status,
            "q": q,
        },
    )
    week = request.POST.get("week") or request.GET.get("week") or ""
    if week:
        return redirect(f"/prof/calendario/?week={week}")
        return redirect("professional_calendar")

@login_required(login_url="/login/")
def my_appointments_view(request):
    if Professional.objects.filter(user=request.user).exists():
        return redirect("professional_calendar")
    """
    Página do cliente: lista de marcações futuras e passadas.
    """
    qs = (
        Appointment.objects
        .filter(client=request.user)
        .select_related("service", "professional", "professional__user")
        .order_by("-date", "-time", "-id")
    )

    today = timezone.localdate()
    now_t = timezone.localtime().time()

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

    for a in qs:
        # label bonito para o template (sem mexer no model)
        a.status_label = _status_label(a.status)

        is_future = (a.date > today) or (a.date == today and a.time and a.time >= now_t)
        if a.status == "completed":
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

    # Turmas
    GroupSession.objects.filter(
        date__lt=today,
        status=GroupSession.STATUS_SCHEDULED
    ).update(status=GroupSession.STATUS_COMPLETED)
    GroupSession.objects.filter(
        date=today,
        time__lt=now_t,
        status=GroupSession.STATUS_SCHEDULED
    ).update(status=GroupSession.STATUS_COMPLETED)

    group_enrolments = (
        GroupEnrollment.objects
        .select_related("session", "session__service", "session__professional", "session__professional__user")
        .filter(client=request.user, status="active")
        .order_by("session__date", "session__time")
    )
    group_upcoming = []
    group_past = []
    for e in group_enrolments:
        s = e.session
        is_future = (s.date > today) or (s.date == today and s.time and s.time > now_t)
        (group_upcoming if is_future else group_past).append(e)

    upcoming_page = Paginator(upcoming, 5).get_page(request.GET.get("page") or 1)
    upcoming_months = (
        Appointment.objects
        .filter(client=request.user)
        .exclude(status="completed")
        .dates("date", "month", order="ASC")
    )

    return render(
        request,
        "core/my_appointments.html",
        {
            "next_appointment": next_appt,
            "upcoming": upcoming_page,
            "past": past,
            "group_upcoming": group_upcoming,
            "group_past": group_past,
            "upcoming_months": upcoming_months,
            "selected_month": month_value,
        },
    )

@login_required(login_url="/login/")
@require_POST
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

    if appt.status == Appointment.STATUS_COMPLETED:
        return HttpResponseForbidden("Não podes cancelar uma marcação concluída.")

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



@login_required(login_url="/login/")
@require_http_methods(["GET", "POST"])
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

    if appt.status in ["completed", "cancelled"]:
        return HttpResponseForbidden("Não podes reagendar uma marcação concluída ou cancelada.")

    service = appt.service
    prof = appt.professional

    is_prof_flow = can_view_all_calendar(request.user) or hasattr(request.user, "professional")

    def _redirect_after_save():
        week = request.GET.get("week") or request.POST.get("week") or ""
        if is_prof_flow:
            if week:
                return redirect(f"/prof/calendario/?week={week}")
            return redirect("professional_calendar")
        return redirect("my_appointments")

    if request.method == "POST":
        date_str = (request.POST.get("date") or "").strip()
        time_str = (request.POST.get("time") or "").strip()
        week = (request.POST.get("week") or "").strip()

        if not (date_str and time_str):
            return render(
                request,
                "core/reschedule_appointment.html",
                {
                    "appointment": appt,
                    "slots": [],
                    "selected_date": date_str,
                    "error": "Data e hora são obrigatórias.",
                    "week": week,
                    "is_professional_flow": (
                    Professional.objects.filter(user=request.user).exists()
                    or can_view_all_calendar(request.user)
                     ),
                },
            )

        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
        today = timezone.localdate()
        now_t = timezone.localtime().time()
        if date_obj < today:
            return render(
                request,
                "core/reschedule_appointment.html",
                {
                    "appointment": appt,
                    "slots": [],
                    "selected_date": date_str,
                    "error": "Não podes marcar no passado.",
                    "week": week,
                    "is_professional_flow": (
                    Professional.objects.filter(user=request.user).exists()
                    or can_view_all_calendar(request.user)
                    ),
                },
            )
        if date_obj == today:
            time_obj = datetime.strptime(time_str, "%H:%M").time()
            if time_obj <= now_t:
                return render(
                    request,
                    "core/reschedule_appointment.html",
                    {
                        "appointment": appt,
                        "slots": [],
                        "selected_date": date_str,
                        "error": "Este horário já passou.",
                        "week": week,
                        "is_professional_flow": (
                        Professional.objects.filter(user=request.user).exists()
                        or can_view_all_calendar(request.user)
                        ),
                    },
                )
        time_obj = datetime.strptime(time_str, "%H:%M").time()
        if _is_slot_blocked(prof, date_obj, time_obj):
            return render(
                request,
                "core/reschedule_appointment.html",
                {
                    "appointment": appt,
                    "slots": _get_slots(prof, date_obj, step_minutes=service.duration_minutes),
                    "selected_date": date_str,
                    "error": "Este horário está indisponível.",
                    "week": week,
                    "is_professional_flow": (
                    Professional.objects.filter(user=request.user).exists()
                    or can_view_all_calendar(request.user)
                    ),
                },
            )
        valid_slots = _get_slots(prof, date_obj, step_minutes=service.duration_minutes)

        if time_str not in valid_slots:
            return render(
                request,
                "core/reschedule_appointment.html",
                {
                    "appointment": appt,
                    "slots": valid_slots,
                    "selected_date": date_str,
                    "error": "Hora inválida ou já ocupada.",
                    "week": week,
                    "is_professional_flow": (
                    Professional.objects.filter(user=request.user).exists()
                    or can_view_all_calendar(request.user)
                    ),
                },
            )

        time_obj = datetime.strptime(time_str, "%H:%M").time()
        old_date, old_time = appt.date, appt.time
        appt.date = date_obj
        appt.time = time_obj
        appt.save(update_fields=["date", "time"])
        
        log_appt(
            AppointmentLog.ACTION_RESCHEDULED,
            appt,
            request.user,
            old_date=old_date,
            old_time=old_time,
            new_date=appt.date,
            new_time=appt.time,
        )

        if can_view_all_calendar(request.user) or Professional.objects.filter(user=request.user).exists():
            settings_obj = clinic_settings()
            if settings_obj.notify_client_on_clinic_changes:
                client_email = (appt.client.email or "").strip()
                if client_email:
                    send_templated_email(
                        client_email,
                        f"Marcação reagendada — {settings_obj.clinic_name}",
                        "emails/appointment_changed_by_clinic.html",
                        "emails/appointment_changed_by_clinic.txt",
                        {
                            "client_name": appt.client.get_full_name() or appt.client.username,
                            "change_type": "rescheduled",
                            "old_date": old_date,
                            "old_time": old_time,
                            "new_date": appt.date,
                            "new_time": appt.time,
                            "service_name": appt.service.name if appt.service else "-",
                            "professional_name": appt.professional.user.get_full_name() or appt.professional.user.username,
                            "reason": "",
                            "manage_url": request.build_absolute_uri("/marcacoes/"),
                        },
                        event="reschedule_clinic",
                    )
                else:
                    log_email_skip(
                        "reschedule_clinic",
                        "Marcação reagendada",
                        "Cliente sem email.",
                    )

            if settings_obj.notify_clinic_on_client_reschedule:
                clinic_to = clinic_email()
                if clinic_to:
                    send_templated_email(
                        clinic_to,
                        f"Marcação reagendada — {appt.service.name if appt.service else '-'} — {old_date}→{appt.date}",
                        "emails/clinic_appointment_event.html",
                        "emails/clinic_appointment_event.txt",
                        {
                            "event_type": "rescheduled",
                            "event_title": "Marcação reagendada",
                            "client_name": appt.client.get_full_name() or appt.client.username,
                            "client_phone": getattr(getattr(appt.client, "client_profile", None), "phone", ""),
                            "service_name": appt.service.name if appt.service else "-",
                            "professional_name": appt.professional.user.get_full_name() or appt.professional.user.username,
                            "old_date": old_date,
                            "old_time": old_time,
                            "new_date": appt.date,
                            "new_time": appt.time,
                            "cancelled_at": "",
                            "actor": request.user.get_full_name() or request.user.username,
                            "admin_url": request.build_absolute_uri("/prof/calendario/"),
                        },
                        event="reschedule_clinic",
                    )
                else:
                    log_email_skip(
                        "reschedule_clinic",
                        "Marcação reagendada",
                        "Email da clínica vazio.",
                    )
        else:
            settings_obj = clinic_settings()
            if settings_obj.notify_clinic_on_client_reschedule:
                clinic_to = clinic_email()
                if clinic_to:
                    send_templated_email(
                        clinic_to,
                        f"Marcação reagendada — {appt.service.name if appt.service else '-'} — {old_date}→{appt.date}",
                        "emails/clinic_appointment_event.html",
                        "emails/clinic_appointment_event.txt",
                        {
                            "event_type": "rescheduled",
                            "event_title": "Marcação reagendada pelo cliente",
                            "client_name": appt.client.get_full_name() or appt.client.username,
                            "client_phone": getattr(getattr(appt.client, "client_profile", None), "phone", ""),
                            "service_name": appt.service.name if appt.service else "-",
                            "professional_name": appt.professional.user.get_full_name() or appt.professional.user.username,
                            "old_date": old_date,
                            "old_time": old_time,
                            "new_date": appt.date,
                            "new_time": appt.time,
                            "cancelled_at": "",
                            "actor": request.user.get_full_name() or request.user.username,
                            "admin_url": request.build_absolute_uri("/prof/calendario/"),
                        },
                        event="reschedule_client",
                    )
                else:
                    log_email_skip(
                        "reschedule_client",
                        "Marcação reagendada",
                        "Email da clínica vazio.",
                    )

        messages.success(request, "Marcação reagendada com sucesso.")
        return _redirect_after_save()

    # GET
    selected_date = (request.GET.get("date") or "").strip()
    week = (request.GET.get("week") or "").strip()
    slots = []

    if selected_date:
        try:
            date_obj = datetime.strptime(selected_date, "%Y-%m-%d").date()
            if date_obj < timezone.localdate():
                slots = []
            else:
                slots = _get_slots(prof, date_obj, step_minutes=service.duration_minutes)
        except Exception:
            slots = []

    return render(
        request,
        "core/reschedule_appointment.html",
        {
            "appointment": appt,
            "slots": slots,
            "selected_date": selected_date,
            "week": week,
            "is_professional_flow": (
            Professional.objects.filter(user=request.user).exists()
            or can_view_all_calendar(request.user)
            ),
        },
    )
@login_required(login_url="/login/")
@require_POST
def complete_appointment_view(request, appointment_id):
    appt = get_object_or_404(Appointment, id=appointment_id)

    if not can_modify_appointment(request.user, appt):
        return HttpResponseForbidden("Não podes concluir esta marcação.")

    if appt.status == "completed":
        return redirect(request.META.get("HTTP_REFERER", "/"))

    old_status = appt.status
    appt.status = "completed"
    appt.save(update_fields=["status"])

    log_appt(
        AppointmentLog.ACTION_COMPLETED,
        appt,
        request.user,
        old_status=old_status,
        new_status=appt.status,
    )

    messages.success(request, "Marcação marcada como concluída.")

    if can_view_all_calendar(request.user):
        return redirect("professional_calendar")

    return redirect("my_appointments")

@professional_required
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

#---- API AJAX

@require_GET
@rate_limit(
    name="api_prof_by_service_ip_minute",
    limit=60,
    window=60,
    by_ip=True,
    methods=["GET"],
)
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

    data = [
        {"id": p.id, "label": p.user.get_full_name() or p.user.username}
        for p in qs
    ]
    return JsonResponse({"results": data})
