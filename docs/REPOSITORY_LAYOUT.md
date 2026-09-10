# Repository layout and provenance

## Directory boundaries

| Location | Purpose |
| --- | --- |
| `src/` | Installed Python service and its internal modules. |
| `native/` | Swift helper that owns macOS permissions and talks to the service over a local socket. |
| `deploy/` | Standalone installation, client distribution and verification programs, plus the portable client adapter and its lock. These run before the server package is installed or on other computers, so they cannot depend on importing the server package. |
| `tests/` | Regressions and explicitly selected live checks. |
| `skills/` | Agent instructions distributed with the client setup. |
| `docs/` | Architecture, operating instructions and verification records. |
| `pyproject.toml`, `uv.lock` | Package metadata and reproducible server dependencies. Both projects use uv and its build backend. |
| `.venv/`, `dist/`, caches | Ignored, generated local dependencies and build output. |

Python in both `src` and `deploy` is a distinction of responsibility, not a second server implementation. The adapter uses inline script dependencies and its own lock because it runs on client computers without the macOS server package. MCP does not mandate this folder layout; it follows the established private Mail deployment. Retaining the adapter beside its installer also preserves the existing fleet copy contract.

## Runtime and dependency boundary

The signed helper owns the OS permission; the Python service exposes authenticated loopback HTTP; Tailscale Serve supplies private remote access; the client adapter supplies stdio to agent applications. Configuration and credentials live outside the repositories.

Both services pin FastMCP 4.0.3. The portable adapters pin 3.4.5 to preserve the established handshake and interactive confirmation behavior, as documented in Mail's GLOBAL_COMMUNICATIONS guide. A major-version client upgrade needs compatibility verification; it is not implied by a source-layout cleanup. Version 4.0.3 was verified as the current PyPI release on September 10, 2026: https://pypi.org/project/fastmcp/.

## Mail source and consolidation

This private fork uses https://github.com/chikingsley/apple-mail-fast-mcp.git as origin and retains the upstream MIT license and historical documentation. The package is 0.11.0; that number is not the FastMCP dependency version.

On September 10, 2026, `apple-mail-global` was a linked worktree owned by `apple-mail-fast-mcp/.git`. The latter contained an unfinished, uncommitted replacement declaring version 1.0.0. That declaration did not make it a published or validated upgrade. The maintained worktree was converted to an independent repository without changing its HEAD or files, preserving branches and tags. The complete old checkout, including its runtime data and Git metadata, was moved to:

`/Users/simonpeacocks/.local/share/peacockery/source-backups/20260910T230200Z/apple-mail-fast-mcp`

The enclosing owner-only backup also contains a verified all-refs Git bundle and pre-cleanup source archives. Do not develop or run services from that recovery archive. The active source path remains `~/GitHub/apple-mail-global`.

The running Mail/Beeper services use a fixed release under `~/.local/share/peacockery/apple-mail/releases/`, not either development checkout. The September 10 cleanup does not redeploy their unchanged runtime behavior.

## Removed distribution leftovers

The `mcpb` manifest and `.claude-plugin` manifests advertised version 0.10.2 and a local macOS installation. They were not the installed shared service/client route. They and the unused MCPB builder and bundle-publishing workflow were removed together; history and the backup preserve them. MCPB is an optional desktop distribution format, not an MCP runtime requirement: https://github.com/modelcontextprotocol/mcpb.

`LICENSE` stays because it carries the license and attribution for reused upstream code. Historical changelogs and research remain in source history/documentation; they are not executable deployment configuration.
