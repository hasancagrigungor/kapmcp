"""Helpers that turn KAP payloads (HTML bodies, nested flatData/presentation JSON,
attachments) into compact plain text an LLM can read without wasting context."""

from __future__ import annotations

import html as _html
import re
from typing import Any, Iterable, Optional

_BLOCK_TAGS = re.compile(r"</?(p|div|br|tr|li|h[1-6]|table|ul|ol|section|article|blockquote)[^>]*>", re.I)
_CELL_CLOSE = re.compile(r"</(td|th)>", re.I)
_CELL_OPEN = re.compile(r"<(td|th)[^>]*>", re.I)
_TAGS = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANKS = re.compile(r"\n{3,}")


def html_to_text(raw: Optional[str]) -> str:
    """Strip tags but keep table/paragraph structure readable."""
    if not raw:
        return ""
    text = _SCRIPT.sub("", raw)
    text = _CELL_CLOSE.sub(" | ", text)
    text = _CELL_OPEN.sub("", text)
    text = _BLOCK_TAGS.sub("\n", text)
    text = _TAGS.sub("", text)
    text = _html.unescape(text)
    lines = [_WS.sub(" ", ln).strip(" |") for ln in text.split("\n")]
    text = "\n".join(ln for ln in lines if ln)
    return _BLANKS.sub("\n\n", text).strip()


def _label(node: dict[str, Any]) -> Optional[str]:
    """KAP nests bilingual labels as {"tr": ..., "en": ...}; prefer Turkish."""
    for key in ("tr", "label", "title", "name", "key", "desc", "description"):
        val = node.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def flatten(obj: Any, prefix: str = "") -> Iterable[tuple[str, str]]:
    """Yield (path, value) pairs for scalar leaves of an arbitrary JSON structure.

    Bilingual {"tr","en"} objects collapse to their Turkish text; list items are
    addressed by index unless they carry a recognisable label.
    """
    if isinstance(obj, dict):
        if set(obj) <= {"tr", "en"} and obj:
            val = obj.get("tr") or obj.get("en")
            if val not in (None, ""):
                yield prefix or "text", str(val)
            return
        for k, v in obj.items():
            yield from flatten(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            key = None
            if isinstance(item, dict):
                key = _label(item)
            sub = f"{prefix}[{key or i}]" if prefix else f"[{key or i}]"
            yield from flatten(item, sub)
    elif obj is None or obj == "":
        return
    else:
        yield prefix or "value", str(obj)


def flat_lines(obj: Any, max_lines: int = 0) -> list[str]:
    lines = [f"{k}: {v}" for k, v in flatten(obj)]
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines] + [f"...[{len(lines) - max_lines} more lines]"]
    return lines


def search_flat(obj: Any, query: str, normalize) -> list[dict[str, str]]:
    """Find leaves whose path or value contains `query` (normalised comparison)."""
    q = normalize(query)
    hits = []
    for k, v in flatten(obj):
        if q in normalize(k) or q in normalize(v):
            hits.append({"path": k, "value": v})
    return hits


def clip(text: str, max_chars: int) -> tuple[str, bool]:
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    return text[:max_chars] + f"\n...[truncated, {len(text) - max_chars} more chars]", True


def pdf_to_text(data: bytes, max_pages: int = 0) -> tuple[str, int]:
    """Extract text from a PDF. Returns (text, page_count). Requires pypdf."""
    import io

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = reader.pages
    total = len(pages)
    out = []
    for i, page in enumerate(pages):
        if max_pages and i >= max_pages:
            break
        try:
            txt = page.extract_text() or ""
        except Exception:  # noqa: BLE001 — a single bad page shouldn't kill the whole document
            txt = ""
        txt = txt.strip()
        if txt:
            out.append(f"--- sayfa {i + 1} ---\n{txt}")
    return "\n\n".join(out), total
