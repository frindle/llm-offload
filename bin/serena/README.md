# Serena (offload plan step 8)

Status: installed on the Mac (fallback host) via `uv tool install serena-agent` (Serena 2.0.0). Unraid SSH is unreachable, so it moves there once a host/alias exists.

Pilot rules:
- Read-only symbol tools only. Editing tools stay off (`replace_symbol_body` corrupts TS exports, upstream #1956).
- Start with `serena start-mcp-server --project <path> --mode <read-only mode>`; confirm the mode name with `--help` before wiring it into any client.
- Not registered in `~/.claude.json` (left untouched on purpose).
- Drop it if it can't serve fresh working copies.
