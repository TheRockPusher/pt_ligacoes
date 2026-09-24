from django.apps import AppConfig
from django.conf import settings


class PublicConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "ligacoes.public"
    verbose_name = "Consulta pública"

    def ready(self):
        if settings.REQUIRE_VITE_MANIFEST:
            from .assets import load_vite_assets

            load_vite_assets(str(settings.VITE_MANIFEST_PATH), str(settings.VITE_DIST_DIR))
