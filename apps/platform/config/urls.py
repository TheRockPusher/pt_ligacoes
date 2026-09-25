from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from ligacoes.core.import_api import import_detail, import_request
from ligacoes.public.views import healthz

urlpatterns = [
    path("healthz/", healthz, name="healthz"),
    path("ops/imports/", import_request, name="import_request"),
    path("ops/imports/<uuid:run_id>/", import_detail, name="import_detail"),
    path("", include("ligacoes.public.urls")),
]
if settings.ENABLE_ADMIN:
    urlpatterns.insert(0, path("admin/", admin.site.urls))
