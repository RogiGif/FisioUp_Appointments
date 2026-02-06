from __future__ import annotations

from datetime import datetime
from typing import Dict, Iterable, List, Tuple

from django.utils import timezone

from core.models import ClientProfile, MoloniIntegration
from core.services import moloni as moloni_service


def _safe_get(d: Dict, *keys, default=""):
    for k in keys:
        if k in d and d[k]:
            return d[k]
    return default


def _normalize_vat(value: str) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


def sync_customers(full: bool = False, since: str | None = None) -> Dict[str, int]:
    created = 0
    updated = 0
    skipped = 0
    errors = 0

    page = 1
    while True:
        data = moloni_service.customers_get_all(page=page)
        customers = data.get("customers") if isinstance(data, dict) else data
        if not customers:
            break

        for c in customers:
            try:
                vat = _normalize_vat(_safe_get(c, "vat", "nif", "fiscal_id"))
                if not vat:
                    skipped += 1
                    continue

                full_name = _safe_get(c, "name", "company_name", "company")
                phone = _safe_get(c, "phone", "mobile", "telephone")
                email = _safe_get(c, "email")
                address = _safe_get(c, "address", "address_1", "address1")
                postal_code = _safe_get(c, "zip_code", "postal_code")
                city = _safe_get(c, "city", "locality")
                moloni_id = str(_safe_get(c, "customer_id", "id", "customerId"))

                profile = ClientProfile.objects.filter(nif=vat).first()
                if profile:
                    changed = False
                    if moloni_id and not profile.moloni_customer_id:
                        profile.moloni_customer_id = moloni_id
                        changed = True
                    if full_name and not profile.full_name:
                        profile.full_name = full_name
                        changed = True
                    if phone and not profile.phone:
                        profile.phone = phone
                        changed = True
                    if email and profile.user and not profile.user.email:
                        profile.user.email = email
                        profile.user.save(update_fields=["email"])
                    if address and not profile.address_line1:
                        profile.address_line1 = address
                        changed = True
                    if postal_code and not profile.postal_code:
                        profile.postal_code = postal_code
                        changed = True
                    if city and not profile.city:
                        profile.city = city
                        changed = True
                    if changed:
                        profile.save()
                        updated += 1
                    else:
                        skipped += 1
                else:
                    profile = ClientProfile.objects.create(
                        user=None,
                        full_name=full_name or "—",
                        nif=vat,
                        phone=phone or "",
                        address_line1=address or "",
                        postal_code=postal_code or "",
                        city=city or "",
                        moloni_customer_id=moloni_id or "",
                    )
                    created += 1
            except Exception:
                errors += 1

        page += 1
        if isinstance(data, dict) and data.get("total_pages"):
            if page > int(data.get("total_pages")):
                break

    integ = MoloniIntegration.get_solo()
    integ.last_sync_at = timezone.now()
    integ.save(update_fields=["last_sync_at", "updated_at"])

    return {"created": created, "updated": updated, "skipped": skipped, "errors": errors}
