"""Local web server for Metadata Scrubber.

Endpoints:
    GET  /                       → index.html
    POST /api/inspect            → multipart file, returns inspection report
    POST /api/scrub              → multipart file, returns cleaned binary
    POST /api/convert            → multipart file + target_format, returns converted binary
    POST /api/scrub-local-path   → JSON { path, recursive, backup }, in-place scrub
    POST /api/inspect-local-path → JSON { path, recursive }, read-only inspection
    GET  /api/status             → health / capability probe
"""

from __future__ import annotations

import argparse
import base64
import io
import logging
import os
import threading
import webbrowser
from pathlib import Path

from aiohttp import web
from PIL import Image

from engine import (
    AVIF_AVAILABLE,
    FORMAT_EXTENSIONS,
    HEIF_AVAILABLE,
    convert_bytes,
    inspect_directory,
    inspect_metadata,
    scrub_bytes,
    scrub_directory_inplace,
    scrub_file_inplace,
    verify_clean,
)

BASE_DIR = Path(__file__).parent.resolve()
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)


def _generate_thumbnail(data: bytes, file_type: str) -> str | None:
    """Return a base64 data-URI thumbnail for image formats."""
    if file_type not in ("PNG", "JPEG", "HEIC", "WEBP", "TIFF", "BMP", "AVIF"):
        return None
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.thumbnail((260, 260))
            buf = io.BytesIO()
            if img.mode in ("RGBA", "LA") or (
                img.mode == "P" and "transparency" in img.info
            ):
                img.save(buf, format="PNG")
                mime = "image/png"
            else:
                img.convert("RGB").save(buf, format="JPEG", quality=80)
                mime = "image/jpeg"
            encoded = base64.b64encode(buf.getvalue()).decode("ascii")
            return f"data:{mime};base64,{encoded}"
    except Exception:
        return None


async def _read_multipart(request: web.Request) -> dict:
    """Read a multipart body into { field_name: value_or_bytes }."""
    reader = await request.multipart()
    out: dict = {}
    while True:
        part = await reader.next()
        if part is None:
            break
        name = part.name
        if part.filename:
            buf = bytearray()
            while True:
                chunk = await part.read_chunk()
                if not chunk:
                    break
                buf.extend(chunk)
            out[name] = {"filename": part.filename, "bytes": bytes(buf)}
        else:
            out[name] = (await part.read()).decode("utf-8", "replace")
    return out


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def handle_index(request: web.Request) -> web.Response:
    index_file = TEMPLATES_DIR / "index.html"
    if not index_file.exists():
        return web.Response(text="index.html not found", status=404)
    return web.FileResponse(index_file)


async def handle_inspect(request: web.Request) -> web.Response:
    parts = await _read_multipart(request)
    file_part = parts.get("file")
    if not isinstance(file_part, dict):
        return web.json_response({"error": "No file uploaded"}, status=400)

    filename = file_part["filename"] or "uploaded_file"
    data = file_part["bytes"]

    inspection = inspect_metadata(data, filename)
    ftype = inspection.get("file_type", "")
    thumbnail = _generate_thumbnail(data, ftype)

    return web.json_response(
        {"success": True, "inspection": inspection, "thumbnail": thumbnail}
    )


async def handle_scrub(request: web.Request) -> web.Response:
    parts = await _read_multipart(request)
    file_part = parts.get("file")
    if not isinstance(file_part, dict):
        return web.json_response({"error": "No file uploaded"}, status=400)

    filename = file_part["filename"] or "uploaded_file"
    data = file_part["bytes"]

    try:
        scrubbed, summary = scrub_bytes(data, filename)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

    headers = {
        "Content-Disposition": f'attachment; filename="cleaned_{filename}"',
        "Content-Type": "application/octet-stream",
        "X-Original-Size": str(summary["original_size"]),
        "X-Cleaned-Size": str(summary["cleaned_size"]),
        "X-Bytes-Saved": str(summary["bytes_saved"]),
        "X-Stripped-Count": str(summary["stripped_count"]),
        "Access-Control-Expose-Headers": (
            "X-Original-Size, X-Cleaned-Size, X-Bytes-Saved, X-Stripped-Count"
        ),
    }
    return web.Response(body=scrubbed, headers=headers)


async def handle_convert(request: web.Request) -> web.Response:
    parts = await _read_multipart(request)
    file_part = parts.get("file")
    if not isinstance(file_part, dict):
        return web.json_response({"error": "No file uploaded"}, status=400)

    filename = file_part["filename"] or "uploaded_file"
    data = file_part["bytes"]

    target_format = (parts.get("target_format") or "JPEG").upper()
    try:
        quality = int(parts.get("quality") or 85)
    except Exception:
        quality = 85

    raw_max = parts.get("max_edge")
    if raw_max is None or str(raw_max).lower() in ("", "none", "0"):
        max_edge: int | None = 1920
    else:
        try:
            max_edge = max(64, int(raw_max))
        except Exception:
            max_edge = 1920

    try:
        converted, summary = convert_bytes(
            data,
            filename,
            target_format,
            quality=quality,
            max_edge=max_edge,
        )
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

    verification = verify_clean(
        converted, f"clean{FORMAT_EXTENSIONS.get(target_format, '.bin')}"
    )

    out_name = Path(filename).stem + FORMAT_EXTENSIONS.get(target_format, ".bin")

    headers = {
        "Content-Disposition": f'attachment; filename="{out_name}"',
        "Content-Type": "application/octet-stream",
        "X-Converted-From": summary.get("source_format", ""),
        "X-Converted-To": summary.get("target_format", ""),
        "X-Bytes-Saved": str(summary.get("bytes_saved", 0)),
        "X-Verified-Clean": "true" if verification["clean"] else "false",
        "X-Remaining-Metadata": str(verification["remaining_count"]),
        "X-Destroyed-Count": str(summary.get("destroyed_count", 0)),
        "X-Destroyed-Items": "|".join(summary.get("destroyed_items", [])[:20]),
        "Access-Control-Expose-Headers": (
            "X-Converted-From, X-Converted-To, X-Bytes-Saved, "
            "X-Verified-Clean, X-Remaining-Metadata, "
            "X-Destroyed-Count, X-Destroyed-Items"
        ),
    }
    return web.Response(body=converted, headers=headers)


async def handle_scrub_local_path(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    target_path = (body.get("path") or "").strip()
    if not target_path:
        return web.json_response({"error": "Path cannot be empty"}, status=400)

    p = Path(os.path.expanduser(target_path)).resolve()
    if not p.exists():
        return web.json_response({"error": f"Path not found: {p}"}, status=404)

    backup = bool(body.get("backup", False))
    recursive = bool(body.get("recursive", True))

    try:
        if p.is_file():
            result = scrub_file_inplace(p, backup=backup)
            return web.json_response(
                {"success": True, "mode": "file", "result": result}
            )
        elif p.is_dir():
            result = scrub_directory_inplace(
                p, recursive=recursive, backup=backup
            )
            return web.json_response(
                {"success": True, "mode": "directory", "result": result}
            )
        else:
            return web.json_response(
                {"error": "Target must be a regular file or directory"}, status=400
            )
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_inspect_local_path(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    target_path = (body.get("path") or "").strip()
    if not target_path:
        return web.json_response({"error": "Path cannot be empty"}, status=400)

    p = Path(os.path.expanduser(target_path)).resolve()
    if not p.exists():
        return web.json_response({"error": f"Path not found: {p}"}, status=404)

    recursive = bool(body.get("recursive", True))

    try:
        if p.is_file():
            with open(p, "rb") as f:
                data = f.read()
            result = inspect_metadata(data, p.name)
            result["path"] = str(p)
            return web.json_response(
                {"success": True, "mode": "file", "result": result}
            )
        elif p.is_dir():
            result = inspect_directory(p, recursive=recursive)
            return web.json_response(
                {"success": True, "mode": "directory", "result": result}
            )
        else:
            return web.json_response(
                {"error": "Target must be a regular file or directory"}, status=400
            )
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_status(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "status": "online",
            "app": "Hermes",
            "version": "2.0.0",
            "lossless_scrub_formats": ["PNG", "JPEG", "PDF"],
            "convertible_source_formats": [
                "PNG",
                "JPEG",
                "HEIC",
                "WEBP",
                "TIFF",
                "BMP",
                "AVIF",
            ],
            "convertible_target_formats": ["JPEG", "PNG", "WEBP"],
            "heif_available": HEIF_AVAILABLE,
            "avif_available": AVIF_AVAILABLE,
        }
    )


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app() -> web.Application:
    app = web.Application(client_max_size=200 * 1024 * 1024)
    app.router.add_get("/", handle_index)
    app.router.add_post("/api/inspect", handle_inspect)
    app.router.add_post("/api/scrub", handle_scrub)
    app.router.add_post("/api/convert", handle_convert)
    app.router.add_post("/api/scrub-local-path", handle_scrub_local_path)
    app.router.add_post("/api/inspect-local-path", handle_inspect_local_path)
    app.router.add_get("/api/status", handle_status)

    app.router.add_static("/static", STATIC_DIR, show_index=False)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Start the Hermes Web Application"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")

    args = parser.parse_args()

    app = create_app()
    url = f"http://{args.host}:{args.port}"

    print(f"\n Hermes running at: {url}")
    print("  HEIF (HEIC/HEIF): " + ("available" if HEIF_AVAILABLE else "MISSING — install pillow-heif"))
    print("  AVIF:             " + ("available" if AVIF_AVAILABLE else "not available (Pillow < 11.3)"))
    print("  Press Ctrl+C to stop.\n")

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()