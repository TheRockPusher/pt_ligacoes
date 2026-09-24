from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from ligacoes.public.views import healthz

urlpatterns = [
    path("healthz/", healthz, name="healthz"),
    path("", include("ligacoes.public.urls")),
]
if settings.ENABLE_ADMIN:
    urlpatterns.insert(0, path("admin/", admin.site.urls))
