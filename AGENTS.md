# Agent Instructions - MemPalace

MemPalace is the core knowledge graph and semantic memory system for the AI Agent Task Management OS.

## Operating Rules

- Treat GitHub issues as canonical task records and `OPEN_TASKS.md` as the local index.
- Follow the governance baseline in [CONTRIBUTING.md](CONTRIBUTING.md).
- Use branch names in the form `agent/{agent-name}/{issue-number}-{slug}`.
- Stay within the allowed file scope defined in the issue.
- Maintain the HNSW index and database integrity when modifying core logic.
- Use `uv` for dependency management and environment isolation.

## Technical Standards

- Language: Python 3.12+
- Tooling: `uv`, `ruff`, `pytest`, `mypy`.
- Documentation: AAAK (Agent-to-Agent Knowledge) and MemPalace schema.
- HNSW: Vector search via `chromadb` or custom HNSW implementation.

## Safety

- Do not commit production database files (`.sqlite`, `.bin`).
- Do not expose `mempalace.yaml` configurations that contain private paths.
- Perform index rebuilds only when necessary and verify retrieval correctness with tests.