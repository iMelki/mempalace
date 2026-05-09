# Contributing to MemPalace

MemPalace is the unified knowledge brain of the portfolio. This repository follows the portfolio-wide governance baseline.

## Issue Flow

1. Create work from `.github/ISSUE_TEMPLATE/agent_task.md`.
2. Link relevant docs or plans.
3. Classify work with risk, agent suitability, and scope.
4. Use labels from `.github/labels.yml` (e.g., `domain:memory`).
5. Maintain `OPEN_TASKS.md` as the local task index.

## Branch Naming

Use the agent branch convention:
```text
agent/{agent-name}/{issue-number}-{slug}
```

## Pull Requests

1. Use `.github/PULL_REQUEST_TEMPLATE.md`.
2. Link the issue with `Closes #<number>`.
3. Request human review for medium, high, and critical risk changes.
4. Ensure tests pass and the HNSW index is stable.

## Commit Messages

Prefer conventional commits (`feat`, `fix`, `docs`, `refactor`, `chore`).

## Technical Guidelines

- Use `uv run` for executing scripts and tests.
- Run `ruff check .` and `ruff format .` before committing.
- Ensure all new features have corresponding unit or integration tests.
- Do not modify the core vector search logic without performance regression testing.

## Protected Files

- Do not modify `mempalace.yaml` unless explicitly requested.
- Treat `.github/` and `AGENTS.md` as governance files.
- Never commit private memory drawers or database backups.
