from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Q
from core.models import Appointment, PartnerServicePrice
from django.utils import timezone


def _quantize(value):
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _apply_pricing_to_appointment(appt, pricing):
    changed = (
        appt.base_price != pricing["base_price_applied"]
        or appt.partner_id != (pricing["partner"].id if pricing["partner"] else None)
        or appt.partner_price != pricing["partner_price_applied"]
        or appt.discount_type != pricing["discount_type"]
        or appt.discount_value != pricing["discount_value"]
        or appt.final_price != pricing["final_price"]
        or appt.session_index != pricing["session_index"]
        or appt.pricing_tier != pricing["pricing_tier"]
        or appt.base_price_applied != pricing["base_price_applied"]
        or appt.partner_price_applied != pricing["partner_price_applied"]
        or appt.discount_applied != pricing["discount_applied"]
    )
    if not changed:
        return False

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
    appt.save(
        update_fields=[
            "base_price",
            "partner",
            "partner_price",
            "discount_type",
            "discount_value",
            "final_price",
            "session_index",
            "pricing_tier",
            "base_price_applied",
            "partner_price_applied",
            "discount_applied",
        ]
    )
    return True


def _service_price_for_tier(service, is_first):
    if service.pricing_mode == "first_followup":
        if is_first:
            return Decimal(service.price_first or 0), "first"
        return Decimal(service.price_followup or 0), "followup"
    return Decimal(service.price or 0), "single"


def _partner_price_for_tier(psp, is_first):
    if psp.pricing_mode == "first_followup":
        if is_first:
            return Decimal(psp.price_first or 0), "first"
        return Decimal(psp.price_followup or 0), "followup"
    return Decimal(psp.price or 0), "single"


def _resolve_discount(client_profile):
    if not client_profile:
        return ("none", Decimal("0.00"))
    if client_profile.discount_type == "percent" and client_profile.discount_percent is not None:
        return ("percent", Decimal(client_profile.discount_percent))
    if client_profile.discount_type == "fixed" and client_profile.discount_amount is not None:
        return ("fixed", Decimal(client_profile.discount_amount))
    return ("none", Decimal("0.00"))


def normalize_pricing_tier_override(raw_value, service=None):
    value = (raw_value or "").strip()
    if service and getattr(service, "pricing_mode", "") != "first_followup":
        return ""
    return value if value in {"first", "followup"} else ""


def _resolve_session_index(service, client, *, appointment=None, date_obj=None, time_obj=None):
    if not client:
        return 1

    queryset = (
        Appointment.objects
        .filter(client=client, service=service)
        .exclude(status=Appointment.STATUS_CANCELLED)
    )

    appointment_id = getattr(appointment, "id", None)
    if appointment_id:
        queryset = queryset.exclude(id=appointment_id)

    reference_date = date_obj or getattr(appointment, "date", None)
    reference_time = time_obj or getattr(appointment, "time", None)
    if reference_date and reference_time:
        earlier_filter = Q(date__lt=reference_date) | Q(date=reference_date, time__lt=reference_time)
        if appointment_id:
            earlier_filter |= Q(date=reference_date, time=reference_time, id__lt=appointment_id)
        prior_count = queryset.filter(earlier_filter).count()
    else:
        prior_count = queryset.count()

    return prior_count + 1


def compute_pricing(service, client_profile, *, appointment=None, date_obj=None, time_obj=None, pricing_tier_override=None):
    client = getattr(client_profile, "user", None) if client_profile else getattr(appointment, "client", None)
    session_index = _resolve_session_index(
        service,
        client,
        appointment=appointment,
        date_obj=date_obj,
        time_obj=time_obj,
    )
    effective_override = normalize_pricing_tier_override(
        pricing_tier_override if pricing_tier_override is not None else getattr(appointment, "pricing_tier_override", ""),
        service,
    )
    if effective_override == "first":
        session_index = 1
        is_first = True
    elif effective_override == "followup":
        session_index = max(session_index, 2)
        is_first = False
    else:
        is_first = session_index == 1

    base_price_raw, tier = _service_price_for_tier(service, is_first)
    base_price = _quantize(base_price_raw)

    partner = None
    partner_price = None
    if client_profile and getattr(client_profile, "partner", None):
        partner = client_profile.partner
        psp = PartnerServicePrice.objects.filter(partner=partner, service=service).first()
        if psp and psp.is_enabled:
            if psp.discount_type and psp.discount_type != "none":
                if psp.discount_type == "percent" and psp.discount_percent is not None:
                    discount_value = Decimal(psp.discount_percent)
                    partner_price_raw = base_price_raw * (Decimal("1.00") - (discount_value / Decimal("100")))
                    partner_price = _quantize(max(partner_price_raw, Decimal("0.00")))
                elif psp.discount_type == "fixed" and psp.discount_amount is not None:
                    discount_value = Decimal(psp.discount_amount)
                    partner_price_raw = base_price_raw - discount_value
                    partner_price = _quantize(max(partner_price_raw, Decimal("0.00")))
            if partner_price is None:
                partner_price_raw, tier = _partner_price_for_tier(psp, is_first)
                partner_price = _quantize(partner_price_raw)

    price_before_discount = partner_price if partner_price is not None else base_price

    discount_type, discount_value = _resolve_discount(client_profile)
    if discount_type == "none":
        discount_type, discount_value = ("none", Decimal("0.00"))

    discount_value = _quantize(Decimal(discount_value or 0))
    discount_applied = Decimal("0.00")
    if discount_type == "percent":
        discount_applied = price_before_discount * (discount_value / Decimal("100"))
    elif discount_type == "fixed":
        discount_applied = min(discount_value, price_before_discount)

    discount_applied = _quantize(discount_applied)
    final_price = price_before_discount - discount_applied
    if final_price < 0:
        final_price = Decimal("0.00")
    final_price = _quantize(final_price)

    return {
        "session_index": session_index,
        "pricing_tier": tier,
        "base_price_applied": base_price,
        "partner_price_applied": _quantize(partner_price or Decimal("0.00")),
        "discount_applied": discount_applied,
        "final_price": final_price,
        "base_price": base_price,
        "partner": partner,
        "partner_price": _quantize(partner_price or Decimal("0.00")),
        "discount_type": discount_type,
        "discount_value": discount_value,
    }


def _monthly_group_service_price(service):
    if service.pricing_mode == "first_followup":
        followup = Decimal(service.price_followup or 0)
        first = Decimal(service.price_first or 0)
        chosen = followup if followup > 0 else first
        return chosen, "monthly_group"
    return Decimal(service.price or 0), "monthly_group"


def _monthly_group_partner_price(psp):
    if psp.pricing_mode == "first_followup":
        followup = Decimal(psp.price_followup or 0)
        first = Decimal(psp.price_first or 0)
        chosen = followup if followup > 0 else first
        return chosen, "monthly_group"
    return Decimal(psp.price or 0), "monthly_group"


def compute_group_monthly_pricing(service, client_profile, monthly_price_override=None):
    """
    Pricing de mensalidade de turma.
    O preço base vem do serviço (mensal) e pode ser personalizado por inscrito.
    """
    if monthly_price_override is not None:
        base_price_raw = Decimal(monthly_price_override)
        tier = "monthly_override"
        skip_partner_price_rules = True
    else:
        base_price_raw, tier = _monthly_group_service_price(service)
        skip_partner_price_rules = False

    base_price = _quantize(base_price_raw)

    partner = None
    partner_price = None
    if not skip_partner_price_rules and client_profile and getattr(client_profile, "partner", None):
        partner = client_profile.partner
        psp = PartnerServicePrice.objects.filter(partner=partner, service=service).first()
        if psp and psp.is_enabled:
            if psp.discount_type and psp.discount_type != "none":
                if psp.discount_type == "percent" and psp.discount_percent is not None:
                    discount_value = Decimal(psp.discount_percent)
                    partner_price_raw = base_price_raw * (Decimal("1.00") - (discount_value / Decimal("100")))
                    partner_price = _quantize(max(partner_price_raw, Decimal("0.00")))
                elif psp.discount_type == "fixed" and psp.discount_amount is not None:
                    discount_value = Decimal(psp.discount_amount)
                    partner_price_raw = base_price_raw - discount_value
                    partner_price = _quantize(max(partner_price_raw, Decimal("0.00")))
            if partner_price is None:
                partner_price_raw, tier = _monthly_group_partner_price(psp)
                partner_price = _quantize(partner_price_raw)

    price_before_discount = partner_price if partner_price is not None else base_price

    discount_type, discount_value = _resolve_discount(client_profile)
    if discount_type == "none":
        discount_type, discount_value = ("none", Decimal("0.00"))

    discount_value = _quantize(Decimal(discount_value or 0))
    discount_applied = Decimal("0.00")
    if discount_type == "percent":
        discount_applied = price_before_discount * (discount_value / Decimal("100"))
    elif discount_type == "fixed":
        discount_applied = min(discount_value, price_before_discount)

    discount_applied = _quantize(discount_applied)
    final_price = price_before_discount - discount_applied
    if final_price < 0:
        final_price = Decimal("0.00")
    final_price = _quantize(final_price)

    return {
        "session_index": 1,
        "pricing_tier": tier,
        "base_price_applied": base_price,
        "partner_price_applied": _quantize(partner_price or Decimal("0.00")),
        "discount_applied": discount_applied,
        "final_price": final_price,
        "base_price": base_price,
        "partner": partner,
        "partner_price": _quantize(partner_price or Decimal("0.00")),
        "discount_type": discount_type,
        "discount_value": discount_value,
    }


def recalculate_upcoming_appointment_prices(client_profile, *, service_ids=None):
    """
    Recalcula preços de marcações futuras não pagas do cliente quando
    há alterações de parceria/descontos no perfil.
    """
    if not client_profile or not getattr(client_profile, "user_id", None):
        return 0

    today = timezone.localdate()
    now_t = timezone.localtime().time()
    upcoming_q = Q(date__gt=today) | Q(date=today, time__gte=now_t)
    appointments = (
        Appointment.objects
        .select_related("service")
        .filter(
            client_id=client_profile.user_id,
            is_paid=False,
        )
        .exclude(status=Appointment.STATUS_CANCELLED)
        .exclude(status=Appointment.STATUS_COMPLETED)
        .filter(upcoming_q)
    )
    if service_ids:
        appointments = appointments.filter(service_id__in=list(service_ids))
    appointments = appointments.order_by("date", "time", "id")

    updated_count = 0
    for appt in appointments:
        pricing = compute_pricing(appt.service, client_profile, appointment=appt)
        if _apply_pricing_to_appointment(appt, pricing):
            updated_count += 1

    return updated_count


def recalculate_partner_upcoming_appointments(partner, *, service_ids=None):
    if not partner:
        return 0

    today = timezone.localdate()
    now_t = timezone.localtime().time()
    upcoming_q = Q(date__gt=today) | Q(date=today, time__gte=now_t)

    appointments = (
        Appointment.objects
        .select_related("service", "client", "client__client_profile")
        .filter(
            is_paid=False,
        )
        .exclude(status=Appointment.STATUS_CANCELLED)
        .exclude(status=Appointment.STATUS_COMPLETED)
        .filter(upcoming_q)
        .filter(
            Q(partner=partner) | Q(client__client_profile__partner=partner)
        )
        .distinct()
    )
    if service_ids:
        appointments = appointments.filter(service_id__in=list(service_ids))
    appointments = appointments.order_by("date", "time", "id")

    updated_count = 0
    for appt in appointments:
        client_profile = getattr(appt.client, "client_profile", None)
        pricing = compute_pricing(appt.service, client_profile, appointment=appt)
        if _apply_pricing_to_appointment(appt, pricing):
            updated_count += 1

    return updated_count
