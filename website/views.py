from __future__ import annotations

import re
import unicodedata
from decimal import Decimal

from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static
from django.utils import timezone

from core.models import ClinicSettings, ContentPost, Partner, Professional, Service

ABOUT_CONTENT = {
    "paragraphs": [
        "A Fisio UP e um espaco moderno e acolhedor em Canas de Senhorim. Desde setembro de 2016, prestamos servicos de excelencia nas areas da saude e do bem-estar.",
        "Acompanhamos utentes desde a idade pediatrica a geriatrica, com solucoes terapeuticas ajustadas a cada situacao clinica.",
    ],
    "mission": "Prestar servicos de saude e bem-estar com elevados padroes de qualidade, sustentados por uma equipa altamente qualificada.",
    "vision": "Ser uma referencia na prestacao de cuidados de saude, reconhecida pela qualidade, proximidade e resultados.",
    "values": [
        "Competencia",
        "Personalizacao",
        "Humanizacao",
        "Etica",
        "Excelencia",
        "Inovacao",
    ],
}

CONTACT_PLACEHOLDERS = {
    "address": "Rua do Rossio, Loteamento das 4 Esquinas, Lote 2 - Loja 1, 3525-064 Canas de Senhorim",
    "phone": "+352 232 395 560",
}


def _model_has_field(model, field_name: str) -> bool:
    return field_name in {field.name for field in model._meta.get_fields()}


def _split_emails(raw_value: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if raw_value is None:
        return []
    if isinstance(raw_value, (list, tuple, set)):
        values = [str(item).strip() for item in raw_value if str(item).strip()]
        return values
    raw_text = str(raw_value)
    return [part.strip() for part in re.split(r"[,;\n]+", raw_text) if part.strip()]


def _initials(name: str) -> str:
    parts = [part for part in (name or "").split() if part]
    if not parts:
        return "FU"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return f"{parts[0][0]}{parts[-1][0]}".upper()


def _money(value: Decimal | None) -> str:
    if value is None:
        return ""
    return f"{value:.2f}".replace(".", ",")


def _service_price_label(service: Service) -> str:
    pricing_mode = getattr(service, "pricing_mode", "")
    if pricing_mode == "first_followup":
        price_first = getattr(service, "price_first", None)
        price_followup = getattr(service, "price_followup", None)
        if price_first or price_followup:
            return f"1a consulta {_money(price_first)} EUR | seguintes {_money(price_followup)} EUR"
        return ""
    price = getattr(service, "price", None)
    if price and price > 0:
        return f"{_money(price)} EUR"
    return ""


def _normalize_service_name(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.lower().strip()


def _service_icon_filename(service: Service) -> str:
    normalized_name = _normalize_service_name(getattr(service, "name", ""))
    rules = [
        (("fisioterapia",), "fisioterapia.svg"),
        (("pilates",), "pilates.svg"),
        (("nutricao", "nutri"), "nutricao.svg"),
        (("psicologia",), "psicologia.svg"),
        (("acupuntura",), "acupuntura.svg"),
        (("terapia ocupacional",), "terapia_ocupacional.svg"),
        (("terapia da fala", "terapia fala", "fala"), "terapia_fala.svg"),
    ]
    for keywords, filename in rules:
        if any(keyword in normalized_name for keyword in keywords):
            return filename
    return "fisioterapia.svg"


def _service_preview_image_url(service: Service) -> str:
    normalized_name = _normalize_service_name(getattr(service, "name", ""))
    rules = [
        (("acupuntura",), "website/dente/images/replacements/acupuntura_4.png"),
        (("fisioterapia",), "website/dente/images/replacements/image_2_top_right.png"),
        (("nutricao", "nutri"), "website/dente/images/replacements/image_3_bottom_left.png"),
        (("pilates",), "website/dente/images/replacements/image_4_bottom_right.png"),
        (("psicologia",), "website/dente/images/replacements/panel1_top_left.png"),
        (("terapia ocupacional",), "website/dente/images/replacements/panel2_top_right.png"),
        (("terapia da fala", "terapia fala", "fala"), "website/dente/images/replacements/panel3_middle_left.png"),
    ]
    for keywords, path in rules:
        if any(keyword in normalized_name for keyword in keywords):
            return static(path)
    return static("website/dente/images/img_1.jpg")


def _published_posts_queryset():
    now = timezone.now()
    return (
        ContentPost.objects.filter(status="published")
        .filter(Q(published_at__isnull=True) | Q(published_at__lte=now))
        .exclude(slug__isnull=True)
        .exclude(slug="")
        .select_related("author")
        .order_by("-is_featured", "-published_at", "-created_at")
    )


def _clinic_public_data() -> dict:
    settings_obj = ClinicSettings.objects.first()

    clinic_name = "Fisio UP"
    clinic_email = getattr(settings, "CLINIC_EMAIL", "") or "geral@fisio-up.pt"
    clinic_address = getattr(settings, "CLINIC_ADDRESS", "") or CONTACT_PLACEHOLDERS["address"]
    clinic_phone = getattr(settings, "CLINIC_PHONE", "") or CONTACT_PLACEHOLDERS["phone"]
    notification_emails = _split_emails(getattr(settings, "CLINIC_NOTIFICATION_EMAILS", []))
    logo_url = static("core/images/logo_fisioUP_alto.svg")

    if settings_obj:
        clinic_name = (settings_obj.clinic_name or clinic_name).strip() or clinic_name
        if settings_obj.clinic_email:
            clinic_email = settings_obj.clinic_email.strip()
        if settings_obj.logo:
            logo_url = settings_obj.logo.url
        db_address = (getattr(settings_obj, "clinic_address", "") or "").strip()
        db_phone = (getattr(settings_obj, "clinic_phone", "") or "").strip()
        if db_address:
            clinic_address = db_address
        if db_phone:
            clinic_phone = db_phone
        emails_from_settings = _split_emails(settings_obj.notification_emails)
        if emails_from_settings:
            notification_emails = emails_from_settings

    if not notification_emails and clinic_email:
        notification_emails = [clinic_email]

    return {
        "clinic_name": clinic_name,
        "clinic_email": clinic_email,
        "notification_emails": notification_emails,
        "logo_url": logo_url,
        "address": clinic_address,
        "phone": clinic_phone,
    }


def _serialize_professional(professional: Professional) -> dict:
    display_name = professional.user.get_full_name() or professional.user.username
    services_names = [service.name for service in list(professional.services.all())[:3]]
    speciality = (professional.speciality or "").strip() or ", ".join(services_names)
    description_source = (
        (getattr(professional, "mini_bio", "") or "").strip()
        or (getattr(professional, "bio", "") or "").strip()
        or (getattr(professional, "description", "") or "").strip()
    )
    description = description_source or (
        f"Acompanhamento especializado em {speciality.lower()}."
        if speciality
        else "Acompanhamento personalizado com foco na recuperacao e no bem-estar."
    )
    return {
        "name": display_name,
        "speciality": speciality or "Fisioterapia",
        "description": description,
        "photo_url": professional.profile_photo.url if professional.profile_photo else "",
        "initials": _initials(display_name),
    }


def _serialize_service(service: Service) -> dict:
    service_type = getattr(service, "service_type", "")
    description_source = (
        (getattr(service, "short_description", "") or "").strip()
        or (getattr(service, "description", "") or "").strip()
    )
    if description_source:
        summary = description_source
    elif service_type == "group":
        summary = "Sessao orientada em grupo, com acompanhamento tecnico dedicado."
    else:
        summary = "Sessao individual adaptada a objetivos terapeuticos especificos."

    return {
        "name": service.name,
        "summary": summary,
        "duration": getattr(service, "duration_minutes", None),
        "price_label": _service_price_label(service),
        "icon_filename": _service_icon_filename(service),
        "preview_image_url": _service_preview_image_url(service),
    }


def _serialize_partner(partner: Partner) -> dict:
    logo_file = getattr(partner, "logo", None)
    logo_url = logo_file.url if logo_file else ""
    link = (getattr(partner, "link", "") or getattr(partner, "url", "") or "").strip()
    return {
        "name": partner.name,
        "notes": (partner.notes or "").strip(),
        "initials": _initials(partner.name),
        "logo_url": logo_url,
        "link": link,
    }


def _serialize_post(post: ContentPost) -> dict:
    excerpt = (post.excerpt or "").strip()
    if not excerpt:
        excerpt = (post.body or "").strip()[:180]
        if len(post.body or "") > 180:
            excerpt += "..."

    published = post.published_at or post.created_at

    return {
        "title": post.title,
        "slug": post.slug,
        "excerpt": excerpt,
        "cover_url": post.cover_image.url if post.cover_image else "",
        "published": published,
    }


def _build_base_context(*, active_page: str, page_title: str, meta_description: str, clinic: dict | None = None) -> dict:
    clinic_data = clinic or _clinic_public_data()
    recent_posts = [_serialize_post(post) for post in _published_posts_queryset()[:3]]
    return {
        "active_page": active_page,
        "page_title": page_title,
        "meta_description": meta_description,
        "clinic": clinic_data,
        "about_content": ABOUT_CONTENT,
        "recent_posts": recent_posts,
        "current_year": timezone.now().year,
    }


def home(request):
    clinic = _clinic_public_data()
    services_qs = Service.objects.all()
    if _model_has_field(Service, "active"):
        services_qs = services_qs.filter(active=True)
    services = [_serialize_service(service) for service in services_qs.order_by("name")[:6]]

    professionals_qs = Professional.objects.select_related("user").prefetch_related("services").filter(user__is_active=True)
    if _model_has_field(Professional, "active"):
        professionals_qs = professionals_qs.filter(active=True)
    professional_ordering = ["user__first_name", "user__last_name", "user__username"]
    if _model_has_field(Professional, "position"):
        professional_ordering = ["position"] + professional_ordering
    professionals_qs = professionals_qs.order_by(*professional_ordering)

    team = [_serialize_professional(professional) for professional in professionals_qs[:4]]

    partners_qs = Partner.objects.all()
    if _model_has_field(Partner, "active"):
        partners_qs = partners_qs.filter(active=True)
    partners = [_serialize_partner(partner) for partner in partners_qs.order_by("name")[:6]]

    posts = [_serialize_post(post) for post in _published_posts_queryset()[:3]]

    context = _build_base_context(
        active_page="home",
        clinic=clinic,
        page_title="Fisio UP | Fisioterapia e Bem-Estar em Canas de Senhorim",
        meta_description="Conheca a Fisio UP, os nossos servicos, equipa multidisciplinar e marque a sua consulta de forma simples.",
    )
    context.update(
        {
            "services": services,
            "team": team,
            "partners": partners,
            "posts": posts,
        }
    )
    return render(request, "website/home.html", context)


def about(request):
    context = _build_base_context(
        active_page="about",
        page_title="Sobre Nos | Fisio UP",
        meta_description="Saiba mais sobre a historia, missao, visao e valores da Fisio UP.",
    )
    return render(request, "website/about.html", context)


def team(request):
    professionals = Professional.objects.select_related("user").prefetch_related("services").filter(user__is_active=True)
    if _model_has_field(Professional, "active"):
        professionals = professionals.filter(active=True)
    professional_ordering = ["user__first_name", "user__last_name", "user__username"]
    if _model_has_field(Professional, "position"):
        professional_ordering = ["position"] + professional_ordering
    professionals = professionals.order_by(*professional_ordering)

    context = _build_base_context(
        active_page="team",
        page_title="A Equipa | Fisio UP",
        meta_description="Conheca a equipa da Fisio UP e as especialidades disponiveis.",
    )
    context["team"] = [_serialize_professional(professional) for professional in professionals]
    return render(request, "website/team.html", context)


def services(request):
    context = _build_base_context(
        active_page="services",
        page_title="Servicos | Fisio UP",
        meta_description="Explore os servicos de fisioterapia e bem-estar disponiveis na Fisio UP.",
    )
    services_qs = Service.objects.all()
    if _model_has_field(Service, "active"):
        services_qs = services_qs.filter(active=True)
    context["services"] = [_serialize_service(service) for service in services_qs.order_by("name")]
    return render(request, "website/services.html", context)


def partners(request):
    context = _build_base_context(
        active_page="partners",
        page_title="Parcerias | Fisio UP",
        meta_description="Veja as parcerias ativas da Fisio UP.",
    )
    partners_qs = Partner.objects.all()
    if _model_has_field(Partner, "active"):
        partners_qs = partners_qs.filter(active=True)
    context["partners"] = [_serialize_partner(partner) for partner in partners_qs.order_by("name")]
    return render(request, "website/partners.html", context)


def highlights(request):
    posts_qs = _published_posts_queryset()
    paginator = Paginator(posts_qs, 6)
    page_obj = paginator.get_page(request.GET.get("page"))

    context = _build_base_context(
        active_page="highlights",
        page_title="Destaques | Fisio UP",
        meta_description="Noticias, novidades e destaques da Fisio UP.",
    )
    context["page_obj"] = page_obj
    context["posts"] = [_serialize_post(post) for post in page_obj.object_list]
    return render(request, "website/highlights_list.html", context)


def highlight_detail(request, slug: str):
    post = get_object_or_404(_published_posts_queryset(), slug=slug)

    context = _build_base_context(
        active_page="highlights",
        page_title=f"{post.title} | Fisio UP",
        meta_description=((post.excerpt or post.body or "")[:160]).strip() or "Destaque da Fisio UP.",
    )
    context["post"] = {
        "title": post.title,
        "excerpt": post.excerpt,
        "body": post.body,
        "cover_url": post.cover_image.url if post.cover_image else "",
        "published": post.published_at or post.created_at,
    }
    return render(request, "website/highlights_detail.html", context)


def contacts(request):
    clinic = _clinic_public_data()
    context = _build_base_context(
        active_page="contacts",
        clinic=clinic,
        page_title="Contactos | Fisio UP",
        meta_description="Entre em contacto com a Fisio UP para esclarecimentos e marcacoes.",
    )
    return render(request, "website/contacts.html", context)


def book_now_redirect(request):
    return redirect("/login/")
