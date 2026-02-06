from functools import wraps
from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied

from .models import Professional
from .permissions import can_access_backoffice


def professional_required(view_func):
    """
    Produção:
    - não autenticado -> redirect para LOGIN_URL com next=
    - autenticado mas não é staff e não tem Professional -> 403
    """
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            login_url = getattr(settings, "LOGIN_URL", "/login/")
            return redirect_to_login(request.get_full_path(), login_url=login_url)

        is_professional = request.user.is_staff or Professional.objects.filter(user=request.user).exists()

        if not is_professional:
            raise PermissionDenied("Acesso reservado a profissionais.")

        return view_func(request, *args, **kwargs)

    return _wrapped


def backoffice_required(view_func):
    """
    Produção:
    - não autenticado -> redirect para LOGIN_URL com next=
    - autenticado mas sem permissão -> 403
    """
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            login_url = getattr(settings, "LOGIN_URL", "/login/")
            return redirect_to_login(request.get_full_path(), login_url=login_url)

        if not can_access_backoffice(request.user):
            raise PermissionDenied("Acesso reservado ao backoffice.")

        return view_func(request, *args, **kwargs)

    return _wrapped
