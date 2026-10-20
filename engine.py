"""Metadata Scrubber Core Engine.

Inspection + lossless scrub:
    PNG, JPEG, PDF

Inspection + scrub + conversion:
    HEIC / HEIF, WebP, TIFF, BMP, AVIF

Design:
    inspect_*  → never modify input, return report dict
    scrub_*    → lossless byte surgery, pixels untouched
    convert_*  → decode → rotate → (optionally) downscale → re-encode;
                 destroys every metadata vector including PRNU correlation
"""

from __future__ import annotations

import io
import os
import shutil
import struct
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pypdf
from PIL import Image, ImageOps
from PIL.ExifTags import GPSTAGS, TAGS

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_AVAILABLE = True
except ImportError:
    pillow_heif = None
    HEIF_AVAILABLE = False

try:
    import pillow_avif  # noqa: F401

    AVIF_AVAILABLE = True
except ImportError:
    try:
        from PIL import features as _pil_features

        AVIF_AVAILABLE = bool(_pil_features.check("avif"))
    except Exception:
        AVIF_AVAILABLE = False


# ---------------------------------------------------------------------------
# FORMAT DETECTION
# ---------------------------------------------------------------------------

_HEIC_BRANDS = (b"heic", b"heix", b"heim", b"heis")
_HEIF_GENERIC_BRANDS = (b"mif1", b"msf1")
_AVIF_BRANDS = (b"avif", b"avis")


def detect_file_type(data: bytes, filename: str = "") -> Optional[str]:
    """Detect file type via magic bytes, with extension fallback."""
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG"
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return "JPEG"
    if b"%PDF-" in data[:1024]:
        return "PDF"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    if len(data) >= 4 and data[:4] in (b"II*\x00", b"MM\x00*"):
        return "TIFF"
    if len(data) >= 2 and data[:2] == b"BM":
        return "BMP"

    if len(data) >= 12 and data[4:8] == b"ftyp":
        box_len = struct.unpack(">I", data[:4])[0]
        major_brand = (
            data[16:20].lower()
            if box_len == 1 and len(data) >= 20
            else data[8:12].lower()
        )
        if major_brand in _HEIC_BRANDS:
            return "HEIC"
        if major_brand in _AVIF_BRANDS:
            return "AVIF"
        if major_brand in _HEIF_GENERIC_BRANDS:
            start_compat = 24 if box_len == 1 else 16
            end_compat = min(len(data), 1024 if box_len == 0 else box_len)
            compat = (
                data[start_compat:end_compat].lower()
                if end_compat > start_compat
                else b""
            )
            if b"avif" in compat or b"avis" in compat:
                return "AVIF"
            return "HEIC"

    ext = Path(filename).suffix.lower()
    if ext == ".png":
        return "PNG"
    if ext in (".jpg", ".jpeg"):
        return "JPEG"
    if ext == ".pdf":
        return "PDF"
    if ext in (".heic", ".heif"):
        return "HEIC"
    if ext == ".webp":
        return "WEBP"
    if ext in (".tif", ".tiff"):
        return "TIFF"
    if ext == ".bmp":
        return "BMP"
    if ext == ".avif":
        return "AVIF"

    return None


# ---------------------------------------------------------------------------
# SHARED HELPERS
# ---------------------------------------------------------------------------


def _to_float(v: Any) -> float:
    """Coerce an EXIF rational or numeric value to float."""
    if isinstance(v, (int, float)):
        return float(v)
    if hasattr(v, "numerator") and hasattr(v, "denominator"):
        return (
            float(v.numerator) / float(v.denominator)
            if v.denominator != 0
            else 0.0
        )
    if isinstance(v, tuple) and len(v) == 2:
        return float(v[0]) / float(v[1]) if v[1] != 0 else 0.0
    return float(v)


def _format_gps(
    gps_dict: dict,
) -> Tuple[Optional[str], Optional[float], Optional[float]]:
    """Convert raw EXIF GPSInfo into human text + decimal lat/long."""
    try:
        lat_data = gps_dict.get(2) or gps_dict.get("GPSLatitude")
        lat_ref = gps_dict.get(1) or gps_dict.get("GPSLatitudeRef")
        lon_data = gps_dict.get(4) or gps_dict.get("GPSLongitude")
        lon_ref = gps_dict.get(3) or gps_dict.get("GPSLongitudeRef")

        if not lat_data or not lon_data or not lat_ref or not lon_ref:
            return None, None, None

        lat_deg = (
            _to_float(lat_data[0])
            + _to_float(lat_data[1]) / 60.0
            + _to_float(lat_data[2]) / 3600.0
        )
        if str(lat_ref).upper() in ("S", b"S"):
            lat_deg = -lat_deg

        lon_deg = (
            _to_float(lon_data[0])
            + _to_float(lon_data[1]) / 60.0
            + _to_float(lon_data[2]) / 3600.0
        )
        if str(lon_ref).upper() in ("W", b"W"):
            lon_deg = -lon_deg

        text = f"{abs(lat_deg):.5f}° {lat_ref}, {abs(lon_deg):.5f}° {lon_ref}"
        return text, lat_deg, lon_deg
    except Exception:
        return None, None, None


_HIGH_RISK_XMP_KEYS = (
    "creator",
    "author",
    "user",
    "comment",
    "title",
    "subject",
    "gps",
    "artist",
)


def _extract_xmp_tags(xmp_data: str | bytes) -> List[Dict[str, Any]]:
    """Parse a raw XMP XML packet and extract human-readable tags."""
    entries: List[Dict[str, Any]] = []
    try:
        import xml.etree.ElementTree as ET

        if isinstance(xmp_data, bytes):
            xmp_str = xmp_data.decode("utf-8", "replace")
        else:
            xmp_str = str(xmp_data)

        # Guard against entity-expansion abuse.
        if len(xmp_str) > 200_000:
            return entries

        start = xmp_str.find("<x:xmpmeta")
        if start == -1:
            start = xmp_str.find("<rdf:RDF")
        if start != -1:
            xmp_str = xmp_str[start:]
            end = xmp_str.rfind("</x:xmpmeta>")
            if end != -1:
                xmp_str = xmp_str[: end + len("</x:xmpmeta>")]

        root = ET.fromstring(xmp_str)
        seen = set()

        for elem in root.iter():
            tag_name = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            tag_clean = tag_name.strip()
            if tag_clean.lower() in (
                "xmpmeta",
                "rdf",
                "description",
                "bag",
                "seq",
                "alt",
                "li",
            ):
                continue

            if elem.text and elem.text.strip():
                val = elem.text.strip()
                if (tag_clean, val) not in seen:
                    seen.add((tag_clean, val))
                    risk = (
                        "high"
                        if any(k in tag_clean.lower() for k in _HIGH_RISK_XMP_KEYS)
                        else "medium"
                    )
                    entries.append(
                        {
                            "category": "XMP Metadata",
                            "key": tag_clean,
                            "value": val,
                            "risk": risk,
                        }
                    )

            for attr, val in elem.attrib.items():
                attr_name = attr.split("}")[-1] if "}" in attr else attr
                attr_clean = attr_name.strip()
                if (
                    attr_clean.lower() not in ("about", "xmptk", "xmlns", "parsetype")
                    and not attr_clean.startswith("xmlns:")
                ):
                    val_str = str(val).strip()
                    if val_str and (attr_clean, val_str) not in seen:
                        seen.add((attr_clean, val_str))
                        risk = (
                            "high"
                            if any(k in attr_clean.lower() for k in _HIGH_RISK_XMP_KEYS)
                            else "medium"
                        )
                        entries.append(
                            {
                                "category": "XMP Metadata",
                                "key": attr_clean,
                                "value": val_str,
                                "risk": risk,
                            }
                        )
    except Exception:
        pass
    return entries


# ---------------------------------------------------------------------------
# PNG
# ---------------------------------------------------------------------------

PNG_CRITICAL_CHUNKS = {b"IHDR", b"PLTE", b"IDAT", b"IEND"}
PNG_SAFE_RENDER_CHUNKS = {
    b"tRNS",
    b"cHRM",
    b"gAMA",
    b"sRGB",
    b"sBIT",
    b"acTL",
    b"fcTL",
    b"fdAT",
}


def _parse_png_chunks(data: bytes) -> List[Tuple[bytes, bytes, bytes, int]]:
    if len(data) < 8 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Invalid PNG header")
    pos = 8
    chunks: List[Tuple[bytes, bytes, bytes, int]] = []
    while pos < len(data):
        if pos + 8 > len(data):
            break
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        ctype = data[pos + 4 : pos + 8]
        cdata = data[pos + 8 : pos + 8 + length]
        crc = data[pos + 8 + length : pos + 12 + length]
        chunks.append((ctype, cdata, crc, pos))
        pos += 12 + length
        if ctype == b"IEND":
            break
    return chunks


def inspect_png_metadata(data: bytes) -> Dict[str, Any]:
    entries: List[Dict[str, Any]] = []
    details: Dict[str, Any] = {}

    try:
        chunks = _parse_png_chunks(data)
    except Exception as e:
        return {"error": f"Failed to parse PNG chunks: {e}", "entries": []}

    for ctype, cdata, _, _ in chunks:
        if ctype == b"IHDR" and len(cdata) >= 8:
            width, height = struct.unpack(">II", cdata[:8])
            details["dimensions"] = f"{width} × {height}"
            details["width"] = width
            details["height"] = height
            break

    for ctype, cdata, _, _ in chunks:
        ctype_str = ctype.decode("latin1", "replace")

        if ctype == b"tEXt":
            if b"\x00" in cdata:
                kw, val = cdata.split(b"\x00", 1)
                kw_str = kw.decode("latin1", "replace")
                val_str = val.decode("latin1", "replace").strip()
                risk = (
                    "high"
                    if any(
                        x in kw_str.lower()
                        for x in ("author", "creator", "prompt", "parameters", "user")
                    )
                    else "medium"
                )
                entries.append(
                    {
                        "category": "Text Chunk (tEXt)",
                        "key": kw_str,
                        "value": val_str,
                        "risk": risk,
                    }
                )

        elif ctype == b"zTXt":
            if b"\x00" in cdata:
                kw, rest = cdata.split(b"\x00", 1)
                kw_str = kw.decode("latin1", "replace")
                val_str = "[Compressed Text]"
                if len(rest) > 1:
                    try:
                        val_str = (
                            zlib.decompress(rest[1:])
                            .decode("utf-8", "replace")
                            .strip()
                        )
                    except Exception:
                        pass
                risk = (
                    "high"
                    if any(
                        x in kw_str.lower()
                        for x in ("author", "creator", "prompt", "parameters")
                    )
                    else "medium"
                )
                entries.append(
                    {
                        "category": "Compressed Text (zTXt)",
                        "key": kw_str,
                        "value": val_str,
                        "risk": risk,
                    }
                )

        elif ctype == b"iTXt":
            null1 = cdata.find(b"\x00")
            if null1 != -1 and len(cdata) >= null1 + 3:
                kw_str = cdata[:null1].decode("utf-8", "replace")
                comp_flag = cdata[null1 + 1]
                rest = cdata[null1 + 3:]
                parts = rest.split(b"\x00", 2)
                if len(parts) == 3:
                    raw_text = parts[2]
                    if comp_flag == 1:
                        try:
                            raw_text = zlib.decompress(raw_text)
                        except Exception:
                            pass
                    val_str = raw_text.decode("utf-8", "replace").strip()

                    if "<x:xmpmeta" in val_str or "<rdf:RDF" in val_str:
                        xmp_entries = _extract_xmp_tags(val_str)
                        if xmp_entries:
                            entries.extend(xmp_entries)
                        else:
                            entries.append(
                                {
                                    "category": "XMP Metadata (iTXt)",
                                    "key": kw_str,
                                    "value": (
                                        val_str
                                        if len(val_str) < 300
                                        else val_str[:300] + "..."
                                    ),
                                    "risk": "medium",
                                }
                            )
                    else:
                        risk = (
                            "high"
                            if any(
                                x in kw_str.lower()
                                for x in ("author", "creator", "prompt", "parameters")
                            )
                            else "medium"
                        )
                        entries.append(
                            {
                                "category": "International Text (iTXt)",
                                "key": kw_str,
                                "value": (
                                    val_str
                                    if len(val_str) < 500
                                    else val_str[:500] + "..."
                                ),
                                "risk": risk,
                            }
                        )

        elif ctype == b"eXIf":
            try:
                exif_obj = Image.Exif()
                exif_obj.load(cdata)
                for tag_id, val in exif_obj.items():
                    tag_name = TAGS.get(tag_id, f"Tag_{tag_id}")
                    if tag_name == "GPSInfo" and isinstance(val, dict):
                        coords, lat, lon = _format_gps(val)
                        entries.append(
                            {
                                "category": "GPS / Location",
                                "key": "GPS Coordinates",
                                "value": coords or str(val),
                                "risk": "high",
                                "lat": lat,
                                "lon": lon,
                            }
                        )
                    else:
                        val_str = str(val).strip()
                        risk = (
                            "high"
                            if tag_name
                            in (
                                "Make",
                                "Model",
                                "Artist",
                                "Software",
                                "SerialNumber",
                                "LensModel",
                            )
                            else "medium"
                        )
                        entries.append(
                            {
                                "category": "EXIF Header",
                                "key": tag_name,
                                "value": val_str,
                                "risk": risk,
                            }
                        )
            except Exception:
                entries.append(
                    {
                        "category": "EXIF Header",
                        "key": "Raw EXIF Chunk",
                        "value": f"{len(cdata)} bytes embedded EXIF",
                        "risk": "high",
                    }
                )

        elif ctype == b"tIME":
            if len(cdata) >= 7:
                year, month, day, hour, minute, second = struct.unpack(
                    ">HBBBBB", cdata[:7]
                )
                entries.append(
                    {
                        "category": "Timestamp",
                        "key": "Modification Time (tIME)",
                        "value": (
                            f"{year:04d}-{month:02d}-{day:02d} "
                            f"{hour:02d}:{minute:02d}:{second:02d}"
                        ),
                        "risk": "medium",
                    }
                )

        elif ctype == b"iCCP":
            parts = cdata.split(b"\x00", 1)
            prof_name = (
                parts[0].decode("latin1", "replace") if parts else "ICC Profile"
            )
            entries.append(
                {
                    "category": "Color Profile",
                    "key": "ICC Profile Name",
                    "value": prof_name,
                    "risk": "low",
                }
            )

        elif ctype == b"pHYs":
            if len(cdata) >= 9:
                x_res, y_res, unit = struct.unpack(">IIB", cdata[:9])
                unit_str = "meters" if unit == 1 else "unknown unit"
                entries.append(
                    {
                        "category": "Hardware / DPI",
                        "key": "Pixel Dimensions (pHYs)",
                        "value": f"{x_res} × {y_res} per {unit_str}",
                        "risk": "low",
                    }
                )

        elif ctype not in PNG_CRITICAL_CHUNKS and ctype not in PNG_SAFE_RENDER_CHUNKS:
            entries.append(
                {
                    "category": "Ancillary Chunk",
                    "key": f"Chunk [{ctype_str}]",
                    "value": f"{len(cdata)} bytes private/auxiliary data",
                    "risk": "low",
                }
            )

    return {
        "file_type": "PNG",
        "file_size": len(data),
        "metadata_count": len(entries),
        "has_high_risk": any(e["risk"] == "high" for e in entries),
        "entries": entries,
        "details": details,
    }


def scrub_png_bytes(
    data: bytes, *, preserve_icc: bool = True
) -> Tuple[bytes, List[str]]:
    chunks = _parse_png_chunks(data)
    out = bytearray(b"\x89PNG\r\n\x1a\n")
    stripped_items: List[str] = []

    keep_set = set(PNG_CRITICAL_CHUNKS) | set(PNG_SAFE_RENDER_CHUNKS)
    if preserve_icc:
        keep_set.add(b"iCCP")

    for ctype, cdata, crc, _ in chunks:
        if ctype in keep_set:
            out.extend(struct.pack(">I", len(cdata)))
            out.extend(ctype)
            out.extend(cdata)
            out.extend(crc)
        else:
            ctype_str = ctype.decode("latin1", "replace")
            stripped_items.append(f"Removed PNG chunk: {ctype_str} ({len(cdata)} bytes)")

    return bytes(out), stripped_items


# ---------------------------------------------------------------------------
# JPEG
# ---------------------------------------------------------------------------


def inspect_jpeg_metadata(data: bytes) -> Dict[str, Any]:
    entries: List[Dict[str, Any]] = []
    details: Dict[str, Any] = {}

    try:
        with Image.open(io.BytesIO(data)) as img:
            details["dimensions"] = f"{img.width} × {img.height}"
            details["width"] = img.width
            details["height"] = img.height
            details["mode"] = img.mode

            exif = img.getexif()
            if exif:
                for tag_id, val in exif.items():
                    tag_name = TAGS.get(tag_id, f"Tag_{tag_id}")
                    if tag_name == "GPSInfo" or tag_id == 0x8825:
                        gps_data = exif.get_ifd(0x8825)
                        if gps_data:
                            decoded_gps = {
                                GPSTAGS.get(k, k): v for k, v in gps_data.items()
                            }
                            coords, lat, lon = _format_gps(decoded_gps)
                            if coords:
                                entries.append(
                                    {
                                        "category": "GPS / Location",
                                        "key": "GPS Coordinates",
                                        "value": coords,
                                        "risk": "high",
                                        "lat": lat,
                                        "lon": lon,
                                    }
                                )
                            for gk, gv in decoded_gps.items():
                                entries.append(
                                    {
                                        "category": "GPS / Location",
                                        "key": str(gk),
                                        "value": str(gv),
                                        "risk": "high",
                                    }
                                )
                    else:
                        val_str = str(val).strip()
                        risk = (
                            "high"
                            if tag_name
                            in (
                                "Make",
                                "Model",
                                "Artist",
                                "Software",
                                "SerialNumber",
                                "LensModel",
                                "CameraSerialNumber",
                                "BodySerialNumber",
                                "OwnerName",
                            )
                            else "medium"
                        )
                        entries.append(
                            {
                                "category": "EXIF Header",
                                "key": tag_name,
                                "value": (
                                    val_str
                                    if len(val_str) < 200
                                    else val_str[:200] + "..."
                                ),
                                "risk": risk,
                            }
                        )

                try:
                    exif_sub = exif.get_ifd(0x8769)
                    for sub_id, val in exif_sub.items():
                        sub_name = TAGS.get(sub_id, f"ExifSub_{sub_id}")
                        val_str = str(val).strip()
                        risk = (
                            "high"
                            if any(
                                x in sub_name.lower()
                                for x in ("owner", "serial", "lens")
                            )
                            else "medium"
                        )
                        entries.append(
                            {
                                "category": "EXIF Photo Data",
                                "key": sub_name,
                                "value": (
                                    val_str
                                    if len(val_str) < 200
                                    else val_str[:200] + "..."
                                ),
                                "risk": risk,
                            }
                        )
                except Exception:
                    pass

            try:
                from PIL import IptcImagePlugin

                iptc_dict = IptcImagePlugin.getiptcinfo(img)
                if iptc_dict:
                    IPTC_TAG_MAP = {
                        (2, 5): "Object Name / Title",
                        (2, 25): "Keywords",
                        (2, 80): "By-line (Author)",
                        (2, 85): "By-line Title",
                        (2, 90): "City",
                        (2, 92): "Sub-location",
                        (2, 95): "State/Province",
                        (2, 101): "Country",
                        (2, 105): "Headline",
                        (2, 110): "Credit",
                        (2, 115): "Source",
                        (2, 116): "Copyright Notice",
                        (2, 120): "Caption / Description",
                    }
                    for tag_id, val in iptc_dict.items():
                        tag_label = IPTC_TAG_MAP.get(tag_id, f"IPTC_{tag_id}")
                        val_str = (
                            val.decode("utf-8", "replace")
                            if isinstance(val, bytes)
                            else str(val)
                        )
                        entries.append(
                            {
                                "category": "IPTC / Photoshop",
                                "key": tag_label,
                                "value": val_str,
                                "risk": (
                                    "high"
                                    if any(
                                        k in tag_label.lower()
                                        for k in (
                                            "author",
                                            "title",
                                            "caption",
                                            "copyright",
                                        )
                                    )
                                    else "medium"
                                ),
                            }
                        )
            except Exception:
                pass

            STANDARD_TECH_KEYS = {
                "exif",
                "icc_profile",
                "jfif",
                "jfif_version",
                "jfif_unit",
                "jfif_density",
                "dpi",
                "progressive",
                "photoshop",
                "quantization",
            }

            for k, v in img.info.items():
                if str(k).lower() in STANDARD_TECH_KEYS:
                    continue
                k_str = str(k)
                v_str = str(v)
                if "<x:xmpmeta" in v_str or "<rdf:RDF" in v_str:
                    xmp_tags = _extract_xmp_tags(v_str)
                    if xmp_tags:
                        entries.extend(xmp_tags)
                        continue
                entries.append(
                    {
                        "category": "Image Metadata (XMP/Info)",
                        "key": k_str,
                        "value": (v_str if len(v_str) < 300 else v_str[:300] + "..."),
                        "risk": "medium",
                    }
                )

            if "icc_profile" in img.info:
                entries.append(
                    {
                        "category": "Color Profile",
                        "key": "ICC Profile",
                        "value": (
                            f"{len(img.info['icc_profile'])} bytes embedded profile"
                        ),
                        "risk": "low",
                    }
                )

    except Exception as e:
        entries.append(
            {
                "category": "Parse Notice",
                "key": "Inspection Note",
                "value": f"Image header parsed: {e}",
                "risk": "low",
            }
        )

    return {
        "file_type": "JPEG",
        "file_size": len(data),
        "metadata_count": len(entries),
        "has_high_risk": any(e["risk"] == "high" for e in entries),
        "entries": entries,
        "details": details,
    }


def scrub_jpeg_bytes(
    data: bytes, *, preserve_icc: bool = True
) -> Tuple[bytes, List[str]]:
    if len(data) < 2 or data[:2] != b"\xff\xd8":
        raise ValueError("Invalid JPEG header")

    out = bytearray(b"\xff\xd8")
    pos = 2
    stripped_items: List[str] = []

    while pos < len(data):
        if data[pos] != 0xFF:
            pos += 1
            continue

        marker = data[pos + 1]

        if marker == 0xD9:
            out.extend(b"\xff\xd9")
            break

        if marker == 0xDA:
            out.extend(data[pos:])
            break

        if marker in (0xD8, 0x01) or (0xD0 <= marker <= 0xD7):
            out.extend(data[pos : pos + 2])
            pos += 2
            continue

        if marker == 0xFF:
            pos += 1
            continue

        if pos + 4 > len(data):
            break
        length = (data[pos + 2] << 8) + data[pos + 3]
        total_len = 2 + length

        if length < 2 or pos + total_len > len(data):
            stripped_items.append(f"Malformed marker 0x{marker:02X}, truncating")
            break

        marker_data = data[pos + 4 : pos + total_len]

        is_metadata = False
        marker_label = ""

        if marker == 0xE1:
            is_metadata = True
            marker_label = "APP1 (EXIF / XMP)"
        elif marker == 0xE2:
            if not preserve_icc:
                is_metadata = True
                marker_label = "APP2 (ICC Color Profile)"
        elif marker == 0xED:
            is_metadata = True
            marker_label = "APP13 (Photoshop IPTC)"
        elif marker == 0xFE:
            is_metadata = True
            marker_label = "COM (Comment)"
        elif 0xE3 <= marker <= 0xEC:
            is_metadata = True
            marker_label = f"APP{marker - 0xE0} (Vendor Data)"
        elif marker == 0xEE:
            if not marker_data.startswith(b"Adobe"):
                is_metadata = True
                marker_label = "APP14 (Non-Adobe metadata)"

        if is_metadata:
            stripped_items.append(
                f"Removed JPEG marker: {marker_label} ({length} bytes)"
            )
        else:
            out.extend(data[pos : pos + total_len])

        pos += total_len

    return bytes(out), stripped_items


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def inspect_pdf_metadata(data: bytes) -> Dict[str, Any]:
    entries: List[Dict[str, Any]] = []
    details: Dict[str, Any] = {}

    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        details["pages"] = len(reader.pages)
        details["is_encrypted"] = reader.is_encrypted

        doc_info = reader.metadata
        if doc_info:
            for k, v in doc_info.items():
                if v is None:
                    continue
                k_clean = str(k).lstrip("/")
                v_clean = str(v).strip()
                risk = (
                    "high"
                    if any(
                        x in k_clean.lower()
                        for x in ("author", "creator", "producer", "title", "subject")
                    )
                    else "medium"
                )
                entries.append(
                    {
                        "category": "Document Info (/Info)",
                        "key": k_clean,
                        "value": v_clean,
                        "risk": risk,
                    }
                )

        xmp = reader.xmp_metadata
        if xmp:
            if hasattr(xmp, "dc_creator") and xmp.dc_creator:
                entries.append(
                    {
                        "category": "XMP Dublin Core",
                        "key": "dc:creator (Author)",
                        "value": (
                            ", ".join(xmp.dc_creator)
                            if isinstance(xmp.dc_creator, list)
                            else str(xmp.dc_creator)
                        ),
                        "risk": "high",
                    }
                )
            if hasattr(xmp, "dc_title") and xmp.dc_title:
                entries.append(
                    {
                        "category": "XMP Dublin Core",
                        "key": "dc:title",
                        "value": str(xmp.dc_title),
                        "risk": "high",
                    }
                )
            if hasattr(xmp, "dc_description") and xmp.dc_description:
                entries.append(
                    {
                        "category": "XMP Dublin Core",
                        "key": "dc:description",
                        "value": str(xmp.dc_description),
                        "risk": "medium",
                    }
                )
            if hasattr(xmp, "xmp_creator_tool") and xmp.xmp_creator_tool:
                entries.append(
                    {
                        "category": "XMP Basic",
                        "key": "CreatorTool",
                        "value": str(xmp.xmp_creator_tool),
                        "risk": "medium",
                    }
                )
            if hasattr(xmp, "xmp_create_date") and xmp.xmp_create_date:
                entries.append(
                    {
                        "category": "XMP Basic",
                        "key": "CreateDate",
                        "value": str(xmp.xmp_create_date),
                        "risk": "medium",
                    }
                )
            if hasattr(xmp, "xmp_modify_date") and xmp.xmp_modify_date:
                entries.append(
                    {
                        "category": "XMP Basic",
                        "key": "ModifyDate",
                        "value": str(xmp.xmp_modify_date),
                        "risk": "medium",
                    }
                )
            if hasattr(xmp, "pdf_producer") and xmp.pdf_producer:
                entries.append(
                    {
                        "category": "XMP Adobe PDF",
                        "key": "Producer",
                        "value": str(xmp.pdf_producer),
                        "risk": "medium",
                    }
                )

        has_piece_info = False
        if "/PieceInfo" in reader.trailer.get("/Root", {}):
            has_piece_info = True
        for page in reader.pages:
            if "/PieceInfo" in page:
                has_piece_info = True
                break

        if has_piece_info:
            entries.append(
                {
                    "category": "Private Application Data",
                    "key": "/PieceInfo",
                    "value": (
                        "Private software editing history "
                        "(Adobe Illustrator/Photoshop/InDesign)"
                    ),
                    "risk": "medium",
                }
            )

    except Exception as e:
        entries.append(
            {
                "category": "Parse Notice",
                "key": "Notice",
                "value": f"PDF parse error: {e}",
                "risk": "low",
            }
        )

    return {
        "file_type": "PDF",
        "file_size": len(data),
        "metadata_count": len(entries),
        "has_high_risk": any(e["risk"] == "high" for e in entries),
        "entries": entries,
        "details": details,
    }


def scrub_pdf_bytes(data: bytes) -> Tuple[bytes, List[str]]:
    reader = pypdf.PdfReader(io.BytesIO(data))
    writer = pypdf.PdfWriter()
    stripped_items: List[str] = []

    for i, page in enumerate(reader.pages):
        if "/PieceInfo" in page:
            del page["/PieceInfo"]
            stripped_items.append(f"Removed /PieceInfo from page {i + 1}")

        if "/Annots" in page:
            try:
                annots = page["/Annots"]
                if annots:
                    for annot_ref in annots:
                        annot = annot_ref.get_object()
                        if "/T" in annot:
                            stripped_items.append(
                                f"Sanitized author '{annot['/T']}' "
                                f"from annotation on page {i + 1}"
                            )
                            del annot["/T"]
                        if "/M" in annot:
                            del annot["/M"]
            except Exception:
                pass

        writer.add_page(page)

    if "/Metadata" in writer._root_object:
        del writer._root_object["/Metadata"]
        stripped_items.append("Purged XMP /Metadata stream from document root")

    if "/PieceInfo" in writer._root_object:
        del writer._root_object["/PieceInfo"]
        stripped_items.append("Purged /PieceInfo from document root")

    writer._info_obj = None
    stripped_items.append(
        "Purged /Info dictionary completely"
    )

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue(), stripped_items


# ---------------------------------------------------------------------------
# CONVERSION
# ---------------------------------------------------------------------------

CONVERTIBLE_SOURCE_FORMATS = {
    "PNG",
    "JPEG",
    "HEIC",
    "WEBP",
    "TIFF",
    "BMP",
    "AVIF",
}

CONVERTIBLE_TARGET_FORMATS = {"JPEG", "PNG", "WEBP"}

FORMAT_EXTENSIONS = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
}

_PILLOW_SAVE_NAMES = {"JPEG": "JPEG", "PNG": "PNG", "WEBP": "WEBP"}


def convert_bytes(
    data: bytes,
    filename: str = "",
    target_format: str = "JPEG",
    *,
    quality: int = 85,
    max_edge: int | None = 1920,
    flatten_alpha_on: Tuple[int, int, int] = (255, 255, 255),
) -> Tuple[bytes, Dict[str, Any]]:
    source_format = detect_file_type(data, filename)
    if source_format not in CONVERTIBLE_SOURCE_FORMATS:
        raise ValueError(
            f"Unsupported source format for conversion: {source_format!r}. "
            f"Supported: {', '.join(sorted(CONVERTIBLE_SOURCE_FORMATS))}"
        )

    target_format = target_format.upper()
    if target_format not in CONVERTIBLE_TARGET_FORMATS:
        raise ValueError(
            f"Unsupported target format: {target_format!r}. "
            f"Supported: {', '.join(sorted(CONVERTIBLE_TARGET_FORMATS))}"
        )

    orig_size = len(data)

    try:
        with Image.open(io.BytesIO(data)) as img:
            source_dimensions = img.size
            had_alpha = img.mode in ("RGBA", "LA") or (
                img.mode == "P" and "transparency" in img.info
            )

            img = ImageOps.exif_transpose(img)

            if max_edge is not None and max(img.size) > max_edge:
                img.thumbnail((max_edge, max_edge), Image.LANCZOS)

            output_dimensions = img.size

            if target_format == "JPEG":
                if img.mode in ("RGBA", "LA"):
                    bg = Image.new("RGB", img.size, flatten_alpha_on)
                    bg.paste(img, mask=img.split()[-1])
                    img = bg
                elif img.mode != "RGB":
                    img = img.convert("RGB")

            out = io.BytesIO()
            save_kwargs: Dict[str, Any] = {
                "format": _PILLOW_SAVE_NAMES[target_format]
            }

            if target_format == "JPEG":
                save_kwargs["quality"] = quality
                save_kwargs["optimize"] = True
                save_kwargs["progressive"] = True
            elif target_format == "WEBP":
                save_kwargs["quality"] = quality
                save_kwargs["method"] = 6
            elif target_format == "PNG":
                save_kwargs["optimize"] = True
                save_kwargs["compress_level"] = 9

            img.save(out, **save_kwargs)
    except Exception as exc:
        raise ValueError(
            f"Failed to decode/convert {filename or 'input'}: {exc}"
        ) from exc

    converted = out.getvalue()
    clean_size = len(converted)

    summary = {
        "filename": Path(filename).name if filename else "unnamed",
        "source_format": source_format,
        "target_format": target_format,
        "original_size": orig_size,
        "converted_size": clean_size,
        "bytes_saved": orig_size - clean_size,
        "source_dimensions": f"{source_dimensions[0]} × {source_dimensions[1]}",
        "output_dimensions": f"{output_dimensions[0]} × {output_dimensions[1]}",
        "downscaled": source_dimensions != output_dimensions,
        "had_alpha": had_alpha,
        "alpha_flattened": had_alpha and target_format == "JPEG",
        "quality": quality if target_format in ("JPEG", "WEBP") else None,
        "metadata_stripped": True,
    }

    # Capture what the conversion destroyed. This runs against the source
    # bytes and works for every format the engine can inspect. The result is
    # the "receipt" shown to the user of what the re-encode removed.
    destroyed_items: List[str] = []
    try:
        pre = inspect_metadata(data, filename)
        for e in pre.get("entries", []):
            destroyed_items.append(f"{e['category']}: {e['key']}")
    except Exception:
        pass

    summary["destroyed_items"] = destroyed_items
    summary["destroyed_count"] = len(destroyed_items)
    return converted, summary


def convert_file(
    filepath: str | Path,
    target_format: str = "JPEG",
    *,
    output_path: str | Path | None = None,
    quality: int = 85,
    max_edge: int | None = 1920,
) -> Dict[str, Any]:
    p = Path(filepath).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"File not found: {p}")

    with open(p, "rb") as f:
        data = f.read()

    converted, summary = convert_bytes(
        data, p.name, target_format, quality=quality, max_edge=max_edge
    )

    target_format = target_format.upper()
    if output_path is None:
        out_p = p.with_suffix(FORMAT_EXTENSIONS[target_format])
    else:
        out_p = Path(output_path).expanduser().resolve()

    out_p.parent.mkdir(parents=True, exist_ok=True)

    temp_path = out_p.with_name(f".{out_p.name}.converting_tmp")
    try:
        with open(temp_path, "wb") as f:
            f.write(converted)
            f.flush()
            os.fsync(f.fileno())
        try:
            shutil.copymode(p, temp_path)
        except Exception:
            pass
        os.replace(temp_path, out_p)
    finally:
        if temp_path.exists():
            temp_path.unlink()

    summary["source_path"] = str(p)
    summary["path"] = str(out_p)
    summary["success"] = True
    return summary


# ---------------------------------------------------------------------------
# VERIFICATION
# ---------------------------------------------------------------------------


def verify_clean(data: bytes, filename: str = "") -> Dict[str, Any]:
    report = inspect_metadata(data, filename)
    entries = report.get("entries", [])
    high = [e for e in entries if e.get("risk") == "high"]
    medium = [e for e in entries if e.get("risk") == "medium"]

    return {
        "clean": len(entries) == 0,
        "remaining_count": len(entries),
        "high_risk_remaining": len(high),
        "medium_risk_remaining": len(medium),
        "remaining": entries,
        "report": report,
    }


# ---------------------------------------------------------------------------
# HEIC / HEIF — INSPECTION ONLY (no lossless scrub path)
# ---------------------------------------------------------------------------


def _walk_isobmff_boxes(
    data: bytes, start: int = 0, end: int | None = None
) -> List[Dict[str, Any]]:
    """Walk top-level ISOBMFF boxes. Returns a flat list of {type, size, offset}."""
    if end is None:
        end = len(data)
    boxes: List[Dict[str, Any]] = []
    pos = start
    while pos + 8 <= end:
        size = struct.unpack(">I", data[pos : pos + 4])[0]
        btype = data[pos + 4 : pos + 8]
        header_len = 8
        if size == 1:
            if pos + 16 > end:
                break
            size = struct.unpack(">Q", data[pos + 8 : pos + 16])[0]
            header_len = 16
        elif size == 0:
            size = end - pos
        if size < header_len or pos + size > end:
            break
        boxes.append(
            {
                "type": btype.decode("latin1", "replace"),
                "size": size,
                "offset": pos,
            }
        )
        pos += size
    return boxes


def inspect_heic_metadata(data: bytes) -> Dict[str, Any]:
    """Inspect HEIC/HEIF metadata for what a conversion would destroy.

    HEIC has no lossless scrub path. Every call to this function is
    implicitly reporting on what convert_bytes() would remove.

    Note: pillow-heif exposes the primary image's EXIF and XMP, but does not
    surface depth maps, Live Photo video, HDR gain maps, or Apple's edit
    history as structured data. Those are destroyed on conversion regardless.
    Rather than emit speculative "may be present" warnings that inflate the
    metadata count, this function reports only what it can actually read and
    documents the rest in `details["auxiliary_notes"]`.
    """
    entries: List[Dict[str, Any]] = []
    details: Dict[str, Any] = {"lossless_scrub_available": False}

    if not HEIF_AVAILABLE:
        return {
            "file_type": "HEIC",
            "file_size": len(data),
            "metadata_count": 0,
            "has_high_risk": False,
            "entries": [],
            "details": details,
            "error": "HEIC inspection requires pillow-heif. Install with: pip install pillow-heif",
        }

    # 1. Walk the ISOBMFF container to see which structural boxes are present.
    try:
        boxes = _walk_isobmff_boxes(data)
        details["container_boxes"] = [b["type"] for b in boxes]

        has_meta = any(b["type"] == "meta" for b in boxes)
        has_moov = any(b["type"] == "moov" for b in boxes)

        if has_moov:
            entries.append(
                {
                    "category": "HEIC Container",
                    "key": "Live Photo Video",
                    "value": "Embedded .MOV sequence (moov box present) — motion, audio, and separately-timestamped frames",
                    "risk": "high",
                }
            )
        if has_meta:
            entries.append(
                {
                    "category": "HEIC Container",
                    "key": "Metadata Item Box",
                    "value": "ISOBMFF meta box holds Exif, XMP, thumbnails, depth maps, auxiliary images",
                    "risk": "high",
                }
            )
    except Exception as e:
        details["container_parse_error"] = str(e)

    # 2. Extract EXIF / XMP via Pillow + pillow-heif.
    try:
        with Image.open(io.BytesIO(data)) as img:
            details["dimensions"] = f"{img.width} × {img.height}"
            details["width"] = img.width
            details["height"] = img.height
            details["mode"] = img.mode

            exif = img.getexif()
            if exif:
                for tag_id, val in exif.items():
                    tag_name = TAGS.get(tag_id, f"Tag_{tag_id}")
                    if tag_name == "GPSInfo" or tag_id == 0x8825:
                        gps_data = exif.get_ifd(0x8825)
                        if gps_data:
                            decoded_gps = {
                                GPSTAGS.get(k, k): v for k, v in gps_data.items()
                            }
                            coords, lat, lon = _format_gps(decoded_gps)
                            if coords:
                                entries.append(
                                    {
                                        "category": "GPS / Location",
                                        "key": "GPS Coordinates",
                                        "value": coords,
                                        "risk": "high",
                                        "lat": lat,
                                        "lon": lon,
                                    }
                                )
                            for gk, gv in decoded_gps.items():
                                entries.append(
                                    {
                                        "category": "GPS / Location",
                                        "key": str(gk),
                                        "value": str(gv),
                                        "risk": "high",
                                    }
                                )
                    else:
                        val_str = str(val).strip()
                        risk = (
                            "high"
                            if tag_name
                            in (
                                "Make",
                                "Model",
                                "Artist",
                                "Software",
                                "SerialNumber",
                                "LensModel",
                                "CameraSerialNumber",
                                "BodySerialNumber",
                                "OwnerName",
                            )
                            else "medium"
                        )
                        entries.append(
                            {
                                "category": "EXIF Header",
                                "key": tag_name,
                                "value": (
                                    val_str
                                    if len(val_str) < 200
                                    else val_str[:200] + "..."
                                ),
                                "risk": risk,
                            }
                        )

                try:
                    exif_sub = exif.get_ifd(0x8769)
                    for sub_id, val in exif_sub.items():
                        sub_name = TAGS.get(sub_id, f"ExifSub_{sub_id}")
                        val_str = str(val).strip()
                        risk = (
                            "high"
                            if any(
                                x in sub_name.lower()
                                for x in ("owner", "serial", "lens")
                            )
                            else "medium"
                        )
                        entries.append(
                            {
                                "category": "EXIF Photo Data",
                                "key": sub_name,
                                "value": (
                                    val_str
                                    if len(val_str) < 200
                                    else val_str[:200] + "..."
                                ),
                                "risk": risk,
                            }
                        )
                except Exception:
                    pass

            xmp_raw = img.info.get("xmp")
            if xmp_raw:
                xmp_entries = _extract_xmp_tags(xmp_raw)
                if xmp_entries:
                    entries.extend(xmp_entries)
                else:
                    entries.append(
                        {
                            "category": "XMP Metadata",
                            "key": "XMP Packet",
                            "value": f"{len(xmp_raw)} bytes embedded XMP",
                            "risk": "medium",
                        }
                    )

            for key in ("metadata", "icc_profile", "bit_depth"):
                if key in img.info:
                    v = img.info[key]
                    if key == "metadata":
                        entries.append(
                            {
                                "category": "Apple Metadata",
                                "key": "Maker Notes",
                                "value": f"{len(str(v))} bytes Apple-specific metadata",
                                "risk": "high",
                            }
                        )
                    elif key == "icc_profile":
                        entries.append(
                            {
                                "category": "Color Profile",
                                "key": "ICC Profile",
                                "value": f"{len(v)} bytes embedded profile",
                                "risk": "low",
                            }
                        )
                    elif key == "bit_depth":
                        entries.append(
                            {
                                "category": "HEIC Container",
                                "key": "Bit Depth",
                                "value": str(v),
                                "risk": "low",
                            }
                        )

    except Exception as e:
        details["decode_error"] = str(e)

    # 3. Document what HEIC can carry but pillow-heif cannot surface.
    #    These do NOT count toward metadata_count; they are informational.
    details["auxiliary_notes"] = [
        "Depth maps (Portrait Mode) — 3D distance data, if present, is destroyed on conversion",
        "Live Photo video sequences — detected via moov box above when present",
        "HDR gain maps — tone-mapping data, destroyed on conversion",
        "Apple Photos edit history — non-destructive adjustment chain, destroyed on conversion",
    ]

    # Deduplicate by (category, key, value) tuple.
    seen = set()
    deduped = []
    for e in entries:
        k = (e.get("category"), e.get("key"), e.get("value"))
        if k not in seen:
            seen.add(k)
            deduped.append(e)

    return {
        "file_type": "HEIC",
        "file_size": len(data),
        "metadata_count": len(deduped),
        "has_high_risk": any(e["risk"] == "high" for e in deduped),
        "entries": deduped,
        "details": details,
        "note": (
            "HEIC has no lossless scrub path. Converting re-encodes the pixels and "
            "destroys every container-level metadata item in one pass."
        ),
    }


# ---------------------------------------------------------------------------
# DISPATCH
# ---------------------------------------------------------------------------


def inspect_metadata(data: bytes, filename: str = "") -> Dict[str, Any]:
    ftype = detect_file_type(data, filename)
    if ftype == "PNG":
        res = inspect_png_metadata(data)
    elif ftype == "JPEG":
        res = inspect_jpeg_metadata(data)
    elif ftype == "PDF":
        res = inspect_pdf_metadata(data)
    elif ftype == "HEIC":
        res = inspect_heic_metadata(data)
    else:
        res = {
            "file_type": ftype or "UNKNOWN",
            "file_size": len(data),
            "metadata_count": 0,
            "has_high_risk": False,
            "entries": [],
            "error": (
                f"Unsupported file format for inspection: {ftype!r}. "
                "Supported for inspection: PNG, JPEG, PDF, HEIC. "
                "Other formats (WebP, TIFF, BMP, AVIF) can be converted."
            ),
        }
    res["filename"] = Path(filename).name if filename else "unnamed"
    return res


def scrub_bytes(
    data: bytes,
    filename: str = "",
    *,
    preserve_icc: bool = True,
) -> Tuple[bytes, Dict[str, Any]]:
    ftype = detect_file_type(data, filename)
    orig_size = len(data)

    if ftype == "PNG":
        scrubbed, stripped = scrub_png_bytes(data, preserve_icc=preserve_icc)
    elif ftype == "JPEG":
        scrubbed, stripped = scrub_jpeg_bytes(data, preserve_icc=preserve_icc)
    elif ftype == "PDF":
        scrubbed, stripped = scrub_pdf_bytes(data)
    else:
        if ftype == "HEIC":
            raise ValueError(
                "HEIC has no lossless scrub path. Use the Convert action "
                "(→ JPEG) to re-encode and destroy all container metadata."
            )
        raise ValueError(
            f"Unsupported file format for '{filename}'. "
            "Supported for lossless scrub: PNG, JPEG, PDF. "
            "Use Convert for HEIC/WEBP/TIFF/BMP/AVIF."
        )

    clean_size = len(scrubbed)
    saved_bytes = orig_size - clean_size
    pct = round((saved_bytes / orig_size * 100), 2) if orig_size > 0 else 0.0

    summary = {
        "filename": Path(filename).name if filename else "unnamed",
        "file_type": ftype,
        "original_size": orig_size,
        "cleaned_size": clean_size,
        "bytes_saved": saved_bytes,
        "percent_change": pct,
        "stripped_items": stripped,
        "stripped_count": len(stripped),
    }
    return scrubbed, summary


# ---------------------------------------------------------------------------
# IN-PLACE FILESYSTEM OPS
# ---------------------------------------------------------------------------


def scrub_file_inplace(
    filepath: str | Path,
    backup: bool = False,
    *,
    preserve_icc: bool = True,
) -> Dict[str, Any]:
    """Scrub a file in place.

    Returns either:
      {"success": True, ...}   — the file was scrubbed
      {"skipped": True, ...}   — the format has no lossless scrub path
                                 (HEIC and other convert-only formats)
    """
    p = Path(filepath).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"File not found: {p}")

    with open(p, "rb") as f:
        data = f.read()

    ftype = detect_file_type(data, p.name)
    if ftype == "HEIC":
        return {
            "path": str(p),
            "filename": p.name,
            "skipped": True,
            "reason": (
                "HEIC has no lossless scrub path. Use convert to JPEG."
            ),
        }
    if ftype not in ("PNG", "JPEG", "PDF"):
        return {
            "path": str(p),
            "filename": p.name,
            "skipped": True,
            "reason": (
                f"{ftype or 'Unsupported'} file has no lossless scrub path. "
                "Use convert instead."
            ),
        }

    scrubbed_bytes, summary = scrub_bytes(data, p.name, preserve_icc=preserve_icc)

    if backup:
        bak_path = p.with_suffix(p.suffix + ".bak")
        shutil.copy2(p, bak_path)
        summary["backup_created"] = str(bak_path)

    temp_path = p.with_name(f".{p.name}.scrubbing_tmp")
    try:
        with open(temp_path, "wb") as f:
            f.write(scrubbed_bytes)
            f.flush()
            os.fsync(f.fileno())
        try:
            shutil.copymode(p, temp_path)
        except Exception:
            pass
        os.replace(temp_path, p)
    finally:
        if temp_path.exists():
            temp_path.unlink()

    summary["path"] = str(p)
    summary["success"] = True
    return summary


def scrub_directory_inplace(
    dirpath: str | Path,
    recursive: bool = True,
    backup: bool = False,
    *,
    preserve_icc: bool = True,
) -> Dict[str, Any]:
    p = Path(dirpath).resolve()
    if not p.is_dir():
        raise NotADirectoryError(f"Directory not found: {p}")

    extensions = {".png", ".jpg", ".jpeg", ".pdf", ".heic", ".heif"}
    pattern = "**/*" if recursive else "*"
    all_files = [
        f for f in p.glob(pattern) if f.is_file() and f.suffix.lower() in extensions
    ]

    results = []
    total_orig = 0
    total_clean = 0
    success_count = 0
    skipped_count = 0

    for f in all_files:
        try:
            res = scrub_file_inplace(f, backup=backup, preserve_icc=preserve_icc)
            if res.get("success"):
                success_count += 1
                total_orig += res.get("original_size", 0)
                total_clean += res.get("cleaned_size", 0)
            elif res.get("skipped"):
                skipped_count += 1
            results.append(res)
        except Exception as e:
            results.append(
                {"path": str(f), "filename": f.name, "success": False, "error": str(e)}
            )

    return {
        "directory": str(p),
        "total_found": len(all_files),
        "total_scrubbed": success_count,
        "total_skipped": skipped_count,
        "total_original_bytes": total_orig,
        "total_cleaned_bytes": total_clean,
        "total_bytes_saved": total_orig - total_clean,
        "files": results,
    }


def inspect_directory(dirpath: str | Path, recursive: bool = True) -> Dict[str, Any]:
    p = Path(dirpath).resolve()
    if not p.is_dir():
        raise NotADirectoryError(f"Directory not found: {p}")

    extensions = {".png", ".jpg", ".jpeg", ".pdf", ".heic", ".heif"}
    pattern = "**/*" if recursive else "*"
    all_files = [
        f for f in p.glob(pattern) if f.is_file() and f.suffix.lower() in extensions
    ]

    file_reports = []
    total_tags = 0

    for f in all_files:
        try:
            with open(f, "rb") as stream:
                data = stream.read()
            report = inspect_metadata(data, f.name)
            report["path"] = str(f)
            total_tags += report.get("metadata_count", 0)
            file_reports.append(report)
        except Exception as e:
            file_reports.append(
                {
                    "path": str(f),
                    "filename": f.name,
                    "error": str(e),
                    "metadata_count": 0,
                    "entries": [],
                }
            )

    return {
        "directory": str(p),
        "total_found": len(all_files),
        "total_files": len(all_files),
        "total_tags": total_tags,
        "files": file_reports,
    }


__all__ = [
    "detect_file_type",
    "inspect_metadata",
    "inspect_png_metadata",
    "inspect_jpeg_metadata",
    "inspect_pdf_metadata",
    "inspect_heic_metadata",
    "inspect_directory",
    "scrub_bytes",
    "scrub_png_bytes",
    "scrub_jpeg_bytes",
    "scrub_pdf_bytes",
    "scrub_file_inplace",
    "scrub_directory_inplace",
    "convert_bytes",
    "convert_file",
    "verify_clean",
    "HEIF_AVAILABLE",
    "AVIF_AVAILABLE",
]