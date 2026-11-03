"""touai-mcp — MCP server exposing the TouAI platform to agents.

Run ``touai-mcp`` (stdio) with ``TOUAI_ACCESS_KEY_ID`` / ``TOUAI_SECRET_KEY`` in the
environment. See README.md for Cursor / Claude Code / Codex configuration.
"""

from ._version import __version__
from .server import build_server, main

__all__ = ["__version__", "build_server", "main"]
