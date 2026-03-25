from django.urls import path
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from functools import wraps

from . import views
from core.permissions import is_admin_role

app_name = "website"

def admin_only(view_func):
    @login_required(login_url="/login/")
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not is_admin_role(request.user):
            raise PermissionDenied
        return view_func(request, *args, **kwargs)

    return _wrapped

urlpatterns = [
    path("", admin_only(views.home), name="home"),
    path("sobre/", admin_only(views.about), name="about"),
    path("equipa/", admin_only(views.team), name="team"),
    path("servicos/", admin_only(views.services), name="services"),
    path("parcerias/", admin_only(views.partners), name="partners"),
    path("destaques/", admin_only(views.highlights), name="highlights"),
    path("destaques/<slug:slug>/", admin_only(views.highlight_detail), name="highlight_detail"),
    path("contactos/", admin_only(views.contacts), name="contacts"),
    path("marque-ja/", admin_only(views.book_now_redirect), name="book_now"),
]
