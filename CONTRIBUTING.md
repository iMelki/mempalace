# Contributing to MemPalace

Thanks for wanting to help. MemPalace is open source and we welcome contributions of all sizes — from typo fixes to new features.

## iMelki Fork Governance

This fork also follows the portfolio-wide governance baseline for AI/human agent work:

1. Create scoped work from `.github/ISSUE_TEMPLATE/agent_task.md` when assigning tasks to agents.
2. Link relevant docs, issues, PRs, and plans before implementation.
3. Classify work with risk, agent suitability, and allowed file scope.
4. Use labels from `.github/labels.yml`, including `domain:memory` when applicable.
5. Maintain `OPEN_TASKS.md` as the local task index.

Use the agent branch convention:

```text
agent/{agent-name}/{issue-number}-{slug}
```

Request human review for medium, high, and critical risk changes. Treat memory ingestion, sync, MCP behavior, provider configuration, and search ranking/retrieval behavior as functional changes that need targeted tests.

## Getting Started

```bash
# Fork the repo on GitHub first, then clone your fork
git clone https://github.com/<your-username>/mempalace.git
cd mempalace
git remote add upstream https://github.com/MemPalace/mempalace.git

pip install -e ".[dev]"    # installs with dev dependencies (pytest, build, twine)
```

## Running Tests

```bash
pytest tests/ -v
```

All tests must pass before submitting a PR. Tests should run without API keys or network access.

## Running Benchmarks

```bash
# Quick test (20 questions, ~30 seconds)
python benchmarks/longmemeval_bench.py /path/to/longmemeval_s_cleaned.json --limit 20

# Full benchmark (500 questions, ~5 minutes)
python benchmarks/longmemeval_bench.py /path/to/longmemeval_s_cleaned.json
```

See [benchmarks/README.md](benchmarks/README.md) for data download instructions and reproduction guide.

## Project Structure

```
mempalace/          ← core package (see mempalace/README.md for module guide)
benchmarks/         ← reproducible benchmark runners
hooks/              ← Claude Code auto-save hooks
examples/           ← usage examples
tests/              ← test suite
assets/             ← logo + brand
```

## PR Guidelines

1. Fork the repo and create a feature branch: `git checkout -b feat/my-thing`
2. Write your code
3. Add or update tests if applicable
4. Run `pytest tests/ -v` — everything must pass
5. Run `pwsh -NoProfile -File scripts/check-markdown-links.ps1` when Markdown docs change
6. Commit with a clear message following [conventional commits](https://www.conventionalcommits.org/):
   - `feat: add Notion export format`
   - `fix: handle empty transcript files`
   - `docs: update MCP tool descriptions`
   - `bench: add LoCoMo turn-level metrics`
7. Push to your fork and open a PR against `develop`

## Code Style

- **Formatting**: [Ruff](https://docs.astral.sh/ruff/) with 100-char line limit (configured in `pyproject.toml`)
- **Naming**: `snake_case` for functions/variables, `PascalCase` for classes
- **Docstrings**: on all modules and public functions
- **Type hints**: where they improve readability
- **Dependencies**: minimize. ChromaDB + PyYAML only. Don't add new deps without discussion.

## Good First Issues

Check the [Issues](https://github.com/MemPalace/mempalace/issues) tab. Great starting points:

- **New chat formats**: Add import support for Cursor, Copilot, or other AI tool exports
- **Room detection**: Improve pattern matching in `room_detector_local.py`
- **Tests**: Increase coverage — especially for `knowledge_graph.py` and `palace_graph.py`
- **Entity detection**: Better name disambiguation in `entity_detector.py`
- **Docs**: Improve examples, add tutorials

## Architecture Decisions

If you're planning a significant change, open an issue first to discuss the approach. Key principles:

- **Verbatim first**: Never summarize user content. Store exact words.
- **Local first**: Everything runs on the user's machine. No cloud dependencies.
- **Zero API by default**: Core features must work without any API key.
- **Palace structure is scoping, not magic**: Wings, halls, and rooms act as metadata filters in the underlying vector store. They keep retrieval predictable when a palace holds many unrelated projects or people. Respect the hierarchy — but don't present it as a novel retrieval mechanism.

## Community

- **Discord**: [Join us](https://discord.com/invite/ycTQQCu6kn)
- **Issues**: Bug reports and feature requests welcome
- **Discussions**: For questions and ideas

## License

MIT — your contributions will be released under the same license.
