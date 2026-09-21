"""
Attachment (document) access for KAP disclosures — text only, in memory.

Nothing is written to disk and no arbitrary URLs are fetched: attachments are
addressed by the id KAP returns and downloaded through the authenticated KAP
client. Extracted text is cached per attachment so repeated searches are free.
"""

from __future__ import annotations

import asyncio
import io
import re
from typing import Any, Optional

from ..cache import TTLCache
from ..client import KAPClient, normalize_tr
from ..text import clip, html_to_text

_text_cache = TTLCache(max_entries=256)
MAX_ATTACHMENT_BYTES = 40 * 1024 * 1024


def _pdf_pages(data: bytes) -> list[str]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages:
        try:
            pages.append((page.extract_text() or "").strip())
        except Exception:  # noqa: BLE001 — a corrupt page shouldn't kill the document
            pages.append("")
    return pages


def _kind(content_type: Optional[str], name: Optional[str], head: bytes) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    n = (name or "").lower()
    if ct == "application/pdf" or n.endswith(".pdf") or head.startswith(b"%PDF-"):
        return "pdf"
    if ct.startswith("text/html") or n.endswith((".htm", ".html")):
        return "html"
    if ct.startswith("text/") or n.endswith((".txt", ".csv")):
        return "text"
    return "binary"


async def load_document(client: KAPClient, attachment_id: str) -> dict[str, Any]:
    """Download + extract once; returns {"kind", "pages": [str], "content_type", "name", "bytes"}."""
    key = f"doc:{attachment_id}"
    hit = _text_cache.get(key)
    if hit is not None:
        return hit
    content, content_type, name = await client.download_attachment(attachment_id)
    if len(content) > MAX_ATTACHMENT_BYTES:
        doc = {"kind": "too_large", "pages": [], "content_type": content_type, "name": name, "bytes": len(content)}
        _text_cache.set(key, doc, 3600)
        return doc
    kind = _kind(content_type, name, content[:8])
    if kind == "pdf":
        pages = await asyncio.to_thread(_pdf_pages, content)
    elif kind == "html":
        pages = [html_to_text(content.decode("utf-8", errors="replace"))]
    elif kind == "text":
        pages = [content.decode("utf-8", errors="replace")]
    else:
        pages = []
    doc = {"kind": kind, "pages": pages, "content_type": content_type, "name": name, "bytes": len(content)}
    _text_cache.set(key, doc, 6 * 3600)
    return doc


def parse_page_range(spec: Optional[str], total: int) -> list[int]:
    """'1-3,7' -> [0,1,2,6] (0-based, clipped). None -> all."""
    if not spec:
        return list(range(total))
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            lo = max(int(a or 1), 1)
            hi = min(int(b or total), total)
            out.update(range(lo - 1, hi))
        else:
            n = int(part)
            if 1 <= n <= total:
                out.add(n - 1)
    return sorted(out)


def document_text(doc: dict[str, Any], pages: Optional[str], max_chars: int) -> dict[str, Any]:
    total = len(doc["pages"])
    if doc["kind"] in ("binary", "too_large"):
        return {"kind": doc["kind"], "pages_total": total, "text": None,
                "reason": "binary attachment (xlsx/zip/image) is not converted to text" if doc["kind"] == "binary"
                else f"attachment larger than {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB"}
    idxs = parse_page_range(pages, total)
    chunks = []
    for i in idxs:
        t = doc["pages"][i]
        if t:
            chunks.append(f"--- page {i + 1} ---\n{t}" if doc["kind"] == "pdf" else t)
    text, truncated = clip("\n\n".join(chunks), max_chars)
    out = {"kind": doc["kind"], "pages_total": total, "pages_returned": [i + 1 for i in idxs], "text": text, "truncated": truncated}
    if doc["kind"] == "pdf" and total and not any(doc["pages"]):
        out["reason"] = "no extractable text (likely a scanned/image-only PDF)"
    return out


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;:])\s+|\n+")


def search_document(doc: dict[str, Any], query: str, *, context_chars: int = 300, max_hits: int = 10) -> list[dict[str, Any]]:
    """Return small snippets around each match (page-aware). Turkish-insensitive substring match."""
    terms = [normalize_tr(t) for t in re.split(r"\s+", query.strip()) if t.strip()]
    if not terms:
        return []
    hits: list[dict[str, Any]] = []
    for page_no, page in enumerate(doc["pages"], 1):
        if not page:
            continue
        norm = normalize_tr(page)
        # normalize_tr is length-preserving except for .strip(); search on the stripped page.
        stripped = page.strip()
        norm = normalize_tr(stripped)
        pos = 0
        while len(hits) < max_hits:
            found = [(norm.find(t, pos), t) for t in terms]
            found = [(p, t) for p, t in found if p >= 0]
            if not found:
                break
            p, t = min(found)
            start = max(0, p - context_chars // 2)
            end = min(len(stripped), p + len(t) + context_chars // 2)
            snippet = stripped[start:end].replace("\n", " ").strip()
            hits.append({"page": page_no, "text": ("…" if start > 0 else "") + snippet + ("…" if end < len(stripped) else ""),
                         "matched_term": t})
            pos = end
        if len(hits) >= max_hits:
            break
    return hits
