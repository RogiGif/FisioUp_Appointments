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

from core.models import MoloniIntegration


BASE_URL = (getattr(settings, "MOLONI_BASE_URL", "https://api.moloni.pt/v1") or "").rstrip("/")


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


def customers_get_by_vat(vat: str) -> Dict[str, Any]:
    return moloni_request("customers/getByVat", {"vat": vat})


def customers_get_one(customer_id: str) -> Dict[str, Any]:
    return moloni_request("customers/getOne", {"customer_id": customer_id})


def customers_search(query: str) -> Dict[str, Any]:
    return moloni_request("customers/getBySearch", {"search": query})


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
