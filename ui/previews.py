"""Thumbnails and previews for supplier attachments.

Presentation only: nothing here reads a document for extraction. The extracted text
already exists on the run; this just makes an attachment *look* like what it is, so a
buyer can see at a glance that the quotation arrived as a spreadsheet or a photograph.

Images render directly. Everything else is offered to macOS Quick Look, which already
knows how to render a PDF, a spreadsheet or a Word file and needs no new dependency.
Quick Look can hang, so it is run with a hard timeout and a fallback: if no thumbnail
arrives, the file's own extracted text is shown instead, which is honest about what the
system actually read.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Tuple

#: Quick Look occasionally stalls on an unusual file; never let that hang a page render.
THUMBNAIL_TIMEOUT_SECONDS = 8
THUMBNAIL_SIZE = 600

IMAGE_TYPES = {"image"}
QUICKLOOK_TYPES = {"pdf", "xlsx", "docx", "csv"}

ICONS = {"pdf": "📕", "xlsx": "📗", "csv": "📗", "docx": "📘", "image": "🖼️", "txt": "📄"}
TYPE_LABELS = {"pdf": "PDF", "xlsx": "Excel", "csv": "CSV", "docx": "Word",
               "image": "Image", "txt": "Text", "xls": "Excel (legacy)"}


def human_size(num_bytes: int) -> str:
    if not num_bytes:
        return ""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "%.0f %s" % (size, unit) if unit == "B" else "%.1f %s" % (size, unit)
        size /= 1024.0
    return ""


def type_label(media_type: str) -> str:
    return TYPE_LABELS.get(media_type, (media_type or "file").upper())


def icon(media_type: str) -> str:
    return ICONS.get(media_type, "📎")


# --------------------------------------------------------------------------- #
_cache: Dict[Tuple[str, float], Optional[str]] = {}


def thumbnail(path: str, media_type: str) -> Optional[str]:
    """A PNG path to show for this attachment, or None when we cannot render one.

    Images are their own thumbnail. Other formats go through Quick Look, cached per
    file so a page rerun does not re-render.
    """
    if not path or not os.path.exists(path):
        return None
    if media_type in IMAGE_TYPES:
        return path
    if media_type not in QUICKLOOK_TYPES:
        return None

    key = (path, os.path.getmtime(path))
    if key in _cache:
        return _cache[key]
    result = _quicklook(path)
    _cache[key] = result
    return result


def _quicklook(path: str) -> Optional[str]:
    """Ask macOS Quick Look for a thumbnail. Returns None on any failure."""
    out_dir = tempfile.mkdtemp(prefix="ql_thumb_")
    try:
        subprocess.run(
            ["qlmanage", "-t", "-s", str(THUMBNAIL_SIZE), "-o", out_dir, path],
            capture_output=True, timeout=THUMBNAIL_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    for name in os.listdir(out_dir):
        if name.lower().endswith(".png"):
            return os.path.join(out_dir, name)
    return None


# --------------------------------------------------------------------------- #
def spreadsheet_rows(path: str, limit: int = 12) -> Optional[List[List[str]]]:
    """First rows of a spreadsheet or CSV, for an inline table preview."""
    if not path or not os.path.exists(path):
        return None
    lower = path.lower()
    try:
        if lower.endswith((".xlsx", ".xlsm")):
            from openpyxl import load_workbook
            wb = load_workbook(path, data_only=True, read_only=True)
            ws = wb.worksheets[0]
            rows = []
            for row in ws.iter_rows(max_row=limit):
                rows.append(["" if c.value is None else str(c.value) for c in row])
            wb.close()
            return [r for r in rows if any(x.strip() for x in r)] or None
        if lower.endswith((".csv", ".tsv")):
            import csv as _csv
            with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
                sample = f.read(4096)
                f.seek(0)
                try:
                    dialect = _csv.Sniffer().sniff(sample, delimiters=",;\t|")
                except _csv.Error:
                    dialect = _csv.excel
                reader = _csv.reader(f, dialect)
                rows = [row for _, row in zip(range(limit), reader)]
            return [r for r in rows if any(str(x).strip() for x in r)] or None
    except Exception:
        return None
    return None
