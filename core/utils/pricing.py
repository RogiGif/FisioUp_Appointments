from decimal import Decimal, ROUND_HALF_UP

from core.models import Appointment, PartnerServicePrice


def _quantize(value):
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


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


def _resolve_partner_discount(client_profile):
    if not client_profile or not getattr(client_profile, "partner", None):
        return ("none", Decimal("0.00"))
    partner = client_profile.partner
    if partner.discount_type == "percent" and partner.discount_percent is not None:
        return ("percent", Decimal(partner.discount_percent))
    if partner.discount_type == "fixed" and partner.discount_amount is not None:
        return ("fixed", Decimal(partner.discount_amount))
    return ("none", Decimal("0.00"))


def compute_pricing(service, client_profile):
    client = getattr(client_profile, "user", None) if client_profile else None
    prior_count = 0
    if client:
        prior_count = (
            Appointment.objects
            .filter(client=client, service=service)
            .exclude(status="cancelled")
            .count()
        )
    session_index = prior_count + 1
    is_first = session_index == 1

    base_price_raw, tier = _service_price_for_tier(service, is_first)
    base_price = _quantize(base_price_raw)

    partner = None
    partner_price = None
    if client_profile and getattr(client_profile, "partner", None):
        partner = client_profile.partner
        psp = PartnerServicePrice.objects.filter(partner=partner, service=service).first()
        if psp:
            partner_price_raw, tier = _partner_price_for_tier(psp, is_first)
            partner_price = _quantize(partner_price_raw)

    price_before_discount = partner_price if partner_price is not None else base_price

    discount_type, discount_value = _resolve_discount(client_profile)
    if discount_type == "none":
        discount_type, discount_value = _resolve_partner_discount(client_profile)

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
