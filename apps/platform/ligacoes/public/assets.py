import json
from pathlib import Path, PurePosixPath

from django.core.exceptions import ImproperlyConfigured


def load_vite_assets(manifest_path, dist_dir):
    """Read only the trusted local build; reject remote or escaping asset paths."""
    path = Path(manifest_path)
    dist = Path(dist_dir).resolve()
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ImproperlyConfigured(
            "Vite build manifest is missing or invalid. Run pnpm build before starting production."
        ) from exc
    if not isinstance(manifest, dict) or "src/main.ts" not in manifest:
        raise ImproperlyConfigured("Vite manifest must contain the src/main.ts entry.")
    css = []
    visited = set()

    def asset_path(value, suffix):
        if not isinstance(value, str):
            raise ImproperlyConfigured("Invalid Vite asset path.")
        asset = PurePosixPath(value)
        if (
            asset.is_absolute()
            or any(part in {".", ".."} for part in asset.parts)
            or "\\" in value
            or ":" in value
            or "?" in value
            or "#" in value
            or asset.suffix != suffix
        ):
            raise ImproperlyConfigured("Vite assets must be local build-relative files.")
        resolved = (dist / value).resolve()
        if not resolved.is_relative_to(dist) or not resolved.is_file():
            raise ImproperlyConfigured(
                "A file referenced by the Vite manifest is missing or escapes the build directory."
            )
        return value

    def visit(key):
        if key in visited:
            return
        visited.add(key)
        chunk = manifest.get(key)
        if not isinstance(chunk, dict):
            raise ImproperlyConfigured("Invalid Vite manifest chunk.")
        asset_path(chunk.get("file"), ".js")
        imports = chunk.get("imports", [])
        stylesheets = chunk.get("css", [])
        if (
            not isinstance(imports, list)
            or not all(isinstance(item, str) for item in imports)
            or not isinstance(stylesheets, list)
        ):
            raise ImproperlyConfigured("Invalid Vite import or stylesheet list.")
        for imported in imports:
            visit(imported)
        for stylesheet in stylesheets:
            stylesheet = asset_path(stylesheet, ".css")
            if stylesheet not in css:
                css.append(stylesheet)

    visit("src/main.ts")
    return manifest["src/main.ts"]["file"], tuple(css)
