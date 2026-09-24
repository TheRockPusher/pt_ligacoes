from django import template
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.templatetags.static import static
from django.utils.html import format_html, format_html_join

from ligacoes.public.assets import load_vite_assets

register = template.Library()


@register.simple_tag
def vite_assets():
    try:
        entry, stylesheets = load_vite_assets(
            str(settings.VITE_MANIFEST_PATH), str(settings.VITE_DIST_DIR)
        )
    except ImproperlyConfigured:
        if settings.REQUIRE_VITE_MANIFEST:
            raise
        return ""
    tags = [format_html('<link rel="stylesheet" href="{}">', static(path)) for path in stylesheets]
    tags.append(format_html('<script type="module" src="{}"></script>', static(entry)))
    return format_html_join("\n", "{}", ((tag,) for tag in tags))
