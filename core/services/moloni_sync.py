from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, Iterable, List

from django.utils import timezone

from core.models import ClientProfile, MoloniIntegration
from core.services import moloni as moloni_service
from core.views.common import (
    build_client_name_phone_key,
    normalize_client_name,
    normalize_email_address,
    normalize_phone_number,
)


def _safe_get(d: Dict[str, Any], *keys: str, default=""):
    for key in keys:
        if key in d and d[key] not in (None, ""):
            return d[key]
    return default


def _normalize_vat(value: str) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


def _normalize_postal_code(value: str) -> str:
    return str(value or "").strip().replace(" ", "").upper()


def _extract_customers(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, dict):
        customers = data.get("customers") or data.get("customer") or []
    else:
        customers = data
    if isinstance(customers, dict):
        return [customers]
    return list(customers or [])


def _format_since(value) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return value.strip()
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_current_timezone())
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M:%S")


def _iter_remote_customers(*, full: bool = False, since: str | None = None) -> Iterable[Dict[str, Any]]:
    integ = MoloniIntegration.get_solo()
    since_value = ""
    if not full:
        since_value = _format_since(since or integ.last_sync_at)

    qty = 50
    offset = 0
    while True:
        if since_value:
            data = moloni_service.customers_get_modified_since(date=since_value, qty=qty, offset=offset)
        else:
            data = moloni_service.customers_get_all(qty=qty, offset=offset)
        customers = _extract_customers(data)
        if not customers:
            break
        for customer in customers:
            yield customer
        if len(customers) < qty:
            break
        offset += qty


def _remote_customer_to_profile_fields(customer: Dict[str, Any]) -> Dict[str, str]:
    return {
        "full_name": _safe_get(customer, "name", "company_name", "company"),
        "phone": _safe_get(customer, "phone", "mobile", "telephone"),
        "address_line1": _safe_get(customer, "address", "address_1", "address1"),
        "postal_code": _safe_get(customer, "zip_code", "postal_code"),
        "city": _safe_get(customer, "city", "locality"),
        "moloni_customer_id": str(_safe_get(customer, "customer_id", "id", "customerId")),
    }


def fetch_remote_customer_by_vat(vat: str) -> Dict[str, Any]:
    vat = _normalize_vat(vat)
    if not vat:
        raise moloni_service.MoloniError("NIF em falta para procurar cliente na Moloni.")
    data = moloni_service.customers_get_by_vat(vat)
    customers = _extract_customers(data)
    if not customers:
        raise moloni_service.MoloniError("Não foi encontrado nenhum cliente na Moloni com este NIF.")
    return customers[0]


def apply_remote_customer_to_profile(profile: ClientProfile) -> Dict[str, Any]:
    remote = fetch_remote_customer_by_vat(profile.nif)
    payload = _remote_customer_to_profile_fields(remote)
    changed_fields = []

    for field_name in ("full_name", "phone", "address_line1", "postal_code", "city", "moloni_customer_id"):
        incoming = (payload.get(field_name) or "").strip()
        current = (getattr(profile, field_name) or "").strip()
        if incoming and incoming != current:
            setattr(profile, field_name, incoming)
            changed_fields.append(field_name)

    if changed_fields:
        profile.save(update_fields=changed_fields + ["updated_at"])

    remote_email = normalize_email_address(_safe_get(remote, "email"))
    if remote_email and profile.user_id and normalize_email_address(profile.user.email) != remote_email:
        profile.user.email = remote_email
        profile.user.save(update_fields=["email"])
        changed_fields.append("user.email")

    return {
        "profile_id": profile.id,
        "remote_customer_id": payload.get("moloni_customer_id", ""),
        "changed_fields": changed_fields,
    }


def sync_customers(full: bool = False, since: str | None = None) -> Dict[str, Any]:
    created = 0
    updated = 0
    skipped = 0
    errors = 0
    processed = 0

    sync_since = _format_since(since or MoloniIntegration.get_solo().last_sync_at)
    for customer in _iter_remote_customers(full=full, since=since):
        processed += 1
        try:
            vat = _normalize_vat(_safe_get(customer, "vat", "nif", "fiscal_id"))
            if not vat:
                skipped += 1
                continue

            full_name = _safe_get(customer, "name", "company_name", "company")
            phone = _safe_get(customer, "phone", "mobile", "telephone")
            email = _safe_get(customer, "email")
            address = _safe_get(customer, "address", "address_1", "address1")
            postal_code = _safe_get(customer, "zip_code", "postal_code")
            city = _safe_get(customer, "city", "locality")
            moloni_id = str(_safe_get(customer, "customer_id", "id", "customerId"))

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
                ClientProfile.objects.create(
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

    integ = MoloniIntegration.get_solo()
    integ.last_sync_at = timezone.now()
    integ.save(update_fields=["last_sync_at", "updated_at"])

    return {
        "mode": "full" if full else "incremental",
        "processed": processed,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "since": sync_since,
    }


def push_local_customers(*, full: bool = False, since: str | None = None) -> Dict[str, Any]:
    integ = MoloniIntegration.get_solo()
    if not moloni_service.is_configured() or not integ.refresh_token or not moloni_service.get_company_id():
        raise moloni_service.MoloniError("Integração Moloni incompleta ou sem empresa definida.")

    qs = ClientProfile.objects.select_related("user").exclude(nif="").order_by("updated_at", "id")
    if not full:
        since_value = since or integ.last_sync_at
        if since_value:
            if isinstance(since_value, str):
                try:
                    parsed = datetime.fromisoformat(since_value.replace("Z", "+00:00"))
                    if timezone.is_naive(parsed):
                        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
                    since_value = parsed
                except Exception:
                    since_value = None
            if since_value:
                qs = qs.filter(updated_at__gte=since_value)

    pushed = 0
    skipped = 0
    errors = 0
    processed = 0
    for profile in qs:
        processed += 1
        try:
            moloni_service.sync_client_profile(profile)
            pushed += 1
        except moloni_service.MoloniError:
            errors += 1
        except Exception:
            errors += 1

    return {
        "mode": "full" if full else "incremental",
        "processed": processed,
        "pushed": pushed,
        "skipped": skipped,
        "errors": errors,
    }


def run_bidirectional_reconciliation(*, full: bool = False, since: str | None = None) -> Dict[str, Any]:
    incoming = sync_customers(full=full, since=since)
    outgoing = push_local_customers(full=full, since=since)
    return {
        "incoming": incoming,
        "outgoing": outgoing,
    }


def build_reconciliation_report(*, limit: int = 100) -> Dict[str, Any]:
    remote_customers = list(_iter_remote_customers(full=True))
    remote_by_vat: Dict[str, Dict[str, Any]] = {}
    for customer in remote_customers:
        vat = _normalize_vat(_safe_get(customer, "vat", "nif", "fiscal_id"))
        if vat and vat not in remote_by_vat:
            remote_by_vat[vat] = customer

    conflicts = []
    local_qs = ClientProfile.objects.select_related("user").order_by("full_name", "id")
    for profile in local_qs.exclude(nif=""):
        vat = _normalize_vat(profile.nif)
        remote = remote_by_vat.get(vat)
        if not remote:
            continue

        local_email = normalize_email_address(profile.user.email if profile.user_id else "")
        remote_email = normalize_email_address(_safe_get(remote, "email"))
        local_phone = normalize_phone_number(profile.phone)
        remote_phone = normalize_phone_number(_safe_get(remote, "phone", "mobile", "telephone"))
        local_name = normalize_client_name(profile.full_name)
        remote_name = normalize_client_name(_safe_get(remote, "name", "company_name", "company"))
        local_address = normalize_client_name(profile.address_line1)
        remote_address = normalize_client_name(_safe_get(remote, "address", "address_1", "address1"))
        local_postal = _normalize_postal_code(profile.postal_code)
        remote_postal = _normalize_postal_code(_safe_get(remote, "zip_code", "postal_code"))
        local_city = normalize_client_name(profile.city or profile.locality)
        remote_city = normalize_client_name(_safe_get(remote, "city", "locality"))

        diff_fields = []
        if local_name and remote_name and local_name != remote_name:
            diff_fields.append("nome")
        if local_phone and remote_phone and local_phone != remote_phone:
            diff_fields.append("telefone")
        if local_email and remote_email and local_email != remote_email:
            diff_fields.append("email")
        if local_address and remote_address and local_address != remote_address:
            diff_fields.append("morada")
        if local_postal and remote_postal and local_postal != remote_postal:
            diff_fields.append("código-postal")
        if local_city and remote_city and local_city != remote_city:
            diff_fields.append("cidade")

        if diff_fields:
            conflicts.append(
                {
                    "profile": profile,
                    "remote_customer_id": str(_safe_get(remote, "customer_id", "id", "customerId")),
                    "diff_fields": diff_fields,
                    "local": {
                        "name": profile.full_name,
                        "phone": profile.phone,
                        "email": profile.user.email if profile.user_id else "",
                        "address": profile.address_line1,
                        "postal_code": profile.postal_code,
                        "city": profile.city or profile.locality,
                    },
                    "remote": {
                        "name": _safe_get(remote, "name", "company_name", "company"),
                        "phone": _safe_get(remote, "phone", "mobile", "telephone"),
                        "email": _safe_get(remote, "email"),
                        "address": _safe_get(remote, "address", "address_1", "address1"),
                        "postal_code": _safe_get(remote, "zip_code", "postal_code"),
                        "city": _safe_get(remote, "city", "locality"),
                    },
                }
            )
            if len(conflicts) >= limit:
                break

    clients_without_nif = list(
        ClientProfile.objects.filter(nif="")
        .select_related("user")
        .order_by("-updated_at", "-id")[:limit]
    )

    duplicate_groups = []
    grouped = defaultdict(list)
    for profile in ClientProfile.objects.select_related("user").only("id", "full_name", "phone", "nif"):
        key = build_client_name_phone_key(profile.full_name, profile.phone)
        if key:
            grouped[key].append(profile)
    for profiles in grouped.values():
        if len(profiles) > 1:
            duplicate_groups.append(sorted(profiles, key=lambda p: (p.full_name.lower(), p.id)))
        if len(duplicate_groups) >= limit:
            break

    return {
        "remote_total": len(remote_customers),
        "conflicts": conflicts,
        "clients_without_nif": clients_without_nif,
        "duplicate_groups": duplicate_groups,
    }
