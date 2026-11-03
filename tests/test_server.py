"""Offline tests: the server builds, lists its tools, and tool wrappers shape SDK output.

No network. The TouAI client is replaced by simple stand-ins.
"""

from __future__ import annotations

import types
from decimal import Decimal

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from touai_mcp import server as srv

EXPECTED_TOOLS = {
    "touai_project_services",
    "touai_balance",
    "touai_prices",
    "touai_web_scrape",
    "touai_web_search",
    "touai_web_crawl",
    "touai_web_crawl_status",
    "touai_research",
    "touai_research_status",
    "touai_research_cancel",
    "touai_parse_document",
    "touai_parse_status",
    "touai_document_chat",
    "touai_video_chat",
    "touai_kb_list",
    "touai_kb_create",
    "touai_kb_upload",
    "touai_kb_search",
    "touai_connections_list",
    "touai_connections_search",
    "touai_connection_query",
    "touai_storage_upload",
    "touai_storage_list",
    "touai_storage_download_url",
}


@pytest.fixture
def server():
    return srv.build_server()


async def test_tools_registered(server):
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert names == EXPECTED_TOOLS
    for t in tools:
        assert t.description and len(t.description) > 20, t.name
        assert t.annotations is not None, t.name


async def test_missing_credentials_is_a_tool_error(server, monkeypatch):
    monkeypatch.delenv("TOUAI_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("TOUAI_SECRET_KEY", raising=False)
    monkeypatch.setattr(srv, "_client", None)
    monkeypatch.delenv("TOUAI_BASE_URL", raising=False)
    with pytest.raises(ToolError, match="TOUAI_ACCESS_KEY_ID") as ei:
        await server.call_tool("touai_balance", {})
    assert "https://platform.tou.ai/" in str(ei.value)
    assert "API Keys" in str(ei.value)

    monkeypatch.setenv("TOUAI_BASE_URL", "https://example.test/")
    with pytest.raises(ToolError, match=r"https://example\.test/ ,"):
        await server.call_tool("touai_balance", {})


def _ns(**kw):
    return types.SimpleNamespace(**kw)


async def test_web_search_shapes_output(server, monkeypatch):
    fake = _ns(
        web_access=_ns(
            search=lambda q, limit=None, lang=None: _ns(
                query=q,
                lang="en",
                total_results=1,
                results=[_ns(position=1, title="T", url="https://x", description="d")],
            )
        )
    )
    monkeypatch.setattr(srv, "get_client", lambda: fake)
    res = await server.call_tool("touai_web_search", {"query": "hi", "limit": 3})
    data = _structured(res)
    assert data["results"][0]["url"] == "https://x"


async def test_touai_error_is_translated(server, monkeypatch):
    import touai

    def boom(*a, **k):
        raise touai.InsufficientCreditsError(
            "no credits",
            status_code=402,
            error_code="insufficient_credits",
            request_id="req_1",
            body={},
        )

    fake = _ns(web_access=_ns(search=boom))
    monkeypatch.setattr(srv, "get_client", lambda: fake)
    with pytest.raises(ToolError) as ei:
        await server.call_tool("touai_web_search", {"query": "hi"})
    text = str(ei.value)
    assert "insufficient_credits" in text and "req_1" in text and "top-up" in text


async def test_balance_decimal_to_float(server, monkeypatch):
    gate = _ns(
        requires_topup=False,
        balance=Decimal("12.50"),
        overdraft_limit=Decimal("0"),
        product_access=None,
    )

    def balance():
        raise PermissionError("not admin")

    fake = _ns(billing=_ns(credits=_ns(spendability=lambda: gate, balance=balance)))
    monkeypatch.setattr(srv, "get_client", lambda: fake)
    data = _structured(await server.call_tool("touai_balance", {}))
    assert data["balance"] == 12.5 and "note" in data


async def test_balance_wallet_dict_is_flattened(server, monkeypatch):
    """On the real API gate.balance is the wallet block; the tool exposes a number at the top."""
    gate = _ns(
        requires_topup=False,
        balance={"balance": "8480.92", "balance_display": "8480.92", "held_credits": 0},
        overdraft_limit=0,
        product_access=None,
    )

    def balance():
        raise PermissionError("not admin")

    fake = _ns(billing=_ns(credits=_ns(spendability=lambda: gate, balance=balance)))
    monkeypatch.setattr(srv, "get_client", lambda: fake)
    data = _structured(await server.call_tool("touai_balance", {}))
    assert data["balance"] == 8480.92
    assert data["balance_display"] == "8480.92"


async def test_placeholder_credentials_count_as_missing(server, monkeypatch):
    monkeypatch.setenv("TOUAI_ACCESS_KEY_ID", "ak_live_...")
    monkeypatch.setenv("TOUAI_SECRET_KEY", "sk_live_...")
    monkeypatch.delenv("TOUAI_BASE_URL", raising=False)
    monkeypatch.setattr(srv, "_client", None)
    with pytest.raises(ToolError, match="not configured"):
        await server.call_tool("touai_balance", {})


# --------------------------------------------------------------------------- helpers


def _text(res) -> str:
    if isinstance(res, tuple):
        res = res[0]
    content = getattr(res, "content", res)
    return " ".join(getattr(c, "text", "") for c in content)


def _structured(res):
    if isinstance(res, tuple):
        content, structured = res
        return structured.get("result", structured) if isinstance(structured, dict) else structured
    sc = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
    if sc is not None:
        return sc.get("result", sc)
    import json

    return json.loads(_text(res))
