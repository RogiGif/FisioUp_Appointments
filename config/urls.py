from django.contrib import admin
from django.urls import path, include
from django.conf import settings              
from django.conf.urls.static import static
from django.views.defaults import permission_denied

admin.site.site_header = "Marcação Fisio — Administração"
admin.site.site_title = "Admin"
admin.site.index_title = "Painel de gestão"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("core.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

handler403 = permission_denied
