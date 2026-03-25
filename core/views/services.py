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


def backoffice_services_list_view(request):
    if not is_admin_role(request.user):
        messages.error(request, "Sem permissões.")
        return redirect("backoffice_dashboard")
    q = (request.GET.get("q") or "").strip()
    service_type = (request.GET.get("type") or "").strip()
    per_page = request.GET.get("per_page") or "5"
    try:
        per_page = int(per_page)
    except (TypeError, ValueError):
        per_page = 5
    if per_page not in (5, 10, 15, 25, 50):
        per_page = 5

    qs = Service.objects.all().order_by("name")
    if q:
        qs = apply_terms_filter(qs, q, ["name__icontains"])
    if service_type:
        qs = qs.filter(service_type=service_type)

    paginator = Paginator(qs, per_page)
    page_obj = paginator.get_page(request.GET.get("page") or 1)

    query_params = {}
    if q:
        query_params["q"] = q
    if service_type:
        query_params["type"] = service_type
    if per_page:
        query_params["per_page"] = per_page

    return render(
        request,
        "backoffice/services_list.html",
        {
            "services": page_obj.object_list,
            "page_obj": page_obj,
            "paginator": paginator,
            "q": q,
            "per_page": per_page,
            "service_type": service_type,
            "service_type_choices": Service.SERVICE_TYPE_CHOICES,
            "query_prefix": urlencode(query_params),
            "return_to": request.get_full_path(),
        },
    )


def backoffice_service_create_view(request):
    if not is_admin_role(request.user):
        messages.error(request, "Sem permissões.")
        return redirect("backoffice_dashboard")
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


def backoffice_service_edit_view(request, service_id):
    if not is_admin_role(request.user):
        messages.error(request, "Sem permissões.")
        return redirect("backoffice_dashboard")
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
