# MemPalace Agent Instructions

MemPalace is a local-first memory repository with upstream project history and a private operational fork.

## Operating Rules

- Treat GitHub issues as canonical task records and `OPEN_TASKS.md` as the local index.
- Follow the governance baseline in [CONTRIBUTING.md](CONTRIBUTING.md).
- Use branch names in the form `agent/{agent-name}/{issue-number}-{slug}`.
- Stay within the allowed file scope defined in the issue.
- Preserve upstream contribution expectations unless the issue explicitly scopes fork-specific policy changes.

## Safety

- Do not ingest, mine, sync, deploy, or mutate memory stores without explicit user approval and a rollback/backup plan.
- Do not add durable memories from model inference alone. Durable memory needs evidence, source, timestamp, and a deletion path.
- Do not store secrets, credentials, private exports, or raw personal data in this repo.
- Treat runtime search, MCP server behavior, and memory taxonomy changes as functional work requiring targeted tests.

## Technical Standards

- Python changes must preserve the existing test suite and avoid network/API requirements by default.
- Documentation changes should preserve upstream guidance and clearly mark fork-specific operations.
- Run the smallest relevant verification command and record it in the PR.
