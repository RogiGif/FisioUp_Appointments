from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth import logout
from django.http import JsonResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.deprecation import MiddlewareMixin

from core.services.audit import log_audit_event
from core.session_timeout import get_session_timeout_config


class SessionTimeoutMiddleware(MiddlewareMixin):
    EXEMPT_PATH_PREFIXES = (
        "/login/",
        "/logout/",
        "/password/reset/",
        "/health/",
    )

    def _is_exempt(self, request):
        path = request.path or ""
        return any(path.startswith(prefix) for prefix in self.EXEMPT_PATH_PREFIXES)

    def _expired_response(self, request):
        login_url = f"/login/?{urlencode({'next': request.get_full_path()})}"
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse(
                {
                    "ok": False,
                    "expired": True,
                    "login_url": login_url,
                },
                status=401,
            )
        messages.warning(
            request,
            "Sessão expirada por inatividade.",
            fail_silently=True,
        )
        return redirect(login_url)

    def process_request(self, request):
        if self._is_exempt(request) or not request.user.is_authenticated:
            return None

        config = get_session_timeout_config(request.user)
        timeout_seconds = int(config.get("timeout_seconds") or 0)
        if timeout_seconds <= 0:
            return None

        now_ts = int(timezone.now().timestamp())
        last_activity_ts = int(request.session.get("_last_activity_ts") or 0)
        if last_activity_ts and (now_ts - last_activity_ts) > timeout_seconds:
            log_audit_event(
                category="auth",
                action="session_timeout",
                request=request,
                actor=request.user,
                source="session_timeout",
                message="Sessão expirada por inatividade.",
                metadata={"timeout_seconds": timeout_seconds},
            )
            logout(request)
            return self._expired_response(request)
        return None

    def process_response(self, request, response):
        if (
            not self._is_exempt(request)
            and getattr(request, "user", None) is not None
            and request.user.is_authenticated
        ):
            request.session["_last_activity_ts"] = int(timezone.now().timestamp())
        return response
