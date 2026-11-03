# touai-mcp

An [MCP](https://modelcontextprotocol.io) server that gives coding agents (Cursor, Claude Code,
Codex, Claude Desktop, …) tools for the [TouAI](https://tou.ai) platform: web scraping and
search, deep research with cited reports, document / video parsing, knowledge bases,
connectors to databases and SaaS apps, object storage and billing.

It is a thin layer over the official Python SDK, `touai`. Credentials stay in the
environment of the server process and never travel over the protocol.

## Setup

1. Create a project API key in the TouAI console (Project → API Keys). You get an access key
   id (`ak_live_…`) and a secret (`sk_live_…`).
2. Add the server to your client with `uvx touai-mcp` (needs [uv](https://docs.astral.sh/uv/))
   or `pipx run touai-mcp`, using one of the methods below.

### Let your agent do it

Paste this into Cursor, Claude Code or Codex:

```
Add the "touai" MCP server to my config: command `uvx`, args `["touai-mcp"]`,
env `TOUAI_ACCESS_KEY_ID` and `TOUAI_SECRET_KEY`. Leave the two values as placeholders;
I will fill them in myself. Then tell me where the config file is and remind me that the
key pair comes from the TouAI console under Project → API Keys.
```

The agent writes the entry and tells you where to get the key pair; you fill in the two
values and restart the client. Never paste the secret into the chat.

### Cursor — one click

[![Add touai MCP server to Cursor](https://cursor.com/deeplink/mcp-install-dark.svg)](cursor://anysphere.cursor-deeplink/mcp/install?name=touai&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyJ0b3VhaS1tY3AiXSwiZW52Ijp7IlRPVUFJX0FDQ0VTU19LRVlfSUQiOiJha19saXZlXy4uLiIsIlRPVUFJX1NFQ1JFVF9LRVkiOiJza19saXZlXy4uLiJ9fQ==)

Replace the two placeholder values with your key pair after installing.

### Cursor — `~/.cursor/mcp.json` (or `.cursor/mcp.json` in a project)

```json
{
  "mcpServers": {
    "touai": {
      "command": "uvx",
      "args": ["touai-mcp"],
      "env": {
        "TOUAI_ACCESS_KEY_ID": "ak_live_...",
        "TOUAI_SECRET_KEY": "sk_live_..."
      }
    }
  }
}
```

### Claude Code

```bash
claude mcp add touai -e TOUAI_ACCESS_KEY_ID=ak_live_... -e TOUAI_SECRET_KEY=sk_live_... -- uvx touai-mcp
```

### Codex — `~/.codex/config.toml`

```toml
[mcp_servers.touai]
command = "uvx"
args = ["touai-mcp"]
env = { TOUAI_ACCESS_KEY_ID = "ak_live_...", TOUAI_SECRET_KEY = "sk_live_..." }
```

### Claude Desktop — `claude_desktop_config.json`

Same shape as the Cursor block under `"mcpServers"`.

Optional environment: `TOUAI_BASE_URL`, `TOUAI_TIMEOUT`, `TOUAI_MAX_RETRIES` (see the SDK docs).

## Tools

| Tool | Billable | What it does |
|---|---|---|
| `touai_project_services` | no | Which products the key's project may use, and why the others are off |
| `touai_balance` | no | Spend gate, balance, blocked products |
| `touai_prices` | no | Credit price per product |
| `touai_web_scrape` | per page | One URL → clean Markdown, title, links (HTML, PDF, Office) |
| `touai_web_search` | per query | Ranked web results |
| `touai_web_crawl` / `touai_web_crawl_status` | per page | Crawl a site, bounded by `max_pages` |
| `touai_research` / `touai_research_status` / `touai_research_cancel` | tens of credits | Multi-source research → cited Markdown report |
| `touai_parse_document` / `touai_parse_status` | per document / media minute | Local file or URL → Markdown chunks (OCR, tables, transcription) |
| `touai_document_chat`, `touai_video_chat` | per question | Ask about a parsed file |
| `touai_kb_list`, `touai_kb_create` | no | Knowledge bases |
| `touai_kb_upload` | per document | Add a file or text to a knowledge base and index it |
| `touai_kb_search` | per query | Hybrid search with citations |
| `touai_connections_list` | no | Connected data sources and their readiness |
| `touai_connections_search` | per query | Federated search across connections |
| `touai_connection_query` | per query | Read-only SQL / SOQL on a connection |
| `touai_storage_upload`, `touai_storage_list`, `touai_storage_download_url` | storage only | Project buckets |

Long-running tools (`touai_research`, `touai_parse_document`, `touai_web_crawl`) block up to
`timeout_seconds`; on timeout they return the job id and the work continues server-side —
call the matching `*_status` tool instead of re-submitting.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
# until `touai` is on PyPI, install the SDK from a sibling checkout first:
#   uv pip install ../touai-python
.venv/bin/pytest
TOUAI_ACCESS_KEY_ID=… TOUAI_SECRET_KEY=… .venv/bin/touai-mcp   # stdio; point your client at this command
```

While developing, point a client at the checkout's venv instead of `uvx` (`uv run --directory …`
only works once `touai` resolves from PyPI):

```json
{ "command": "/path/to/touai-mcp/.venv/bin/touai-mcp", "env": { … } }
```

## Agent Skills

For agents that write Python against the SDK directly, see
[touai/touai-skills](https://github.com/touai/touai-skills). The two are complementary: skills
teach code, this server provides ready-made tools.

## License

Apache-2.0
