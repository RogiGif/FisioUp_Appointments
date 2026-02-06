import logging
import time
from functools import wraps
from typing import Iterable, Optional, Tuple

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse, HttpResponse
from django.contrib import messages

logger = logging.getLogger("core.ratelimit")


def _get_client_ip(request) -> str:
    if getattr(settings, "SECURE_PROXY_SSL_HEADER", None):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def is_json_request(request) -> bool:
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept


def _bucket(now: float, window: int) -> int:
    return int(now // window)


def _make_key(name: str, parts: Iterable[str], window: int) -> str:
    now = time.time()
    bucket = _bucket(now, window)
    safe_parts = ":".join([p for p in parts if p])
    return f"rl:{name}:{safe_parts}:{bucket}"


def check_rate_limit(
    request,
    *,
    name: str,
    limit: int,
    window: int,
    by_ip: bool = True,
    by_user: bool = False,
    by_value: Optional[str] = None,
) -> Tuple[bool, int]:
    parts = []
    if by_ip:
        parts.append(_get_client_ip(request))
    if by_user and getattr(request.user, "is_authenticated", False):
        parts.append(f"u{request.user.id}")
    if by_value:
        parts.append(f"v{by_value}")

    if not parts:
        return False, 0

    key = _make_key(name, parts, window)
    now = time.time()
    retry_after = window - int(now % window)

    try:
        if cache.add(key, 1, timeout=window):
            return False, retry_after
        count = cache.incr(key)
    except Exception:
        return False, retry_after

    if count > limit:
        logger.warning(
            "Rate limit blocked",
            extra={
                "ip": _get_client_ip(request),
                "path": request.path,
                "user_id": getattr(request.user, "id", None),
                "name": name,
                "count": count,
                "limit": limit,
            },
        )
        return True, retry_after

    return False, retry_after


def rate_limited_response(request, message: str, retry_after: int) -> HttpResponse:
    if is_json_request(request):
        return JsonResponse({"detail": message}, status=429)
    messages.error(request, message)
    response = HttpResponse(message, status=429)
    if retry_after:
        response["Retry-After"] = str(retry_after)
    return response


def rate_limit(
    *,
    name: str,
    limit: int,
    window: int,
    by_ip: bool = True,
    by_user: bool = False,
    by_value=None,
    methods=None,
    message: str = "Demasiadas tentativas. Tenta novamente em alguns minutos.",
):
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if methods and request.method not in methods:
                return view_func(request, *args, **kwargs)

            value = None
            if callable(by_value):
                value = by_value(request)
            elif isinstance(by_value, str):
                value = by_value

            blocked, retry_after = check_rate_limit(
                request,
                name=name,
                limit=limit,
                window=window,
                by_ip=by_ip,
                by_user=by_user,
                by_value=value,
            )
            if blocked:
                return rate_limited_response(request, message, retry_after)

            return view_func(request, *args, **kwargs)

        return _wrapped

    return decorator
