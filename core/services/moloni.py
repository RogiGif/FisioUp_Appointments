from __future__ import annotations

import json
import time
from datetime import timedelta
from typing import Any, Dict, Optional
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.utils import timezone

from core.models import ClientProfile, MoloniIntegration


BASE_URL = (getattr(settings, "MOLONI_BASE_URL", "https://api.moloni.pt/v1") or "").rstrip("/")

CUSTOMER_DEFAULT_FIELDS: dict[str, dict[str, Any]] = {
    "payment_method_id": {
        "label": "Método de pagamento",
        "required": True,
    },
    "document_type_id": {
        "label": "Tipo de documento",
        "required": True,
    },
    "language_id": {
        "label": "Idioma",
        "required": True,
    },
    "maturity_date_id": {
        "label": "Condição de vencimento",
        "required": True,
    },
    "country_id": {
        "label": "País",
        "required": True,
    },
    "delivery_method_id": {
        "label": "Método de envio",
        "required": False,
    },
}


class MoloniError(Exception):
    pass


def _get_settings() -> MoloniIntegration:
    return MoloniIntegration.get_solo()


def is_configured() -> bool:
    return bool(
        getattr(settings, "MOLONI_CLIENT_ID", "")
        and getattr(settings, "MOLONI_CLIENT_SECRET", "")
    )


def build_authorize_url(*, redirect_uri: str, state: str = "") -> str:
    if not is_configured():
        raise MoloniError("Configuração Moloni incompleta.")
    params = {
        "response_type": "code",
        "client_id": settings.MOLONI_CLIENT_ID,
        "redirect_uri": redirect_uri,
    }
    if state:
        params["state"] = state
    query = urlencode(params)
    return f"{BASE_URL}/authorize/?{query}"


def _grant_request(params: Dict[str, Any]) -> Dict[str, Any]:
    query = urlencode(params)
    try:
        with urlopen(f"{BASE_URL}/grant/?{query}", timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise MoloniError(f"Moloni grant falhou: {exc.code} {detail}") from exc
    except URLError as exc:
        raise MoloniError(f"Moloni grant erro: {exc}") from exc
    data = json.loads(raw)
    if isinstance(data, dict) and data.get("error"):
        raise MoloniError(f"Moloni API error: {data}")
    return data


def exchange_authorization_code(*, code: str, redirect_uri: str) -> Dict[str, Any]:
    if not is_configured():
        raise MoloniError("Configuração Moloni incompleta.")
    return _grant_request(
        {
            "grant_type": "authorization_code",
            "client_id": settings.MOLONI_CLIENT_ID,
            "client_secret": settings.MOLONI_CLIENT_SECRET,
            "redirect_uri": redirect_uri,
            "code": code,
        }
    )


def store_tokens(data: Dict[str, Any]) -> MoloniIntegration:
    integ = _get_settings()
    integ.access_token = data.get("access_token", "") or ""
    integ.refresh_token = data.get("refresh_token", integ.refresh_token) or ""
    expires_in = int(data.get("expires_in") or 3600)
    integ.expires_at = timezone.now() + timedelta(seconds=expires_in)
    integ.save(update_fields=["access_token", "refresh_token", "expires_at", "updated_at"])
    return integ


def get_company_id() -> str:
    integ = _get_settings()
    return (integ.company_id or getattr(settings, "MOLONI_COMPANY_ID", "") or "").strip()


def get_company_name() -> str:
    integ = _get_settings()
    return (integ.company_name or "").strip()


def store_company(company_id: str, company_name: str = "") -> MoloniIntegration:
    integ = _get_settings()
    integ.company_id = (company_id or "").strip()
    integ.company_name = (company_name or "").strip()
    integ.save(update_fields=["company_id", "company_name", "updated_at"])
    return integ


def refresh_access_token() -> str:
    integ = _get_settings()
    if not integ.refresh_token:
        raise MoloniError("Moloni: refresh_token em falta. Liga a integração.")
    data = _grant_request(
        {
            "grant_type": "refresh_token",
            "client_id": settings.MOLONI_CLIENT_ID,
            "client_secret": settings.MOLONI_CLIENT_SECRET,
            "refresh_token": integ.refresh_token,
        }
    )
    return store_tokens(data).access_token


def get_access_token() -> str:
    integ = _get_settings()
    if integ.access_token and integ.expires_at and integ.expires_at > timezone.now() + timedelta(minutes=1):
        return integ.access_token
    return refresh_access_token()


def disconnect() -> MoloniIntegration:
    integ = _get_settings()
    integ.access_token = ""
    integ.refresh_token = ""
    integ.company_id = ""
    integ.company_name = ""
    integ.expires_at = None
    integ.save(update_fields=["access_token", "refresh_token", "company_id", "company_name", "expires_at", "updated_at"])
    return integ


def moloni_request(endpoint: str, payload: Optional[Dict[str, Any]] = None, *, retries: int = 2, include_company: bool = True) -> Dict[str, Any]:
    token = get_access_token()
    endpoint = endpoint.lstrip("/").rstrip("/")
    url = f"{BASE_URL}/{endpoint}/"
    payload = payload or {}
    if include_company:
        company_id = get_company_id()
        if not company_id:
            raise MoloniError("Company ID Moloni em falta. Liga a integração ou define a empresa.")
        payload = {"company_id": company_id, **payload}

    for attempt in range(retries + 1):
        req = Request(
            f"{url}?{urlencode({'access_token': token})}",
            data=urlencode(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                status_code = getattr(resp, "status", 200)
        except HTTPError as exc:
            status_code = exc.code
            raw = exc.read().decode("utf-8", errors="ignore")
        except URLError as exc:
            if attempt >= retries:
                raise MoloniError(f"Moloni request erro: {exc}") from exc
            time.sleep(1.5 * (attempt + 1))
            continue

        if status_code == 401:
            if attempt >= retries:
                raise MoloniError("Moloni unauthorized.")
            token = refresh_access_token()
            time.sleep(0.5)
            continue

        if status_code != 200:
            if attempt >= retries:
                raise MoloniError(f"Moloni erro: {status_code} {raw}")
            time.sleep(1.5 * (attempt + 1))
            continue

        data = json.loads(raw)
        if isinstance(data, dict) and data.get("error"):
            raise MoloniError(f"Moloni API error: {data}")
        return data

    raise MoloniError("Moloni request falhou.")


def customers_get_all(*, qty: int = 50, offset: int = 0) -> Dict[str, Any]:
    return moloni_request("customers/getAll", {"qty": qty, "offset": offset})


def customers_get_modified_since(*, date: str, qty: int = 50, offset: int = 0) -> Dict[str, Any]:
    return moloni_request("customers/getModifiedSince", {"date": date, "qty": qty, "offset": offset})


def customers_get_by_vat(vat: str) -> Dict[str, Any]:
    return moloni_request("customers/getByVat", {"vat": vat})


def customers_get_one(customer_id: str) -> Dict[str, Any]:
    return moloni_request("customers/getOne", {"customer_id": customer_id})


def customers_search(query: str) -> Dict[str, Any]:
    return moloni_request("customers/getBySearch", {"search": query})


def customers_insert(payload: Dict[str, Any]) -> Dict[str, Any]:
    return moloni_request("customers/insert", payload)


def customers_update(payload: Dict[str, Any]) -> Dict[str, Any]:
    return moloni_request("customers/update", payload)


def customers_get_next_number() -> Dict[str, Any]:
    return moloni_request("customers/getNextNumber", {})


def companies_get_all() -> Dict[str, Any]:
    return moloni_request("companies/getAll", {}, include_company=False)


def list_companies() -> list[dict[str, str]]:
    data = companies_get_all()
    raw_companies = data.get("companies") if isinstance(data, dict) else data
    raw_companies = raw_companies or []
    companies: list[dict[str, str]] = []
    for company in raw_companies:
        company_id = str(company.get("company_id") or company.get("id") or "").strip()
        company_name = (company.get("name") or company.get("company_name") or "").strip()
        if company_id:
            companies.append(
                {
                    "company_id": company_id,
                    "company_name": company_name or f"Empresa {company_id}",
                }
            )
    return companies


def discover_company() -> Dict[str, str]:
    companies = list_companies()
    if len(companies) == 1:
        company = companies[0]
        company_id = company["company_id"]
        company_name = company["company_name"]
        store_company(company_id, company_name)
        return {"company_id": company_id, "company_name": company_name}

    configured_company_id = (getattr(settings, "MOLONI_COMPANY_ID", "") or "").strip()
    if configured_company_id:
        for company in companies:
            company_id = company["company_id"]
            if company_id == configured_company_id:
                company_name = company["company_name"]
                store_company(company_id, company_name)
                return {"company_id": company_id, "company_name": company_name}

    raise MoloniError("Não foi possível determinar a empresa Moloni automaticamente.")


def test_connection() -> Dict[str, Any]:
    company_id = get_company_id()
    if company_id:
        company = {
            "company_id": company_id,
            "company_name": get_company_name(),
        }
    else:
        try:
            company = discover_company()
        except MoloniError as exc:
            raise MoloniError("Escolhe primeiro a empresa Moloni no painel antes de testar a ligação.") from exc
    data = customers_get_all(qty=1, offset=0)
    customers = data.get("customers") if isinstance(data, dict) else data
    return {
        "ok": True,
        "company_id": company["company_id"],
        "company_name": company["company_name"],
        "customer_count_sample": len(customers or []),
    }


def _safe_get(data: Dict[str, Any], *keys: str, default=""):
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return default


def _normalize_vat(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _clean_postal_code(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    digits = _normalize_vat(value)
    if len(digits) == 7:
        return f"{digits[:4]}-{digits[4:]}"
    return value


def get_customer_defaults_status() -> Dict[str, Any]:
    integ = _get_settings()
    defaults = {
        "language_id": integ.customer_language_id,
        "maturity_date_id": integ.customer_maturity_date_id,
        "payment_method_id": integ.customer_payment_method_id,
        "delivery_method_id": integ.customer_delivery_method_id,
        "country_id": integ.customer_country_id,
        "document_type_id": integ.customer_document_type_id,
    }
    missing = [
        name for name in (
            "language_id",
            "maturity_date_id",
            "payment_method_id",
            "country_id",
            "document_type_id",
        ) if not defaults.get(name)
    ]
    return {
        "defaults": defaults,
        "missing": missing,
        "ready": not missing,
    }


def get_customer_defaults_suggestions(*, qty: int = 25) -> Dict[str, Any]:
    data = customers_get_all(qty=qty, offset=0)
    customers = data.get("customers") if isinstance(data, dict) else data
    customers = customers or []
    fields = {field_name: {} for field_name in CUSTOMER_DEFAULT_FIELDS}

    for customer in customers:
        customer = customer or {}
        customer_name = (
            _safe_get(customer, "name", "company_name", "company", default="Cliente sem nome")
            or "Cliente sem nome"
        )
        copies = customer.get("copies") or []
        document_type_id = _safe_get(customer, "document_type_id")
        if isinstance(copies, list) and copies:
            first_copy = copies[0] or {}
            document_type_id = _safe_get(first_copy, "document_type_id") or document_type_id

        values = {
            "payment_method_id": _safe_get(customer, "payment_method_id"),
            "document_type_id": document_type_id,
            "language_id": _safe_get(customer, "language_id"),
            "maturity_date_id": _safe_get(customer, "maturity_date_id"),
            "country_id": _safe_get(customer, "country_id"),
            "delivery_method_id": _safe_get(customer, "delivery_method_id"),
        }
        for field_name, value in values.items():
            if value in (None, ""):
                continue
            value_key = str(value).strip()
            if not value_key:
                continue
            bucket = fields[field_name].setdefault(value_key, {"count": 0, "sample_names": []})
            bucket["count"] += 1
            if customer_name not in bucket["sample_names"] and len(bucket["sample_names"]) < 3:
                bucket["sample_names"].append(customer_name)

    field_rows = []
    recommended_defaults: dict[str, str] = {}
    for field_name, values in fields.items():
        options = []
        sorted_values = sorted(
            values.items(),
            key=lambda item: (-item[1]["count"], item[0]),
        )
        for value_key, meta in sorted_values:
            options.append({
                "value": value_key,
                "sample_names": meta["sample_names"],
                "count": meta["count"],
            })
        recommended_value = options[0]["value"] if options else ""
        if recommended_value:
            recommended_defaults[field_name] = recommended_value
        field_rows.append({
            "field": field_name,
            "label": CUSTOMER_DEFAULT_FIELDS[field_name]["label"],
            "required": CUSTOMER_DEFAULT_FIELDS[field_name]["required"],
            "options": options,
            "recommended_value": recommended_value,
        })

    return {
        "customer_count": len(customers),
        "fields": field_rows,
        "recommended_defaults": recommended_defaults,
    }


def get_recommended_customer_defaults(*, qty: int = 25) -> Dict[str, Any]:
    suggestions = get_customer_defaults_suggestions(qty=qty)
    defaults: dict[str, int] = {}
    missing: list[str] = []

    for row in suggestions["fields"]:
        recommended_value = row.get("recommended_value")
        if recommended_value not in (None, ""):
            defaults[row["field"]] = int(recommended_value)
        elif row.get("required"):
            missing.append(row["field"])

    return {
        "customer_count": suggestions["customer_count"],
        "defaults": defaults,
        "missing": missing,
    }


def _resolve_customer_defaults() -> Dict[str, Any]:
    status = get_customer_defaults_status()
    if status["missing"]:
        raise MoloniError(
            "A configuração de defaults de clientes da Moloni está incompleta na app: "
            + ", ".join(status["missing"])
            + "."
        )

    defaults = dict(status["defaults"])
    document_type_id = int(defaults["document_type_id"])
    defaults["copies"] = [{"document_type_id": document_type_id, "copies": 1}]
    return defaults


def _next_customer_number() -> str:
    data = customers_get_next_number()
    candidates = []
    if isinstance(data, dict):
        candidates = [
            data.get("number"),
            data.get("next_number"),
            data.get("customer_number"),
        ]
    elif isinstance(data, list) and data:
        first = data[0] or {}
        candidates = [
            first.get("number"),
            first.get("next_number"),
            first.get("customer_number"),
        ]
    for value in candidates:
        if value not in (None, ""):
            return str(value).strip()
    raise MoloniError("Moloni não devolveu o próximo número de cliente.")


def _get_existing_remote_customer(profile: ClientProfile) -> tuple[Dict[str, Any], str]:
    if profile.moloni_customer_id:
        data = customers_get_one(profile.moloni_customer_id)
        customer = data.get("customer") if isinstance(data, dict) else data
        if isinstance(customer, list):
            customer = customer[0] if customer else {}
        if customer:
            return customer, "id"

    vat = _normalize_vat(profile.nif)
    if vat:
        data = customers_get_by_vat(vat)
        if isinstance(data, dict):
            customers = data.get("customers") or data.get("customer") or []
        else:
            customers = data
        if isinstance(customers, dict):
            customers = [customers]
        customers = customers or []
        if customers:
            return customers[0], "vat"

    return {}, ""


def _build_customer_payload(profile: ClientProfile, existing_customer: Dict[str, Any] | None = None) -> Dict[str, Any]:
    existing_customer = existing_customer or {}
    defaults = _resolve_customer_defaults()
    email = ""
    if profile.user_id and getattr(profile.user, "email", ""):
        email = profile.user.email.strip().lower()

    payload: Dict[str, Any] = {
        "number": str(_safe_get(existing_customer, "number") or _next_customer_number()),
        "vat": _normalize_vat(profile.nif),
        "name": (profile.full_name or _safe_get(existing_customer, "name") or "Cliente Fisio-UP").strip(),
        "address": (profile.address_line1 or _safe_get(existing_customer, "address", "address_1", "address1") or "Por definir").strip(),
        "zip_code": _clean_postal_code(profile.postal_code or _safe_get(existing_customer, "zip_code", "postal_code")),
        "city": (profile.city or profile.locality or profile.county or _safe_get(existing_customer, "city", "locality") or "Por definir").strip(),
        "country_id": int(_safe_get(existing_customer, "country_id") or defaults["country_id"]),
        "language_id": int(_safe_get(existing_customer, "language_id") or defaults["language_id"]),
        "maturity_date_id": int(_safe_get(existing_customer, "maturity_date_id") or defaults["maturity_date_id"]),
        "payment_method_id": int(_safe_get(existing_customer, "payment_method_id") or defaults["payment_method_id"]),
        "document_type_id": int(_safe_get(existing_customer, "document_type_id") or defaults["document_type_id"]),
        "copies": existing_customer.get("copies") or defaults["copies"],
        "phone": (profile.phone or _safe_get(existing_customer, "phone", "mobile", "telephone") or "").strip(),
        "email": email or _safe_get(existing_customer, "email"),
        "contact_name": (profile.full_name or _safe_get(existing_customer, "contact_name") or "").strip(),
    }

    delivery_method = _safe_get(existing_customer, "delivery_method_id") or defaults.get("delivery_method_id")
    if delivery_method not in (None, ""):
        payload["delivery_method_id"] = int(delivery_method)

    if existing_customer.get("customer_id") or existing_customer.get("id"):
        payload["customer_id"] = int(_safe_get(existing_customer, "customer_id", "id"))

    return payload


def sync_client_profile(profile: ClientProfile) -> Dict[str, Any]:
    if not is_configured():
        raise MoloniError("Configuração Moloni incompleta.")
    integ = _get_settings()
    if not integ.refresh_token:
        raise MoloniError("Integração Moloni não ligada.")

    vat = _normalize_vat(profile.nif)
    if not vat:
        raise MoloniError("O cliente não tem NIF; não pode ser sincronizado automaticamente com a Moloni.")

    existing_customer, matched_by = _get_existing_remote_customer(profile)
    payload = _build_customer_payload(profile, existing_customer)

    if payload.get("customer_id"):
        response = customers_update(payload)
        action = "updated"
    else:
        response = customers_insert(payload)
        action = "created"

    customer = response.get("customer") if isinstance(response, dict) else response
    if isinstance(response, dict) and not customer:
        customer = response.get("customer_id") and {"customer_id": response.get("customer_id")}
    if isinstance(customer, list):
        customer = customer[0] if customer else {}
    customer = customer or {}
    customer_id = str(_safe_get(customer, "customer_id", "id") or payload.get("customer_id") or "").strip()
    if not customer_id:
        raise MoloniError("A Moloni não devolveu o customer_id do cliente sincronizado.")

    if profile.moloni_customer_id != customer_id:
        profile.moloni_customer_id = customer_id
        profile.save(update_fields=["moloni_customer_id", "updated_at"])

    integ.last_sync_at = timezone.now()
    integ.save(update_fields=["last_sync_at", "updated_at"])

    return {
        "action": action,
        "customer_id": customer_id,
        "matched_by": matched_by or ("vat" if action == "updated" else ""),
        "customer_name": payload["name"],
    }
