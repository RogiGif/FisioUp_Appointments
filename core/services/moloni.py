from __future__ import annotations

import json
import time
from datetime import timedelta
from typing import Any, Dict, Optional

import requests
from django.conf import settings
from django.utils import timezone

from core.models import MoloniIntegration


BASE_URL = getattr(settings, "MOLONI_BASE_URL", "https://api.moloni.pt")
COMPANY_ID = getattr(settings, "MOLONI_COMPANY_ID", None)


class MoloniError(Exception):
    pass


def _get_settings() -> MoloniIntegration:
    return MoloniIntegration.get_solo()


def get_access_token() -> str:
    integ = _get_settings()
    if integ.access_token and integ.expires_at and integ.expires_at > timezone.now() + timedelta(minutes=1):
        return integ.access_token

    if not integ.refresh_token:
        raise MoloniError("Moloni: refresh_token em falta. Liga a integração.")

    payload = {
        "grant_type": "refresh_token",
        "client_id": settings.MOLONI_CLIENT_ID,
        "client_secret": settings.MOLONI_CLIENT_SECRET,
        "refresh_token": integ.refresh_token,
    }
    resp = requests.post(f"{BASE_URL}/oauth/grant", data=payload, timeout=20)
    if resp.status_code != 200:
        raise MoloniError(f"Moloni token refresh falhou: {resp.status_code} {resp.text}")

    data = resp.json()
    integ.access_token = data.get("access_token", "")
    integ.refresh_token = data.get("refresh_token", integ.refresh_token)
    expires_in = int(data.get("expires_in") or 3600)
    integ.expires_at = timezone.now() + timedelta(seconds=expires_in)
    integ.save(update_fields=["access_token", "refresh_token", "expires_at", "updated_at"])
    return integ.access_token


def moloni_request(endpoint: str, payload: Dict[str, Any], *, retries: int = 2) -> Dict[str, Any]:
    token = get_access_token()
    url = f"{BASE_URL}/{endpoint.lstrip('/')}"
    headers = {"Content-Type": "application/json"}
    body = {"access_token": token, **payload}
    if COMPANY_ID and "company_id" not in body:
        body["company_id"] = COMPANY_ID

    for attempt in range(retries + 1):
        try:
            resp = requests.post(url, data=json.dumps(body), headers=headers, timeout=30)
        except requests.RequestException as exc:
            if attempt >= retries:
                raise MoloniError(f"Moloni request erro: {exc}")
            time.sleep(1.5 * (attempt + 1))
            continue

        if resp.status_code == 401:
            if attempt >= retries:
                raise MoloniError("Moloni unauthorized.")
            # força refresh e tenta de novo
            _ = get_access_token()
            time.sleep(0.5)
            continue

        if resp.status_code != 200:
            if attempt >= retries:
                raise MoloniError(f"Moloni erro: {resp.status_code} {resp.text}")
            time.sleep(1.5 * (attempt + 1))
            continue

        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            raise MoloniError(f"Moloni API error: {data}")
        return data

    raise MoloniError("Moloni request falhou.")


def customers_get_all(page: int = 1) -> Dict[str, Any]:
    return moloni_request("customers/getAll", {"page": page})


def customers_get_by_vat(vat: str) -> Dict[str, Any]:
    return moloni_request("customers/getByVat", {"vat": vat})


def customers_get_one(customer_id: str) -> Dict[str, Any]:
    return moloni_request("customers/getOne", {"customer_id": customer_id})


def customers_search(query: str) -> Dict[str, Any]:
    return moloni_request("customers/getBySearch", {"search": query})
