# Changelog

## 0.4.0 (2025-11)

### Breaking changes

- **Package renamed** `clm_mem` → `shatterling`. All imports change:
  `from shatterling.engine.harness import ...` replaces the old flat layout.
- **Subpackage split**: the monolithic `clm_mem/` package is now four
  subpackages — `engine`, `purslane`, `campion`, `clients` — plus `tools/`.
- **Session API**: explicit `open` / `turns` / SSE verbs replace the old
  implicit header-memory model. The legacy `X-Cllm-Mem-*` headers now return
  HTTP 410 Gone (shim remains for one release cycle).
- **CLI entry point**: `purslane` is the canonical command; `clm-mem` is a
  deprecated alias scheduled for removal in 0.5.

### Added

- Declarative route table (`purslane/routes.py`) with `match()` dispatch.
- `TimingRecorder` / `SessionMetrics` module (`purslane/metrics.py`).
- MCP server decoupled into an HTTP client of `/v1/clm/*` endpoints.
- Chat isolation: sessions are isolated by default; reheat scopes
  (`global` | `chat` | `fresh`) control memory visibility.
- Python SDK client (`shatterling/clients/sdk.py`).
- Security audit report and remediation (see `SECURITY_AUDIT.md`).

### Fixed

- Reopened branch no longer reports `created: true` / HTTP 201.
- Percent-encoded session paths handled in turns verb.
- Search bot-blocks and fork exhaustion surfaced instead of silent failures.
- CI: Test and Lint workflows green.

## 0.3.x (2025-10)

- Initial CLM harness with three-phase eviction.
- Tagging system (`<longterm>` / `<shortterm>` markers).
- Studio browser UI.
- Tool sandbox and web fetch.
