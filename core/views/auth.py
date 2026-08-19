from datetime import datetime, timedelta, time as dtime
from decimal import Decimal
from collections import defaultdict
from dataclasses import dataclass
from uuid import uuid4
import logging
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
from core.services.audit import log_audit_event
from core.services import moloni as moloni_service
from core.session_timeout import get_session_timeout_config
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
    MoloniIntegration,
    ClientImportLog,
    ClientImportBatch,
    ClientImportRow,
    Partner,
    PartnerServicePrice,
)

from core.views.common import *

logger = logging.getLogger(__name__)


def _get_real_ip(request) -> str:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def _moloni_auto_sync_is_ready():
    if not moloni_service.is_configured():
        return False
    integ = MoloniIntegration.get_solo()
    return bool(integ.refresh_token and moloni_service.get_company_id())


def _sync_client_profile_with_moloni(profile, request, *, source):
    try:
        result = moloni_service.sync_client_profile(profile)
    except moloni_service.MoloniError as exc:
        log_audit_event(
            category="integrations",
            action="moloni_customer_sync_failed",
            request=request,
            actor=getattr(request, "user", None),
            instance=profile,
            source=source,
            message="Falha na sincronização do cliente com a Moloni.",
            after={"error": str(exc), "manual": False},
        )
        messages.warning(request, f"Registo submetido, mas a sincronização com a Moloni falhou: {exc}")
        return None
    except Exception:
        logger.exception(
            "Unexpected error while syncing client profile %s with Moloni from %s",
            getattr(profile, "pk", None),
            source,
        )
        log_audit_event(
            category="integrations",
            action="moloni_customer_sync_failed",
            request=request,
            actor=getattr(request, "user", None),
            instance=profile,
            source=source,
            message="Falha inesperada na sincronização do cliente com a Moloni.",
            after={
                "error": "Erro inesperado durante a sincronização Moloni.",
                "exception_type": "unexpected",
                "manual": False,
            },
        )
        messages.warning(
            request,
            "Registo submetido, mas a sincronização com a Moloni falhou por erro inesperado.",
        )
        return None

    log_audit_event(
        category="integrations",
        action="moloni_customer_synced",
        request=request,
        actor=getattr(request, "user", None),
        instance=profile,
        source=source,
        message="Cliente sincronizado com a Moloni.",
        after=result,
    )
    return result


def home_view(request):
    """
    Home "premium":
    - Se user logado e for cliente: mostra próximas marcações e alerta de perfil incompleto
    - Se user for staff: mostra stats rápidas (hoje / pendentes / concluídas)
    """
    if request.user.is_authenticated:
        if can_access_backoffice(request.user):
            return redirect("backoffice_dashboard")
        if Professional.objects.filter(user=request.user).exists():
            return redirect("professional_calendar")
        return redirect("my_appointments")

    return redirect("login")


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
            log_audit_event(
                category="auth",
                action="login_success",
                request=request,
                actor=user,
                source="login",
                message="Login efetuado.",
                metadata={"next": next_url or ""},
            )

            # Se o utilizador vinha de um sítio específico, vai para lá
            if next_url:
                return redirect(next_url)

            # Caso contrário, escolhe landing page por role
            is_professional = Professional.objects.filter(user=user).exists()
            if can_access_backoffice(user):
                return redirect("backoffice_dashboard")
            if is_professional:
                return redirect("professional_calendar")
            return redirect("my_appointments")

        message = "Credenciais inválidas."
        log_audit_event(
            category="auth",
            action="login_failed",
            request=request,
            source="login",
            message=message,
            metadata={"username": email},
        )

    # Para GET: manda o next para o template (pode vir vazio)
    return render(request, "core/login.html", {"message": message, "next": next_url})


def logout_view(request):
    reason = (request.GET.get("reason") or "").strip().lower()
    if reason == "timeout":
        log_audit_event(
            category="auth",
            action="session_timeout",
            request=request,
            actor=request.user,
            source="session_timeout",
            message="Sessão terminada por inatividade.",
        )
        messages.warning(request, "Sessão expirada por inatividade.")
    else:
        log_audit_event(
            category="auth",
            action="logout",
            request=request,
            actor=request.user,
            source="logout",
            message="Logout efetuado.",
        )
    logout(request)
    return redirect("/login/")


@login_required
@require_POST
def session_keepalive_view(request):
    config = get_session_timeout_config(request.user)
    return JsonResponse(
        {
            "ok": True,
            "timeout_seconds": config["timeout_seconds"],
            "warning_seconds": config["warning_seconds"],
            "keepalive_interval_seconds": config["keepalive_interval_seconds"],
        }
    )


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
            accepted_terms = form.cleaned_data.get("accepted_terms", False)
            terms_accepted = accepted_terms
            rgpd_accepted = accepted_terms
            city = locality
            accepted_terms_at = timezone.now()
            accepted_terms_ip = _get_real_ip(request)
            accepted_terms_user_agent = request.META.get("HTTP_USER_AGENT", "")

            settings_obj = clinic_settings()
            profile = ClientProfile.objects.filter(nif=nif).first()
            profile_by_name_phone = find_existing_client_by_name_phone(full_name, phone)
            if profile_by_name_phone and (not profile or profile_by_name_phone.id != profile.id):
                if profile_by_name_phone.user_id:
                    form.add_error(
                        None,
                        "Já existe uma conta com este nome e contacto. Faz login ou recupera a palavra-passe.",
                    )
                else:
                    form.add_error(
                        None,
                        "Já existe um cliente com este nome e contacto. Contacta a clínica para ativação.",
                    )
                return render(request, "core/register.html", {"form": form})
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
                    profile.accepted_terms_at = accepted_terms_at
                    profile.accepted_terms_ip = accepted_terms_ip
                    profile.accepted_terms_user_agent = accepted_terms_user_agent
                    profile.registration_status = "pending"
                    profile.registration_requested_at = timezone.now()
                    profile.require_complete_profile = True
                    profile.updated_by = user
                    profile.save()
                    target_profile = profile
                else:
                    target_profile = ClientProfile.objects.create(
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
                        accepted_terms_at=accepted_terms_at,
                        accepted_terms_ip=accepted_terms_ip,
                        accepted_terms_user_agent=accepted_terms_user_agent,
                        registration_status="pending",
                        registration_requested_at=timezone.now(),
                        require_complete_profile=True,
                        created_by=user,
                        updated_by=user,
                    )

                if target_profile.nif and _moloni_auto_sync_is_ready():
                    _sync_client_profile_with_moloni(
                        target_profile,
                        request,
                        source="public_register",
                    )

                if settings_obj.notify_admin_on_pending_registration:
                    clinic_to = clinic_email()
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
