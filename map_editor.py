#!/usr/bin/env python3
"""Local editor and Cloudflare R2 publisher for the Cape Cup map.

The editor binds to localhost, opens in the default browser, and publishes both
the view-only and editable SVG versions directly to Cloudflare R2 with boto3.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import tempfile
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET


APP_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE = APP_DIR / "Cape Cup Map - Editor Source.svg"
LOCAL_ENV_PATH = APP_DIR / ".env"
MAX_REQUEST_BYTES = 2_000_000
PUBLISHED_MAP_OBJECT_KEY = "map.svg"
EDITABLE_MAP_OBJECT_KEY = "editable-map.svg"
PUBLISHED_MAP_CACHE_CONTROL = "no-cache, max-age=0, must-revalidate"
R2_ENVIRONMENT_VARIABLES = (
    "R2_ENDPOINT_URL",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET_NAME",
    "R2_PUBLIC_MAP_URL",
)
_R2_UPLOAD_LOCK = threading.Lock()

COLORS = [
    ("Red", "#e41a1c"),
    ("Orange", "#ff8c00"),
    ("Yellow", "#ffd92f"),
    ("Green", "#4daf4a"),
    ("Light Blue", "#7fcef3"),
    ("Light Purple", "#c7a6e8"),
    ("Pink", "#f781bf"),
    ("White", "#ffffff"),
]
ALLOWED_COLORS = {value.lower(): value for _, value in COLORS}


class PublishError(RuntimeError):
    """A recoverable map publishing failure that should be shown in the editor."""


class PublishInProgressError(PublishError):
    """Raised when another publish is already writing or uploading the map."""


def current_r2_settings() -> dict[str, str]:
    return {name: os.environ.get(name, "").strip() for name in R2_ENVIRONMENT_VARIABLES}


def validate_r2_settings(settings: dict[str, str]) -> dict[str, str]:
    settings = {name: str(settings.get(name, "")).strip() for name in R2_ENVIRONMENT_VARIABLES}
    missing = [name for name, value in settings.items() if not value]
    if missing:
        raise PublishError(
            "R2 publishing is not configured. Missing environment variable(s): "
            + ", ".join(missing)
        )
    if not settings["R2_PUBLIC_MAP_URL"].lower().startswith(("https://", "http://")):
        raise PublishError("R2_PUBLIC_MAP_URL must be a complete http:// or https:// URL to map.svg.")
    if not urlparse(settings["R2_PUBLIC_MAP_URL"]).path.endswith("/map.svg"):
        raise PublishError("R2_PUBLIC_MAP_URL must end with /map.svg.")
    if not settings["R2_ENDPOINT_URL"].lower().startswith(("https://", "http://")):
        raise PublishError("R2_ENDPOINT_URL must be a complete Cloudflare S3 endpoint URL.")
    return settings


def r2_settings() -> dict[str, str]:
    return validate_r2_settings(current_r2_settings())


def atomic_write_text(path: Path, content: str) -> None:
    """Replace a text file only after its complete contents are on disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def load_local_r2_settings(path: Path = LOCAL_ENV_PATH) -> dict[str, str]:
    """Load the editor's local .env values into this process."""
    if not path.is_file():
        return current_r2_settings()

    allowed = set(R2_ENVIRONMENT_VARIABLES)
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, raw_value = line.split("=", 1)
        name = name.strip()
        if name not in allowed:
            continue
        raw_value = raw_value.strip()
        try:
            value = json.loads(raw_value) if raw_value.startswith(('"', "'")) else raw_value
        except json.JSONDecodeError:
            value = raw_value.strip('"\'')
        os.environ[name] = str(value).strip()
    return current_r2_settings()


def save_local_r2_settings(settings: dict[str, str], path: Path = LOCAL_ENV_PATH) -> None:
    """Atomically save R2 settings for the local editor and use them immediately."""
    settings = validate_r2_settings(settings)
    content = "\n".join(f"{name}={json.dumps(settings[name])}" for name in R2_ENVIRONMENT_VARIABLES) + "\n"
    atomic_write_text(path, content)
    for name, value in settings.items():
        os.environ[name] = value


def _run_r2_operation(action: str, operation):
    """Run one non-overlapping R2 operation and translate failures for the UI."""
    settings = r2_settings()

    if not _R2_UPLOAD_LOCK.acquire(blocking=False):
        raise PublishInProgressError("Another R2 transfer is in progress. Please wait for it to finish.")
    try:
        try:
            import boto3
            from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError
        except ImportError as exc:
            raise PublishError(
                "R2 publishing requires boto3. Install it with: python -m pip install -r requirements.txt"
            ) from exc

        try:
            client = boto3.client(
                "s3",
                endpoint_url=settings["R2_ENDPOINT_URL"],
                aws_access_key_id=settings["R2_ACCESS_KEY_ID"],
                aws_secret_access_key=settings["R2_SECRET_ACCESS_KEY"],
                region_name="auto",
            )
            return operation(client, settings)
        except EndpointConnectionError as exc:
            raise PublishError(
                f"Could not reach Cloudflare R2 while {action}. Check the internet connection "
                "and R2_ENDPOINT_URL."
            ) from exc
        except ClientError as exc:
            error = exc.response.get("Error", {})
            code = error.get("Code", "unknown error")
            raise PublishError(
                f"Cloudflare R2 rejected the request ({code}). Check the access key, secret, bucket name, "
                "and bucket-scoped token permissions."
            ) from exc
        except BotoCoreError as exc:
            raise PublishError(f"Cloudflare R2 {action} failed ({type(exc).__name__}).") from exc
        except ValueError as exc:
            raise PublishError(f"R2 client configuration is invalid: {exc}.") from exc
        except PublishError:
            raise
        except Exception as exc:
            raise PublishError(
                f"Cloudflare R2 {action} failed ({type(exc).__name__}). Check credentials, bucket "
                "permissions, and internet access."
            ) from exc
    finally:
        _R2_UPLOAD_LOCK.release()


def _upload_svg_objects(objects: dict[str, str]) -> str:
    for object_key, svg in objects.items():
        if not isinstance(svg, str) or not svg.strip():
            raise PublishError(f"{object_key} is empty and was not uploaded.")

    def upload(client, settings: dict[str, str]) -> str:
        # Upload the editable copy first. Publishing map.svg last means the
        # website changes only after the matching editable version is stored.
        for object_key, svg in objects.items():
            client.put_object(
                Bucket=settings["R2_BUCKET_NAME"],
                Key=object_key,
                Body=svg.encode("utf-8"),
                ContentType="image/svg+xml",
                CacheControl=PUBLISHED_MAP_CACHE_CONTROL,
            )
        return settings["R2_PUBLIC_MAP_URL"]

    return _run_r2_operation("uploading the map", upload)


def upload_map_versions(published_svg: str, editable: str) -> str:
    """Upload both SVG versions directly from memory, with map.svg last."""
    return _upload_svg_objects(
        {
            EDITABLE_MAP_OBJECT_KEY: editable,
            PUBLISHED_MAP_OBJECT_KEY: published_svg,
        }
    )


def upload_published_map(svg_path: str | Path) -> str:
    """Backward-compatible helper for uploading only an existing map.svg."""
    path = Path(svg_path).resolve()
    if not path.is_file():
        raise PublishError(f"Published map was not found: {path}")
    return _upload_svg_objects({PUBLISHED_MAP_OBJECT_KEY: path.read_text(encoding="utf-8")})


def download_editable_map() -> str:
    """Load editable-map.svg from R2 without creating a local copy."""
    def download(client, settings: dict[str, str]) -> str:
        response = client.get_object(
            Bucket=settings["R2_BUCKET_NAME"],
            Key=EDITABLE_MAP_OBJECT_KEY,
        )
        body = response["Body"]
        try:
            content = body.read(MAX_REQUEST_BYTES + 1)
        finally:
            body.close()
        if len(content) > MAX_REQUEST_BYTES:
            raise PublishError("The cloud editable-map.svg is too large for the editor.")
        return content.decode("utf-8")

    return _run_r2_operation("loading editable-map.svg", download)


def strip_embedded_scripts(svg: str) -> str:
    """Remove executable SVG scripts while keeping the artwork and styles."""
    return re.sub(r"\s*<script\b[^>]*>.*?</script>", "", svg, flags=re.I | re.S)


def editor_svg(svg: str) -> str:
    """Prepare the map for the editor's page-level controller."""
    return strip_embedded_scripts(svg)


def region_names(svg: str) -> list[str]:
    return re.findall(r'<path\b[^>]*\bdata-region="([^"]+)"', svg)


def validate_editor_svg(svg: str) -> None:
    """Accept SVGs produced by this editor and reject incomplete map files."""
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as exc:
        raise ValueError(f"The selected file is not valid SVG: {exc}") from exc
    if not root.tag.endswith("svg"):
        raise ValueError("The selected file is not an SVG map.")
    names = region_names(svg)
    ids = re.findall(
        r'<path\b(?=[^>]*\bdata-region="[^"]+")(?=[^>]*\bid="([^"]+)")[^>]*/>',
        svg,
    )
    inputs = re.findall(r'<input\b[^>]*\bid="[^"]+-input"', svg)
    if len(names) != 43 or len(set(names)) != 43:
        raise ValueError("This map must contain the 43 uniquely named Cape Cup regions.")
    if len(ids) != 43 or len(set(ids)) != 43:
        raise ValueError("This map is missing unique region IDs.")
    if len(inputs) != 43:
        raise ValueError("Import an editable SVG exported by this editor, not a published map.svg.")


def update_editable_paths(svg: str, regions: dict[str, dict[str, object]]) -> str:
    path_pattern = re.compile(
        r'<path\b(?=[^>]*\bid="([^"]+)")(?=[^>]*\bdata-region="([^"]+)")[^>]*/>'
    )

    def replace(match: re.Match[str]) -> str:
        element = match.group(0)
        region_id = match.group(1)
        state = regions.get(region_id, {})
        requested = str(state.get("fill", "#ffffff")).lower()
        fill = ALLOWED_COLORS.get(requested, "#ffffff")
        try:
            color_index = int(state.get("colorIndex", 7))
        except (TypeError, ValueError):
            color_index = 7
        if not 0 <= color_index < len(COLORS):
            color_index = 7
        if 'class="published-region"' in element:
            element = element.replace('class="published-region"', 'class="clickable-region"')
        elif 'class="clickable-region"' not in element:
            element = element[:-2] + ' class="clickable-region"/>'
        if re.search(r'\sdata-color-index="[^"]*"', element):
            element = re.sub(
                r'\sdata-color-index="[^"]*"',
                f' data-color-index="{color_index}"',
                element,
                count=1,
            )
        else:
            element = element[:-2] + f' data-color-index="{color_index}"/>'
        if re.search(r'\sfill="[^"]*"', element):
            element = re.sub(r'\sfill="[^"]*"', f' fill="{fill}"', element, count=1)
        else:
            element = element[:-2] + f' fill="{fill}"/>'
        return element

    return path_pattern.sub(replace, svg)


def update_input_values(svg: str, notes: dict[str, str]) -> str:
    input_pattern = re.compile(r'<input\b(?=[^>]*\bid="([^"]+)-input")[^>]*/>')

    def replace(match: re.Match[str]) -> str:
        element = match.group(0)
        region_id = match.group(1)
        value = str(notes.get(region_id, ""))[:80]
        element = re.sub(r'\svalue="[^"]*"', "", element)
        return element[:-2] + f' value="{html.escape(value, quote=True)}" />'

    return input_pattern.sub(replace, svg)


def editable_svg(source_svg: str, payload: dict[str, object]) -> str:
    raw_regions = payload.get("regions", {})
    raw_notes = payload.get("notes", {})
    regions = raw_regions if isinstance(raw_regions, dict) else {}
    notes = raw_notes if isinstance(raw_notes, dict) else {}
    svg = update_editable_paths(source_svg, regions)
    svg = update_input_values(svg, notes)
    validate_editor_svg(svg)
    return svg


def update_region_paths(svg: str, regions: dict[str, dict[str, object]]) -> str:
    path_pattern = re.compile(
        r'<path\b(?=[^>]*\bid="([^"]+)")(?=[^>]*\bdata-region="([^"]+)")[^>]*/>'
    )

    def replace(match: re.Match[str]) -> str:
        element = match.group(0)
        region_id = match.group(1)
        state = regions.get(region_id, {})
        requested = str(state.get("fill", "#ffffff")).lower()
        fill = ALLOWED_COLORS.get(requested, "#ffffff")
        element = re.sub(r'\s+class="clickable-region"', ' class="published-region"', element)
        element = re.sub(r'\s+data-color-index="[^"]*"', "", element)
        element = re.sub(r'\s+tabindex="[^"]*"', "", element)
        element = re.sub(r'\s+role="[^"]*"', "", element)
        if re.search(r'\sfill="[^"]*"', element):
            element = re.sub(r'\sfill="[^"]*"', f' fill="{fill}"', element, count=1)
        else:
            element = element[:-2] + f' fill="{fill}"/>'
        return element

    return path_pattern.sub(replace, svg)


def replace_inputs_with_static_notes(svg: str, notes: dict[str, str]) -> str:
    """Replace HTML inputs with SVG-only, non-editable note boxes."""
    overlay_pattern = re.compile(
        r'\s*<g id="region-input-overlay"\s+transform="([^"]+)">(.*?)</g>',
        flags=re.S,
    )
    overlay_match = overlay_pattern.search(svg)
    if not overlay_match:
        raise ValueError("The source SVG is missing the region input overlay.")

    transform = overlay_match.group(1)
    body = overlay_match.group(2)
    box_pattern = re.compile(
        r'<foreignObject\b[^>]*\bid="([^"]+)-input-box"[^>]*'
        r'\bx="([^"]+)"\s+y="([^"]+)"\s+width="([^"]+)"\s+height="([^"]+)"[^>]*>'
        r'.*?</foreignObject>',
        flags=re.S,
    )
    rendered: list[str] = []
    for match in box_pattern.finditer(body):
        region_id, x_raw, y_raw, width_raw, height_raw = match.groups()
        value = str(notes.get(region_id, "")).strip()
        if not value:
            continue
        # Limit display length so a visitor cannot receive malformed or unusable markup.
        value = value[:80]
        x, y = float(x_raw), float(y_raw)
        width, height = float(width_raw), float(height_raw)
        center_x = x + width / 2
        center_y = y + height / 2
        safe_id = html.escape(region_id, quote=True)
        safe_text = html.escape(value)
        rendered.append(
            f'''  <g id="{safe_id}-published-note" class="published-note" pointer-events="none">
   <rect x="{x:.2f}" y="{y:.2f}" width="{width:.2f}" height="{height:.2f}" rx="2" ry="2"/>
   <text x="{center_x:.2f}" y="{center_y:.2f}">{safe_text}</text>
  </g>'''
        )

    replacement = ""
    if rendered:
        replacement = (
            f'\n <g id="published-note-overlay" transform="{html.escape(transform, quote=True)}">\n'
            + "\n".join(rendered)
            + "\n </g>"
        )
    return overlay_pattern.sub(replacement, svg, count=1)


def add_published_styles(svg: str) -> str:
    style = """
 <style type="text/css"><![CDATA[
  .published-region { cursor: default; }
  .published-note rect { fill: rgba(255,255,255,.96); stroke: #222; stroke-width: 1.5; }
  .published-note text {
    fill: #111; font: 700 30px Arial, sans-serif; text-anchor: middle;
    dominant-baseline: middle; paint-order: stroke fill; stroke: #fff; stroke-width: 2px;
  }
 ]]></style>
"""
    return svg.replace("</svg>", style + "</svg>", 1)


def static_svg(source_svg: str, payload: dict[str, object]) -> str:
    raw_regions = payload.get("regions", {})
    raw_notes = payload.get("notes", {})
    regions = raw_regions if isinstance(raw_regions, dict) else {}
    notes = raw_notes if isinstance(raw_notes, dict) else {}

    svg = strip_embedded_scripts(source_svg)
    # Remove editor-only styles before adding the small published stylesheet.
    svg = re.sub(
        r'\s*<style\b[^>]*><!\[CDATA\[.*?\.clickable-region.*?\]\]></style>',
        "",
        svg,
        flags=re.I | re.S,
    )
    svg = update_region_paths(svg, regions)
    svg = replace_inputs_with_static_notes(svg, notes)
    return add_published_styles(svg)


def github_index(public_map_url: str | None = None) -> str:
    """Responsive GitHub Pages shell that displays and refreshes the R2 SVG."""
    map_url = public_map_url or os.environ.get("R2_PUBLIC_MAP_URL", "").strip()
    if not map_url:
        map_url = "REPLACE_WITH_PUBLIC_R2_MAP_URL"
    map_url_attribute = html.escape(map_url, quote=True)
    map_url_javascript = json.dumps(map_url)
    return f'''<!doctype html>
<html lang="en">
<head>
 <meta charset="utf-8" />
 <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
 <meta name="theme-color" content="#f3f5f7" />
 <title>Cape Cup Map</title>
 <style>
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; min-height: 100%; background: #f3f5f7; }}
  body {{ display: grid; place-items: center; padding: clamp(4px, 2vw, 24px); font-family: Arial, sans-serif; }}
  main {{ width: min(100%, 1700px); }}
  img {{ display: block; width: 100%; height: auto; max-height: calc(100vh - 2 * clamp(4px, 2vw, 24px));
    object-fit: contain; background: white; box-shadow: 0 8px 30px rgba(0,0,0,.13); touch-action: pinch-zoom; }}
  @media (max-width: 600px) {{
   body {{ display: block; padding: 0; }}
   img {{ min-height: 100svh; max-height: none; box-shadow: none; }}
  }}
 </style>
</head>
<body>
 <main>
  <img id="cape-cup-map" src="{map_url_attribute}" alt="Cape Cup game map" />
 </main>
 <script>
  const displayedMap = document.getElementById("cape-cup-map");

  // Public, read-only Cloudflare R2 URL. Never put R2 write credentials here.
  const MAP_URL = {map_url_javascript};
  const REFRESH_INTERVAL_MS = 2000;
  const REFRESH_TIMEOUT_MS = 10000;

  let refreshTimer = null;
  let refreshInProgress = false;

  function refreshMap() {{
   if (document.hidden || refreshInProgress) {{
    return;
   }}

   refreshInProgress = true;
   const nextImage = new Image();
   const refreshTimeout = window.setTimeout(() => {{
    nextImage.onload = null;
    nextImage.onerror = null;
    refreshInProgress = false;
   }}, REFRESH_TIMEOUT_MS);

   nextImage.onload = () => {{
    window.clearTimeout(refreshTimeout);
    displayedMap.src = nextImage.src;
    refreshInProgress = false;
   }};

   nextImage.onerror = () => {{
    // Keep displaying the last successfully loaded map.
    window.clearTimeout(refreshTimeout);
    refreshInProgress = false;
   }};

   const separator = MAP_URL.includes("?") ? "&" : "?";
   nextImage.src = `${{MAP_URL}}${{separator}}v=${{Date.now()}}`;
  }}

  function startRefreshing() {{
   if (refreshTimer !== null || document.hidden) {{
    return;
   }}

   refreshMap();
   refreshTimer = window.setInterval(refreshMap, REFRESH_INTERVAL_MS);
  }}

  function stopRefreshing() {{
   if (refreshTimer === null) {{
    return;
   }}

   window.clearInterval(refreshTimer);
   refreshTimer = null;
  }}

  document.addEventListener("visibilitychange", () => {{
   if (document.hidden) {{
    stopRefreshing();
   }} else {{
    startRefreshing();
   }}
  }});

  window.addEventListener("focus", () => {{
   if (!document.hidden) {{
    refreshMap();
   }}
  }});

  startRefreshing();
 </script>
</body>
</html>
'''


def static_html(source_svg: str, payload: dict[str, object]) -> str:
    svg = static_svg(source_svg, payload)

    return f'''<!doctype html>
<html lang="en">
<head>
 <meta charset="utf-8" />
 <meta name="viewport" content="width=device-width, initial-scale=1" />
 <title>Cape Cup Map</title>
 <style>
  html, body {{ margin: 0; min-height: 100%; background: #f7f7f5; }}
  body {{ display: grid; place-items: center; font-family: Arial, sans-serif; }}
  main {{ width: min(100%, 1500px); padding: 16px; box-sizing: border-box; }}
  svg {{ display: block; width: 100%; height: auto; background: white; box-shadow: 0 12px 36px rgba(0,0,0,.12); }}
 </style>
</head>
<body>
 <main aria-label="Cape Cup game map">
{svg}
 </main>
</body>
</html>
'''


def build_editor_html(source_svg: str) -> str:
    svg = editor_svg(source_svg)
    settings = current_r2_settings()
    endpoint_url = html.escape(settings["R2_ENDPOINT_URL"], quote=True)
    bucket_name = html.escape(settings["R2_BUCKET_NAME"], quote=True)
    public_map_url = html.escape(settings["R2_PUBLIC_MAP_URL"], quote=True)
    access_key_state = "Saved — enter a new value to replace" if settings["R2_ACCESS_KEY_ID"] else "Required"
    secret_key_state = "Saved — enter a new value to replace" if settings["R2_SECRET_ACCESS_KEY"] else "Required"
    settings_state = "R2 settings are saved locally." if all(settings.values()) else "Enter all five values, then save."
    swatches = "".join(
        f'<span class="swatch" style="--swatch:{value}" title="{name}"></span>'
        for name, value in COLORS
    )
    color_json = json.dumps([value for _, value in COLORS])

    return f'''<!doctype html>
<html lang="en">
<head>
 <meta charset="utf-8" />
 <meta name="viewport" content="width=device-width, initial-scale=1" />
 <title>Cape Cup Map Editor</title>
 <style>
  :root {{ color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: #eef1f5; color: #17202a; }}
  header {{ position: sticky; top: 0; z-index: 10; display: flex; gap: 16px; align-items: center;
    flex-wrap: wrap; padding: 12px 18px; background: rgba(255,255,255,.96); border-bottom: 1px solid #ccd3dd; }}
  h1 {{ margin: 0; font-size: 19px; }}
  .status {{ margin-left: auto; color: #4b5563; font-size: 14px; }}
  button {{ border: 0; border-radius: 8px; padding: 10px 14px; font-weight: 750; cursor: pointer; }}
  #export-project {{ background: #1f5fd1; color: white; }}
  #load-cloud-editable {{ background: #dbe8ff; color: #153b7a; }}
  #reset {{ background: #e5e9ef; color: #202833; }}
  .layout {{ display: grid; grid-template-columns: 300px minmax(0,1fr); gap: 16px; padding: 16px; }}
  aside {{ align-self: start; position: sticky; top: 82px; padding: 16px; background: white;
    border: 1px solid #d6dce5; border-radius: 12px; box-shadow: 0 6px 20px rgba(24,39,75,.07); }}
  aside h2 {{ margin: 0 0 10px; font-size: 16px; }}
  aside p, aside li {{ font-size: 13px; line-height: 1.45; }}
  aside ol {{ padding-left: 20px; }}
  .swatches {{ display: flex; flex-wrap: wrap; gap: 5px; margin: 12px 0; }}
  .swatch {{ width: 22px; height: 22px; border-radius: 50%; background: var(--swatch); border: 1px solid #9ca3af; }}
  .selection {{ padding: 10px; min-height: 42px; border-radius: 8px; background: #f0f4fa; font-size: 13px; }}
  details {{ margin-top: 16px; padding-top: 14px; border-top: 1px solid #d6dce5; }}
  summary {{ cursor: pointer; font-size: 15px; font-weight: 750; }}
  .r2-settings {{ display: grid; gap: 9px; margin-top: 12px; }}
  .r2-settings label {{ display: grid; gap: 4px; font-size: 12px; font-weight: 700; }}
  .r2-settings input {{ width: 100%; min-width: 0; border: 1px solid #b8c1ce; border-radius: 7px;
    padding: 8px; color: #17202a; background: white; font: inherit; font-size: 12px; }}
  #save-r2-settings {{ background: #253044; color: white; }}
  #r2-settings-state {{ min-height: 18px; color: #4b5563; font-size: 12px; line-height: 1.4; }}
  .canvas {{ overflow: auto; padding: 10px; background: white; border: 1px solid #d6dce5; border-radius: 12px;
    box-shadow: 0 6px 20px rgba(24,39,75,.07); }}
  .canvas svg {{ display: block; width: max(100%, 1100px); height: auto; }}
  .clickable-region {{ cursor: pointer; }}
  .clickable-region:hover {{ filter: brightness(1.06); }}
  @media (max-width: 850px) {{
   header {{ gap: 8px; }} h1 {{ flex-basis: 100%; }} .status {{ order: 10; flex-basis: 100%; margin-left: 0; }}
   .layout {{ grid-template-columns: 1fr; }} aside {{ position: static; }}
  }}
 </style>
</head>
<body>
 <header>
  <h1>Cape Cup Map Editor</h1>
  <button id="load-cloud-editable" type="button">Open cloud editable map</button>
  <button id="export-project" type="button">Publish both maps to R2</button>
  <button id="reset" type="button">Reset</button>
  <div id="status" class="status" role="status">Ready</div>
 </header>
 <div class="layout">
  <aside>
   <h2>How to edit</h2>
   <ol>
    <li>Open your previous <code>editable-map.svg</code> from R2.</li>
    <li>Click regions and type in their white boxes.</li>
    <li>Publish both SVG versions directly to R2.</li>
   </ol>
   <div class="swatches">{swatches}</div>
   <div id="selection" class="selection">No region selected</div>
   <p>Publish uploads <code>editable-map.svg</code> first and <code>map.svg</code> last. Map files are kept in memory and are not written to this computer.</p>
   <details open>
    <summary>R2 publishing settings</summary>
    <div class="r2-settings">
     <label>Endpoint URL
      <input id="r2-endpoint-url" type="url" value="{endpoint_url}" placeholder="https://ACCOUNT_ID.r2.cloudflarestorage.com" />
     </label>
     <label>Access key ID
      <input id="r2-access-key-id" type="password" autocomplete="off" placeholder="{access_key_state}" />
     </label>
     <label>Secret access key
      <input id="r2-secret-access-key" type="password" autocomplete="off" placeholder="{secret_key_state}" />
     </label>
     <label>Bucket name
      <input id="r2-bucket-name" type="text" value="{bucket_name}" placeholder="cape-cup-map" />
     </label>
     <label>Public map URL
      <input id="r2-public-map-url" type="url" value="{public_map_url}" placeholder="https://maps.example.com/map.svg" />
     </label>
     <button id="save-r2-settings" type="button">Save R2 settings</button>
     <div id="r2-settings-state" role="status">{settings_state}</div>
    </div>
   </details>
  </aside>
  <main class="canvas" id="map-canvas">
{svg}
  </main>
 </div>
 <script>
  (() => {{
   const colors = {color_json};
   const status = document.getElementById('status');
   const selection = document.getElementById('selection');
   const exportProjectButton = document.getElementById('export-project');
   const loadCloudEditableButton = document.getElementById('load-cloud-editable');
   const saveR2SettingsButton = document.getElementById('save-r2-settings');
   const r2SettingsState = document.getElementById('r2-settings-state');
   const regions = [...document.querySelectorAll('.clickable-region')];
   const inputs = [...document.querySelectorAll('.region-input')];
   let publishRequestActive = false;

   function setStatus(message, error = false) {{
    status.textContent = message;
    status.style.color = error ? '#b42318' : '#4b5563';
   }}
   function cycle(region) {{
    const current = Number(region.dataset.colorIndex || 7);
    const next = (current + 1) % colors.length;
    region.dataset.colorIndex = String(next);
    region.setAttribute('fill', colors[next]);
    selection.textContent = `${{region.dataset.region}} — ${{colors[next]}}`;
   }}
   regions.forEach(region => {{
    region.addEventListener('click', () => cycle(region));
    region.addEventListener('keydown', event => {{
     if (event.key === 'Enter' || event.key === ' ') {{ event.preventDefault(); cycle(region); }}
    }});
   }});
   inputs.forEach(input => {{
    input.addEventListener('click', event => event.stopPropagation());
    input.addEventListener('keydown', event => event.stopPropagation());
   }});

   function currentPayload() {{
    return {{
     regions: Object.fromEntries(regions.map(region => [region.id, {{
      name: region.dataset.region,
      fill: region.getAttribute('fill') || '#ffffff',
      colorIndex: Number(region.dataset.colorIndex || 7)
     }}])),
     notes: Object.fromEntries(inputs.map(input => [input.id.replace(/-input$/, ''), input.value]))
    }};
   }}

   async function publishMaps() {{
    if (publishRequestActive) {{
     return;
    }}
    publishRequestActive = true;
    exportProjectButton.disabled = true;
    loadCloudEditableButton.disabled = true;
    setStatus('Publishing map.svg and editable-map.svg to R2…');
    try {{
     const response = await fetch('/publish-maps', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(currentPayload())
     }});
     const result = await response.json();
     if (!response.ok) throw new Error(result.error || 'Publish failed');
     setStatus(result.message);
    }} catch (error) {{
     setStatus(error.message, true);
    }} finally {{
     publishRequestActive = false;
     exportProjectButton.disabled = false;
     loadCloudEditableButton.disabled = false;
    }}
   }}

   document.getElementById('reset').addEventListener('click', () => {{
    regions.forEach(region => {{ region.dataset.colorIndex = '7'; region.setAttribute('fill', '#ffffff'); }});
    inputs.forEach(input => input.value = '');
    selection.textContent = 'No region selected';
    setStatus('Map reset');
   }});

   exportProjectButton.addEventListener('click', publishMaps);

   loadCloudEditableButton.addEventListener('click', async () => {{
    if (publishRequestActive) return;
    publishRequestActive = true;
    exportProjectButton.disabled = true;
    loadCloudEditableButton.disabled = true;
    setStatus('Loading editable-map.svg from R2…');
    try {{
     const response = await fetch('/load-cloud-editable', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: '{{}}'
     }});
     const result = await response.json();
     if (!response.ok) throw new Error(result.error || 'Could not load the cloud editable map');
     window.location.reload();
    }} catch (error) {{
     setStatus(error.message, true);
     publishRequestActive = false;
     exportProjectButton.disabled = false;
     loadCloudEditableButton.disabled = false;
    }}
   }});

   saveR2SettingsButton.addEventListener('click', async () => {{
    saveR2SettingsButton.disabled = true;
    r2SettingsState.textContent = 'Saving…';
    try {{
     const response = await fetch('/r2-settings', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{
       R2_ENDPOINT_URL: document.getElementById('r2-endpoint-url').value,
       R2_ACCESS_KEY_ID: document.getElementById('r2-access-key-id').value,
       R2_SECRET_ACCESS_KEY: document.getElementById('r2-secret-access-key').value,
       R2_BUCKET_NAME: document.getElementById('r2-bucket-name').value,
       R2_PUBLIC_MAP_URL: document.getElementById('r2-public-map-url').value
      }})
     }});
     const result = await response.json();
     if (!response.ok) throw new Error(result.error || 'Could not save R2 settings');
     document.getElementById('r2-access-key-id').value = '';
     document.getElementById('r2-secret-access-key').value = '';
     document.getElementById('r2-access-key-id').placeholder = 'Saved — enter a new value to replace';
     document.getElementById('r2-secret-access-key').placeholder = 'Saved — enter a new value to replace';
     r2SettingsState.textContent = result.message;
     setStatus(result.message);
    }} catch (error) {{
     r2SettingsState.textContent = error.message;
     setStatus(error.message, true);
    }} finally {{
     saveR2SettingsButton.disabled = false;
    }}
   }});

  }})();
 </script>
</body>
</html>
'''


class EditorApplication:
    def __init__(self, source_path: Path):
        load_local_r2_settings()
        self.source_path = source_path
        self.source_svg = source_path.read_text(encoding="utf-8")
        validate_editor_svg(self.source_svg)
        self.editor_page = build_editor_html(self.source_svg).encode("utf-8")
        self.lock = threading.Lock()
        self.publish_lock = threading.Lock()
    def load_cloud_editable(self) -> None:
        svg = download_editable_map()
        validate_editor_svg(svg)
        with self.lock:
            self.source_svg = svg
            self.editor_page = build_editor_html(svg).encode("utf-8")

    def save_r2_settings(self, payload: dict[str, object]) -> None:
        current = current_r2_settings()
        updated: dict[str, str] = {}
        for name in R2_ENVIRONMENT_VARIABLES:
            supplied = payload.get(name, "")
            if not isinstance(supplied, str):
                raise PublishError(f"{name} must be text.")
            value = supplied.strip()
            # Empty credential fields preserve the saved values because secrets
            # are deliberately not rendered back into the editor page.
            if name in {"R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"} and not value:
                value = current[name]
            updated[name] = value

        save_local_r2_settings(updated)
        with self.lock:
            self.editor_page = build_editor_html(self.source_svg).encode("utf-8")

    def publish_maps(self, payload: dict[str, object], upload: bool = True) -> str | None:
        if not self.publish_lock.acquire(blocking=False):
            raise PublishInProgressError("A publish is already in progress. Please wait for it to finish.")
        try:
            with self.lock:
                source_svg = self.source_svg
            editable = editable_svg(source_svg, payload)
            map_svg = static_svg(source_svg, payload)
            public_url = upload_map_versions(map_svg, editable) if upload else None
            with self.lock:
                self.source_svg = editable
                self.editor_page = build_editor_html(editable).encode("utf-8")
            return public_url
        finally:
            self.publish_lock.release()


def make_handler(app: EditorApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CapeCupEditor/1.0"

        def log_message(self, fmt: str, *args: object) -> None:
            print(f"[{self.log_date_time_string()}] {fmt % args}")

        def send_bytes(self, content: bytes, content_type: str, filename: str | None = None) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(content)

        def send_json(self, data: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
            content = json.dumps(data).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/":
                self.send_bytes(app.editor_page, "text/html; charset=utf-8")
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path not in {"/publish-maps", "/load-cloud-editable", "/r2-settings"}:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_REQUEST_BYTES:
                    raise ValueError("Invalid request size.")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("Invalid request data.")
                if path == "/r2-settings":
                    app.save_r2_settings(payload)
                    self.send_json({"ok": True, "message": "R2 settings saved locally. You can publish now."})
                elif path == "/load-cloud-editable":
                    app.load_cloud_editable()
                    self.send_json({"ok": True, "message": "Loaded editable-map.svg from R2."})
                else:
                    public_url = app.publish_maps(payload)
                    self.send_json(
                        {
                            "ok": True,
                            "message": (
                                "Published map.svg and editable-map.svg to Cloudflare R2: "
                                f"{public_url}"
                            ),
                        }
                    )
            except PublishInProgressError as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
            except PublishError as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_GATEWAY)
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except OSError as exc:
                self.send_json(
                    {"ok": False, "error": f"Could not update the local editor settings: {exc}"},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )

    return Handler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Edit the Cape Cup map and publish both SVG versions to R2.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="SVG source file")
    parser.add_argument("--port", type=int, default=8765, help="Local editor port (default: 8765)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the browser automatically")
    parser.add_argument("--check", action="store_true", help="Validate files without uploading, then exit")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app = EditorApplication(args.source.resolve())
    if args.check:
        default = {"regions": {}, "notes": {}}
        app.publish_maps(default, upload=False)
        print("Validated 43 regions; no files were written or uploaded.")
        return 0

    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(app))
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Cape Cup Map Editor: {url}")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.35, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nEditor stopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
