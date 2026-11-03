"""TouAI MCP server.

One process per MCP client, talking stdio. Every tool is a thin wrapper over the
official ``touai`` Python SDK; credentials come from the environment
(``TOUAI_ACCESS_KEY_ID`` / ``TOUAI_SECRET_KEY``, optional ``TOUAI_BASE_URL``) and
never pass through the protocol.

Tools return plain JSON-serialisable dicts. Platform errors are surfaced as tool
errors whose text carries the stable ``error_code`` and the ``request_id`` so the
agent can branch and the user can quote it to support.
"""

from __future__ import annotations

import os
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from ._version import __version__

# ----------------------------------------------------------------------------- client


_client = None
_client_lock = threading.Lock()


def get_client():
    """Lazily build one TouAI client per process."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                if not os.environ.get("TOUAI_ACCESS_KEY_ID") or not os.environ.get(
                    "TOUAI_SECRET_KEY"
                ):
                    raise ToolError(
                        "TouAI credentials are not configured. Set TOUAI_ACCESS_KEY_ID and "
                        "TOUAI_SECRET_KEY in the MCP server's environment (create a project API "
                        "key in the TouAI console under Project → API Keys)."
                    )
                from touai import TouAI

                _client = TouAI()
    return _client


def _touai_error(exc: Exception) -> ToolError:
    """Translate a touai exception into a ToolError the agent can act on."""
    try:
        from touai import TouAIError
    except ImportError:  # pragma: no cover
        return ToolError(str(exc))
    if isinstance(exc, TouAIError):
        parts = [exc.__class__.__name__]
        if getattr(exc, "status_code", None):
            parts.append(f"HTTP {exc.status_code}")
        if getattr(exc, "error_code", None):
            parts.append(f"error_code={exc.error_code}")
        msg = f"{' '.join(parts)}: {exc.message}"
        hint = _hint_for(exc)
        if hint:
            msg += f" — {hint}"
        if getattr(exc, "request_id", None):
            msg += f" (request_id={exc.request_id})"
        return ToolError(msg)
    return ToolError(f"{exc.__class__.__name__}: {exc}")


def _hint_for(exc: Any) -> str:
    import touai

    if isinstance(exc, touai.InsufficientCreditsError):
        return "the TouAI wallet needs a top-up (console only); nothing was charged"
    if isinstance(exc, touai.ServiceDisabledError):
        return f"product '{exc.service_type}' is switched off for this project; enable it in the console"
    if isinstance(exc, touai.ApiKeyRoleInsufficient):
        return f"this API key's role is {exc.api_key_role}, the call needs {exc.required_role}"
    if isinstance(exc, touai.AuthenticationError):
        return "check TOUAI_ACCESS_KEY_ID / TOUAI_SECRET_KEY"
    if isinstance(exc, touai.PollTimeoutError):
        return "the job is still running server-side; call the matching *_status tool later"
    if isinstance(exc, touai.RateLimitError):
        return f"retry after {exc.retry_after or 'a few'} seconds"
    return ""


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001 — translated below
        raise _touai_error(exc) from exc


def _num(v: Any) -> Any:
    return float(v) if isinstance(v, Decimal) else v


def _clip(text: str | None, limit: int) -> str:
    text = text or ""
    return (
        text
        if len(text) <= limit
        else text[:limit] + f"\n\n[... truncated, {len(text) - limit} more characters]"
    )


READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)
READ_ONLY_CLOSED = ToolAnnotations(read_only_hint=True, open_world_hint=False)
BILLABLE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)

INSTRUCTIONS = """TouAI is a data-infrastructure platform: web access, deep research, document/video
parsing, knowledge bases, connectors to databases and SaaS apps, object storage and billing.

Start with `touai_project_services` to learn which products this project may use and
`touai_balance` before expensive work. Billable tools say so in their description.
Prefer `touai_web_scrape` / `touai_web_search` for a few pages; use `touai_research`
only for a synthesised, cited report (minutes, tens of credits). Long jobs return a
job id when they time out — resume with the matching status tool, never re-submit.
"""


# ----------------------------------------------------------------------------- server


def build_server() -> MCPServer:
    server = MCPServer(name="touai", version=__version__, instructions=INSTRUCTIONS)

    # ------------------------------------------------------------------ project / billing

    @server.tool(
        name="touai_project_services",
        title="Which TouAI products this project may use",
        description=(
            "List every TouAI product with whether it is enabled for the API key's project and, "
            "if not, which layer closed it (project / organization / contract). Free. Call first."
        ),
        annotations=READ_ONLY_CLOSED,
    )
    def touai_project_services() -> dict[str, Any]:
        svcs = _run(get_client().project.services)
        return {
            "project_id": svcs.project_id,
            "project_name": svcs.project_name,
            "enabled": list(svcs.enabled),
            "disabled": [
                {"service_type": s.service_type, "reason": s.reason, "name": s.name}
                for s in svcs
                if not s.enabled
            ],
        }

    @server.tool(
        name="touai_balance",
        title="TouAI credit balance and spend gate",
        description=(
            "Read the wallet: whether the project can start billable work (`requires_topup`), the "
            "balance, held credits, and which products are blocked. Free. Admin keys get the full "
            "balance; other roles get the spend gate only."
        ),
        annotations=READ_ONLY_CLOSED,
    )
    def touai_balance() -> dict[str, Any]:
        client = get_client()
        gate = _run(client.billing.credits.spendability)
        out: dict[str, Any] = {
            "requires_topup": gate.requires_topup,
            "balance": _num(gate.balance),
            "overdraft_limit": _num(gate.overdraft_limit),
            "blocked_products": list(
                getattr(gate.product_access, "blocked_service_types", []) or []
            ),
        }
        try:
            bal = client.billing.credits.balance()
            out.update(
                {
                    "held_credits": _num(bal.held_credits),
                    "total_consumed": _num(bal.total_consumed),
                    "balance_display": bal.balance_display,
                }
            )
        except Exception:  # noqa: BLE001 — non-admin key; the gate above is enough
            out["note"] = "full ledger needs a project_admin key"
        return out

    @server.tool(
        name="touai_prices",
        title="TouAI price catalog",
        description="Current credit prices per product (service_type, billing mode, default credits per unit). Free.",
        annotations=READ_ONLY_CLOSED,
    )
    def touai_prices() -> list[dict[str, Any]]:
        rows = _run(get_client().billing.credits.costs)
        out = []
        for r in rows:
            try:
                price = _num(r.resolve_default_credits_per_unit())
            except Exception:  # noqa: BLE001 — aggregate rows have no unit price
                price = None
            out.append(
                {
                    "service_type": r.service_type,
                    "billing_mode": r.billing_mode,
                    "credits_per_unit": price,
                    "unit_name": r.unit_name,
                }
            )
        return out

    # ------------------------------------------------------------------ web access

    @server.tool(
        name="touai_web_scrape",
        title="Fetch a web page as Markdown",
        description=(
            "Fetch one URL and return its main content as clean Markdown plus title and links. "
            "Handles HTML, PDF and Office documents at the URL. Billable: 1 web_scrape per page. "
            "Raises on anti-bot walls, paywalls and 404s instead of returning empty text."
        ),
        annotations=BILLABLE,
    )
    def touai_web_scrape(
        url: str,
        wait_for_seconds: int = 0,
        max_chars: int = 60000,
    ) -> dict[str, Any]:
        page = _run(
            get_client().web_access.scrape, url, formats=["markdown"], wait_for=wait_for_seconds
        )
        md = page.markdown or {}
        text = md.get("fit_markdown") or md.get("raw_markdown") or ""
        return {
            "url": page.url,
            "title": (page.metadata or {}).get("title"),
            "markdown": _clip(text, max_chars),
            "links": list(page.links or [])[:200],
            "content_truncated": bool(page.content_truncated) or len(text) > max_chars,
        }

    @server.tool(
        name="touai_web_search",
        title="Web search",
        description=(
            "Search the web; returns ranked results with url, title and snippet. Billable: 1 "
            "web_search per query. `lang` defaults to English — pass e.g. 'zh' or 'ja' for other "
            "languages. Follow up with touai_web_scrape to read a result."
        ),
        annotations=BILLABLE,
    )
    def touai_web_search(query: str, limit: int = 10, lang: str | None = None) -> dict[str, Any]:
        res = _run(get_client().web_access.search, query, limit=limit, lang=lang)
        return {
            "query": res.query,
            "lang": res.lang,
            "total_results": res.total_results,
            "results": [
                {
                    "position": r.position,
                    "title": r.title,
                    "url": r.url,
                    "description": r.description,
                }
                for r in res.results
            ],
        }

    @server.tool(
        name="touai_web_crawl",
        title="Crawl a website",
        description=(
            "Crawl a site breadth-first from a start URL and return the Markdown of every page "
            "reached. Billable: 1 deep_crawl per page crawled, so `max_pages` (1–100) is the cost "
            "control; it is pre-flighted against the wallet. `exclude_patterns` are plain "
            "substrings. Blocks up to `timeout_seconds`; on timeout the crawl keeps running and the "
            "task_id is returned for touai_web_crawl_status."
        ),
        annotations=BILLABLE,
    )
    def touai_web_crawl(
        url: str,
        max_pages: int = 20,
        max_depth: int = 2,
        same_domain_only: bool = True,
        exclude_patterns: list[str] | None = None,
        timeout_seconds: int = 300,
        max_chars_per_page: int = 8000,
    ) -> dict[str, Any]:
        import touai

        client = get_client()
        kwargs: dict[str, Any] = dict(
            max_depth=max_depth, max_pages=max_pages, same_domain_only=same_domain_only
        )
        if exclude_patterns:
            kwargs["exclude_patterns"] = exclude_patterns
        task = _run(client.web_access.deep_crawl, url, **kwargs)
        try:
            status = _wait_crawl(client, task.task_id, timeout_seconds)
        except touai.PollTimeoutError:
            return {
                "status": "running",
                "task_id": task.task_id,
                "note": f"still crawling after {timeout_seconds}s; call touai_web_crawl_status",
            }
        except Exception as exc:  # noqa: BLE001
            raise _touai_error(exc) from exc
        return _crawl_payload(status, max_chars_per_page)

    @server.tool(
        name="touai_web_crawl_status",
        title="Crawl status / results",
        description="Read the status and pages of a crawl started by touai_web_crawl. Free.",
        annotations=READ_ONLY_CLOSED,
    )
    def touai_web_crawl_status(task_id: str, max_chars_per_page: int = 8000) -> dict[str, Any]:
        status = _run(get_client().web_access.deep_crawl_status, task_id)
        return _crawl_payload(status, max_chars_per_page)

    # ------------------------------------------------------------------ deep research

    @server.tool(
        name="touai_research",
        title="Deep research with a cited report",
        description=(
            "Run multi-source research (web, academic, patents, optionally the project's connected "
            "data) and return a Markdown report with numbered citations. Takes minutes and tens of "
            "credits (base 8 for mode 'pro', 10 for 'pro_plus', plus per-source costs) — tell the "
            "user before calling and use touai_web_search for simple lookups. Blocks up to "
            "`timeout_seconds`; on timeout the job keeps running and its job_id is returned for "
            "touai_research_status. Never re-submit the same query after a timeout."
        ),
        annotations=BILLABLE,
    )
    def touai_research(
        query: str,
        mode: Literal["pro", "pro_plus"] = "pro",
        max_iterations: int = 15,
        timeout_seconds: int = 900,
        knowledge_base_id: str | None = None,
    ) -> dict[str, Any]:
        import touai

        client = get_client()
        kwargs: dict[str, Any] = {"mode": mode, "max_iterations": max_iterations}
        if knowledge_base_id:
            kwargs["output"] = {
                "destination": "knowledge_base",
                "knowledge_base": {"knowledge_base_id": knowledge_base_id, "index": True},
            }
        job = _run(client.deep_research.create_job, query, **kwargs)
        try:
            job = client.deep_research.wait_for_job(
                job.job_id, poll_interval=10, timeout=timeout_seconds
            )
        except touai.PollTimeoutError:
            return {
                "status": "running",
                "job_id": job.job_id,
                "note": f"still running after {timeout_seconds}s; call touai_research_status with this job_id",
            }
        except Exception as exc:  # noqa: BLE001
            raise _touai_error(exc) from exc
        return _research_payload(job)

    @server.tool(
        name="touai_research_status",
        title="Research job status / report",
        description="Read a research job started by touai_research; returns the report once completed. Free.",
        annotations=READ_ONLY_CLOSED,
    )
    def touai_research_status(job_id: str) -> dict[str, Any]:
        job = _run(get_client().deep_research.get_job, job_id)
        return _research_payload(job)

    @server.tool(
        name="touai_research_cancel",
        title="Cancel a research job",
        description="Cancel a queued or running research job. Nothing is charged for a cancelled job.",
        annotations=WRITE,
    )
    def touai_research_cancel(job_id: str) -> dict[str, Any]:
        res = _run(get_client().deep_research.cancel_job, job_id)
        return {"job_id": job_id, "status": getattr(res, "status", "cancel_requested")}

    # ------------------------------------------------------------------ unstructured

    @server.tool(
        name="touai_parse_document",
        title="Parse a document, image, audio or video into Markdown",
        description=(
            "Extract text, tables and layout from a local file path or a public URL (PDF, Office, "
            "images with OCR, audio/video with transcription) and return Markdown chunks. Billable "
            "per document / media minute. Documents finish in ~1–2 min; video takes "
            "600 s + 0.7 × duration. On timeout the file_id or job_id is returned for "
            "touai_parse_status."
        ),
        annotations=BILLABLE,
    )
    def touai_parse_document(
        path_or_url: str,
        timeout_seconds: int = 600,
        max_chars: int = 80000,
    ) -> dict[str, Any]:
        import touai

        client = get_client()
        u = client.unstructured
        if path_or_url.startswith(("http://", "https://")):
            job = _run(u.jobs.create, source={"source_type": "url", "url": path_or_url})
            try:
                job = u.jobs.wait_until_complete(job.job_id, timeout=timeout_seconds)
            except touai.PollTimeoutError:
                return {
                    "status": "running",
                    "job_id": job.job_id,
                    "note": "still processing; call touai_parse_status with this job_id",
                }
            except Exception as exc:  # noqa: BLE001
                raise _touai_error(exc) from exc
            return _job_payload(client, job, max_chars)

        p = Path(path_or_url).expanduser()
        if not p.is_file():
            raise ToolError(
                f"{path_or_url} is not a readable file; pass a local path or an http(s) URL"
            )
        f = _run(u.files.upload_file, str(p))
        _run(u.files.process, f.file_id)
        try:
            u.files.wait_until_complete(f.file_id, timeout=timeout_seconds)
        except touai.PollTimeoutError:
            return {
                "status": "running",
                "file_id": f.file_id,
                "note": "still processing; call touai_parse_status with this file_id",
            }
        except Exception as exc:  # noqa: BLE001
            raise _touai_error(exc) from exc
        return _file_payload(client, f.file_id, max_chars)

    @server.tool(
        name="touai_parse_status",
        title="Parse status / result",
        description="Read a parse started by touai_parse_document (pass file_id or job_id); returns the Markdown once completed. Free.",
        annotations=READ_ONLY_CLOSED,
    )
    def touai_parse_status(
        file_id: str | None = None, job_id: str | None = None, max_chars: int = 80000
    ) -> dict[str, Any]:
        client = get_client()
        if file_id:
            return _file_payload(client, file_id, max_chars)
        if job_id:
            job = _run(client.unstructured.jobs.get, job_id)
            return _job_payload(client, job, max_chars)
        raise ToolError("pass file_id or job_id")

    @server.tool(
        name="touai_document_chat",
        title="Ask a question about a parsed document",
        description=(
            "Answer a question grounded in a document already parsed by touai_parse_document "
            "(by file_id). Billable: document_chat. Works for PDFs, images and Office files; for "
            "video use touai_video_chat."
        ),
        annotations=BILLABLE,
    )
    def touai_document_chat(file_id: str, question: str) -> dict[str, Any]:
        ans = _run(
            get_client().unstructured.documents.chat,
            [{"role": "user", "content": question}],
            file_id=file_id,
            stream=False,
        )
        return {"file_id": file_id, "answer": getattr(ans, "content", None) or str(ans)}

    @server.tool(
        name="touai_video_chat",
        title="Ask a question about a processed video",
        description="Answer a question about a video/audio file parsed by touai_parse_document (by file_id). Billable: video_chat.",
        annotations=BILLABLE,
    )
    def touai_video_chat(file_id: str, question: str) -> dict[str, Any]:
        ans = _run(
            get_client().unstructured.video.chat,
            [{"role": "user", "content": question}],
            file_id=file_id,
            stream=False,
        )
        msg = getattr(ans, "message", None) or {}
        return {
            "file_id": file_id,
            "answer": msg.get("content") if isinstance(msg, dict) else str(ans),
        }

    # ------------------------------------------------------------------ knowledge base

    @server.tool(
        name="touai_kb_list",
        title="List knowledge bases",
        description="List the project's knowledge bases with id, name and document count. Free.",
        annotations=READ_ONLY_CLOSED,
    )
    def touai_kb_list() -> list[dict[str, Any]]:
        res = _run(get_client().knowledge_base.bases.list, page_size=100)
        return [
            {
                "id": kb.id,
                "name": kb.name,
                "description": kb.description,
                "document_count": kb.document_count,
            }
            for kb in res.knowledge_bases
        ]

    @server.tool(
        name="touai_kb_create",
        title="Create a knowledge base",
        description="Create a knowledge base (name unique per project). Free to create; indexing documents into it is billed.",
        annotations=WRITE,
    )
    def touai_kb_create(name: str, description: str | None = None) -> dict[str, Any]:
        kb = _run(get_client().knowledge_base.bases.create, name, description=description)
        return {"id": kb.id, "name": kb.name}

    @server.tool(
        name="touai_kb_upload",
        title="Add a document or text to a knowledge base",
        description=(
            "Upload a local file (PDF, Office, Markdown, images, audio, video, JSON/XML) or add "
            "an in-memory text document to a knowledge base; the platform parses, chunks and "
            "indexes it. Billable: knowledge_ingest per indexed document. Returns when indexing "
            "finishes or after `timeout_seconds` with index_state 'pending'."
        ),
        annotations=BILLABLE,
    )
    def touai_kb_upload(
        knowledge_base_id: str,
        path: str | None = None,
        text: str | None = None,
        title: str | None = None,
        timeout_seconds: int = 300,
    ) -> dict[str, Any]:
        import time

        docs = get_client().knowledge_base.documents
        if path:
            p = Path(path).expanduser()
            if not p.is_file():
                raise ToolError(f"{path} is not a readable file")
            doc = _run(docs.upload, knowledge_base_id, str(p), title=title)
        elif text:
            doc = _run(docs.create_text, knowledge_base_id, title or "Untitled", text)
        else:
            raise ToolError("pass path or text")
        deadline = time.monotonic() + timeout_seconds
        while doc.index_state == "pending" and time.monotonic() < deadline:
            time.sleep(3)
            doc = _run(docs.get, doc.id)
        out = {"document_id": doc.id, "title": doc.title, "index_state": doc.index_state}
        if doc.index_state == "failed" and doc.index_error_detail:
            out["index_error"] = doc.index_error_detail
        return out

    @server.tool(
        name="touai_kb_search",
        title="Search a knowledge base",
        description=(
            "Hybrid (semantic + keyword) search over a knowledge base; returns chunks with scores "
            "and citations. Billable: 1 knowledge_search per query. `min_score` (0–1) drops "
            "weak semantic matches; 0.4–0.6 is a usual floor."
        ),
        annotations=BILLABLE,
    )
    def touai_kb_search(
        knowledge_base_id: str,
        query: str,
        top_k: int = 8,
        mode: Literal["hybrid", "semantic", "keyword"] = "hybrid",
        min_score: float | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"top_k": top_k, "mode": mode}
        if min_score is not None and mode != "keyword":
            kwargs["min_score"] = min_score
        res = _run(get_client().knowledge_base.search, knowledge_base_id, query, **kwargs)
        return {
            "score_type": res.score_type,
            "suppressed_low_relevance": res.suppressed_low_relevance,
            "hits": [
                {
                    "score": _num(h.score),
                    "semantic_score": _num(h.semantic_score),
                    "title": h.title,
                    "document_id": h.kbdoc_id,
                    "content": (h.chunk or {}).get("content", ""),
                    "citation": h.citation,
                }
                for h in res.hits
            ],
        }

    # ------------------------------------------------------------------ connectors

    @server.tool(
        name="touai_connections_list",
        title="List connected data sources",
        description="List the project's Connectors connections (databases, Google Drive, Notion, Slack, …) with readiness for content search. Free.",
        annotations=READ_ONLY_CLOSED,
    )
    def touai_connections_list() -> list[dict[str, Any]]:
        res = _run(get_client().connectors.connections.list)
        return [
            {
                "connection_id": c.connection_id,
                "name": c.name,
                "connector_type": c.connector_type,
                "status": c.status,
                "is_ready": c.is_ready,
                "not_ready_reason": c.not_ready_reason,
            }
            for c in res.items
        ]

    @server.tool(
        name="touai_connections_search",
        title="Search across connected data sources",
        description=(
            "Federated search over every ready connection (catalog + indexed content). Billable: "
            "1 connectors_search per query (free if only the raw catalog layer runs). Use "
            "`connection_ids` to narrow. Trust hits with match confidence 'high' or 'medium'."
        ),
        annotations=BILLABLE,
    )
    def touai_connections_search(
        query: str, top_k: int = 10, connection_ids: list[str] | None = None
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"top_k": top_k}
        if connection_ids:
            kwargs["connection_ids"] = connection_ids
        res = _run(get_client().connectors.search, query, **kwargs)
        hits = []
        for h in res.results:
            item: dict[str, Any] = {
                "kind": h.kind,
                "connection_id": h.connection_id,
                "connector_type": h.connector_type,
                "confidence": getattr(h.match, "confidence", None),
                "citation": h.citation,
            }
            if h.kind == "chunk" and h.chunk is not None:
                item["content"] = h.chunk.content
            if h.entity is not None:
                item["entity"] = {
                    "name": h.entity.name,
                    "entity_type": getattr(h.entity, "entity_type", None),
                }
            hits.append(item)
        return {
            "connections_searched": res.connections_searched,
            "hits": hits,
            "warnings": [w.code for w in res.warnings or []],
        }

    @server.tool(
        name="touai_connection_query",
        title="Run a read-only SQL / SOQL query on a connection",
        description=(
            "Execute one read-only statement against a database or Salesforce connection and "
            "return columns and rows. Billable: connectors_query. Use touai_connections_list "
            "to find the connection_id."
        ),
        annotations=BILLABLE,
    )
    def touai_connection_query(
        connection_id: str, sql: str, limit: int = 100, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"limit": limit}
        if params:
            kwargs["params"] = params
        rows = _run(get_client().connectors.connections.query, connection_id, sql, **kwargs)
        out: dict[str, Any] = {
            "success": rows.success,
            "columns": rows.columns,
            "row_count": rows.row_count,
            "rows": rows.data,
        }
        if not rows.success and rows.error is not None:
            out["error"] = {
                "code": rows.error.code,
                "message": getattr(rows.error, "upstream_message", None) or rows.message,
            }
        return out

    # ------------------------------------------------------------------ object storage

    @server.tool(
        name="touai_storage_upload",
        title="Upload a file to TouAI Object Storage",
        description="Store a local file in the project's default bucket (or `bucket_name`) and return its key. Storage is metered per GB-month.",
        annotations=WRITE,
    )
    def touai_storage_upload(path: str, bucket_name: str | None = None) -> dict[str, Any]:
        p = Path(path).expanduser()
        if not p.is_file():
            raise ToolError(f"{path} is not a readable file")
        storage = get_client().object_storage
        kwargs: dict[str, Any] = {}
        if bucket_name:
            kwargs["bucket_name"] = bucket_name
        with p.open("rb") as fh:
            obj = _run(storage.store, fh, **kwargs)
        return {
            "key": obj.key,
            "size": obj.size,
            "content_type": obj.content_type,
            "bucket": obj.bucket,
        }

    @server.tool(
        name="touai_storage_list",
        title="List stored files",
        description="List files in the default bucket (or `bucket_name`), optionally under a key prefix. Free.",
        annotations=READ_ONLY_CLOSED,
    )
    def touai_storage_list(
        bucket_name: str | None = None, prefix: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        storage = get_client().object_storage
        kwargs: dict[str, Any] = {}
        if bucket_name:
            kwargs["bucket_name"] = bucket_name
        if prefix:
            kwargs["prefix"] = prefix
        out = []
        for f in _run(storage.iter_files, **kwargs):
            out.append(
                {
                    "key": f.key,
                    "size": f.size,
                    "content_type": f.content_type,
                    "last_modified": f.last_modified.isoformat() if f.last_modified else None,
                }
            )
            if len(out) >= limit:
                break
        return out

    @server.tool(
        name="touai_storage_download_url",
        title="Get a download URL for a stored file",
        description="Mint a time-limited (60–86400 s) platform-hosted download URL for a stored key. Free.",
        annotations=READ_ONLY_CLOSED,
    )
    def touai_storage_download_url(
        key: str, bucket_name: str | None = None, expires_in: int = 3600
    ) -> dict[str, Any]:
        storage = get_client().object_storage
        kwargs: dict[str, Any] = {"expires_in": expires_in}
        if bucket_name:
            kwargs["bucket_name"] = bucket_name
        url = _run(storage.download_url, key, **kwargs)
        return {"key": key, "url": str(url), "expires_in": expires_in}

    return server


# ----------------------------------------------------------------------------- payload helpers


def _wait_crawl(client, task_id: str, timeout: int):
    import time

    import touai

    deadline = time.monotonic() + timeout
    while True:
        st = client.web_access.deep_crawl_status(task_id, include_pages=False)
        if st.status in ("completed", "failed", "expired", "cancelled"):
            return client.web_access.deep_crawl_status(task_id)
        if time.monotonic() >= deadline:
            raise touai.PollTimeoutError(f"crawl {task_id} still {st.status} after {timeout}s")
        time.sleep(min(5, max(1, st.retry_after_sec or 2)))


def _crawl_payload(status, max_chars_per_page: int) -> dict[str, Any]:
    pages = []
    for p in status.pages or []:
        pages.append(
            {
                "url": p.url,
                "title": p.title,
                "depth": p.depth,
                "status": p.status,
                "markdown": _clip(p.markdown, max_chars_per_page)
                if p.status == "success"
                else None,
                "error": p.error,
            }
        )
    return {
        "task_id": status.task_id,
        "status": status.status,
        "page_count": status.page_count,
        "error_code": status.error_code,
        "pages": pages,
    }


def _research_payload(job) -> dict[str, Any]:
    out: dict[str, Any] = {
        "job_id": job.job_id,
        "status": job.status,
        "credits_charged": _num(job.credits_charged),
    }
    if job.status == "completed":
        r = job.research_result()
        out.update(
            {
                "report_markdown": r.content,
                "citations": [
                    {"n": i, "label": c.label, "url": c.href, "internal": c.is_internal}
                    for i, c in enumerate(r.citations, 1)
                ],
                "synthesis_tier": r.synthesis_tier,
                "degraded_sources": [
                    d if isinstance(d, dict) else getattr(d, "model_dump", lambda: str(d))()
                    for d in r.degraded_sources or []
                ],
                "duration_sec": _num(r.duration_sec),
            }
        )
    elif job.status in ("failed", "cancelled"):
        out.update({"error_code": job.error_code, "error_message": job.error_message})
    else:
        out["queue_position"] = job.queue_position
        out["estimated_wait_seconds"] = job.estimated_wait_seconds
        out["note"] = "still running; call touai_research_status again later"
    return out


def _file_payload(client, file_id: str, max_chars: int) -> dict[str, Any]:
    d = _run(client.unstructured.files.get, file_id)
    out: dict[str, Any] = {
        "file_id": d.file_id,
        "filename": d.filename,
        "status": d.status,
        "file_type": d.file_type,
        "progress_stage": d.progress_stage,
    }
    if d.status == "completed":
        chunks = getattr(d, "chunks", None) or []
        text = "\n\n".join(c.content for c in chunks)
        out["markdown"] = _clip(text, max_chars)
        out["chunk_count"] = len(chunks)
        out["page_count"] = len(getattr(d, "pages", None) or [])
        if getattr(d, "degraded", False):
            out["degraded_reasons"] = [r.code for r in d.degraded_reasons or []]
        ins = getattr(d, "video_insights", None)
        if ins is not None:
            out["video_summary"] = ins.auto_summary
    elif d.status == "failed":
        out.update(
            {
                "error_code": d.error_code,
                "failure_reason": d.failure_reason,
                "error_message": d.error_message,
            }
        )
    return out


def _job_payload(client, job, max_chars: int) -> dict[str, Any]:
    out: dict[str, Any] = {"job_id": job.job_id, "status": job.status}
    if job.status == "completed":
        res = _run(client.unstructured.jobs.result, job.job_id)
        text = res.content or "\n\n".join(
            (c.get("content") if isinstance(c, dict) else getattr(c, "content", "")) or ""
            for c in res.chunks or []
        )
        out.update(
            {
                "filename": res.filename,
                "markdown": _clip(text, max_chars),
                "chunk_count": res.chunk_count,
            }
        )
    elif job.status == "failed":
        out.update({"error_code": job.error_code, "failure_reason": job.failure_reason})
    return out


# ----------------------------------------------------------------------------- entry point


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
