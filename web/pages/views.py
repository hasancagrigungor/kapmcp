"""Pages are rendered from the MCP server's own registry, so the documentation can never
drift from the tools actually served."""

from __future__ import annotations

import asyncio
import json
from functools import lru_cache
from typing import Any

from django.conf import settings
from django.http import Http404, JsonResponse
from django.shortcuts import render

from kap_mcp import __version__, market
from kap_mcp import server as kap_server
from kap_mcp.market import run_blocking

GROUPS = [
    ("Referans", ["kap_status", "get_reference_codes"]),
    ("Şirketler (KAP)", ["search_companies", "get_company"]),
    ("Bildirimler (KAP)", ["search_disclosures", "get_disclosure", "search_disclosure_data", "get_blocked_disclosures"]),
    ("Ek dosyalar (KAP)", ["get_disclosure_documents", "get_document_text", "search_documents"]),
    ("Finansallar (KAP filing + Yahoo)", ["get_financials", "get_metric_history", "get_financial_ratios", "get_growth", "compare_financials"]),
    ("Kurumsal aksiyonlar", ["get_corporate_actions"]),
    ("Haberler (Yahoo)", ["get_company_news"]),
    ("Piyasa verisi (Yahoo)", ["get_quote", "get_price_history", "get_market_overview", "get_price_reaction", "get_analyst_estimates"]),
    ("Zaman çizelgesi", ["get_company_timeline"]),
    ("Fonlar (KAP)", ["search_funds"]),
]


def _schema_type(prop: dict[str, Any]) -> str:
    if "enum" in prop:
        return "enum: " + " | ".join(str(x) for x in prop["enum"])
    if "anyOf" in prop:
        parts = [_schema_type(p) for p in prop["anyOf"] if p.get("type") != "null"]
        return " | ".join(dict.fromkeys(parts))
    t = prop.get("type", "")
    if t == "array":
        return "array<" + _schema_type(prop.get("items", {})) + ">"
    return t or "object"


@lru_cache(maxsize=1)
def tool_catalog() -> dict[str, dict[str, Any]]:
    tools = asyncio.run(kap_server.mcp.list_tools())
    out: dict[str, dict[str, Any]] = {}
    for t in tools:
        schema = t.input_schema or {}
        required = set(schema.get("required") or [])
        params = []
        for name, prop in (schema.get("properties") or {}).items():
            params.append({
                "name": name, "type": _schema_type(prop), "required": name in required,
                "default": json.dumps(prop["default"], ensure_ascii=False) if "default" in prop and prop["default"] is not None else None,
                "description": prop.get("description") or "",
                "constraints": ", ".join(f"{k}={v}" for k, v in prop.items() if k in ("minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems")),
            })
        summary, _, rest = (t.description or "").strip().partition("\n")
        out[t.name] = {
            "name": t.name, "title": t.title or t.name, "summary": summary.strip(), "description": (t.description or "").strip(),
            "params": params, "input_schema": json.dumps(schema, ensure_ascii=False, indent=2),
            "output_schema": json.dumps(t.output_schema, ensure_ascii=False, indent=2) if t.output_schema else None,
            "read_only": bool(t.annotations and getattr(t.annotations, "read_only_hint", getattr(t.annotations, "readOnlyHint", False))),
        }
    return out


def grouped_tools() -> list[dict[str, Any]]:
    cat = tool_catalog()
    seen = set()
    groups = []
    for label, names in GROUPS:
        items = [cat[n] for n in names if n in cat]
        seen.update(names)
        if items:
            groups.append({"label": label, "tools": items})
    rest = [cat[n] for n in cat if n not in seen]
    if rest:
        groups.append({"label": "Diğer", "tools": rest})
    return groups


def index(request):
    cat = tool_catalog()
    return render(request, "index.html", {"version": __version__, "groups": grouped_tools(), "tool_count": len(cat),
                                          "kap_env": "test" if kap_server.settings.kap_test_mode else "production",
                                          "kap_configured": kap_server.settings.kap_configured})


def tools(request):
    return render(request, "tools.html", {"groups": grouped_tools(), "tool_count": len(tool_catalog())})


def tool_detail(request, name: str):
    tool = tool_catalog().get(name)
    if not tool:
        raise Http404
    return render(request, "tool.html", {"tool": tool})


async def _status() -> dict[str, Any]:
    out: dict[str, Any] = {"version": __version__, "kap_configured": kap_server.settings.kap_configured,
                           "kap_environment": "test" if kap_server.settings.kap_test_mode else "production"}
    try:
        client = await kap_server.get_client()
        out["kap_last_disclosure_id"] = await client.get_last_disclosure_index()
        out["kap_ok"] = True
    except Exception as e:  # noqa: BLE001
        out["kap_ok"] = False
        out["kap_error"] = str(e)[:300]
    try:
        q = await run_blocking(market.get_quote, "XU100", timeout=15)
        out["yahoo_ok"] = True
        out["xu100"] = q["lastPrice"]
    except Exception as e:  # noqa: BLE001
        out["yahoo_ok"] = False
        out["yahoo_error"] = str(e)[:300]
    return out


async def status(request):
    data = await _status()
    if request.GET.get("format") == "json" or request.headers.get("accept", "").startswith("application/json"):
        return JsonResponse(data)
    return render(request, "status.html", {"s": data})


def healthz(request):
    return JsonResponse({"ok": True, "version": __version__})


def privacy(request):
    return render(request, "privacy.html")
