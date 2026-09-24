import json
from pathlib import Path

from django.template import Context, Template


def test_rebuilt_assets_replace_deleted_bundle_in_rendered_page(settings, tmp_path: Path):
    """The watch build may remove the previous bundle without restarting Django."""
    assets = tmp_path / "assets"
    assets.mkdir()
    manifest = tmp_path / "manifest.json"
    settings.VITE_MANIFEST_PATH = manifest
    settings.VITE_DIST_DIR = tmp_path
    settings.REQUIRE_VITE_MANIFEST = True
    template = Template("{% load vite %}{% vite_assets %}")

    first_bundle = assets / "main-first.js"
    first_bundle.write_text("export {};", encoding="utf-8")
    manifest.write_text(json.dumps({"src/main.ts": {"file": "assets/main-first.js"}}))
    first_page = template.render(Context())
    assert 'src="/static/assets/main-first.js"' in first_page

    first_bundle.unlink()
    (assets / "main-next.js").write_text("export {};", encoding="utf-8")
    manifest.write_text(json.dumps({"src/main.ts": {"file": "assets/main-next.js"}}))
    next_page = template.render(Context())
    assert 'src="/static/assets/main-next.js"' in next_page
    assert "main-first.js" not in next_page
