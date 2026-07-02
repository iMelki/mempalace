# CLI Commands

All commands accept `--palace <path>` to override the default palace location.

## `mempalace init`

Scan a project directory for people, projects, and rooms, and set up the palace.

```bash
mempalace init <dir>                 # <dir> is required
mempalace init <dir> --yes           # non-interactive mode
mempalace init ~/projects/myapp      # example
mempalace init .                     # initialize from the current directory
```

| Option  | Description                                                                  |
|---------|------------------------------------------------------------------------------|
| `<dir>` | **Required.** Project directory to scan. Pass `.` for the current directory. |
| `--yes` | Auto-accept all detected entities                                            |

What it does:

1. Scans `<dir>` for people and projects in file content
2. Detects rooms from `<dir>`'s folder structure
3. Saves detected entities to `<dir>/entities.json`
4. Ensures the global `~/.mempalace/` config directory exists

Running `mempalace init` with no argument will exit with
`error: the following arguments are required: dir`.

## `mempalace mine`

Mine files into the palace.

```bash
mempalace mine <dir>
mempalace mine <dir> --mode convos
mempalace mine <dir> --mode convos --extract general
mempalace mine <dir> --wing myapp
```

| Option | Default | Description |
|--------|---------|-------------|
| `<dir>` | — | Directory to mine |
| `--mode` | `projects` | `projects` for code/docs, `convos` for chat exports |
| `--wing` | directory name | Wing name override |
| `--agent` | `mempalace` | Agent name tag |
| `--limit` | `0` (all) | Max files to process |
| `--dry-run` | — | Preview without filing |
| `--extract` | `exchange` | `exchange` or `general` (for convos mode) |
| `--no-gitignore` | — | Don't respect .gitignore |
| `--include-ignored` | — | Always scan these paths even if ignored |

## `mempalace search`

Find anything by semantic search.

```bash
mempalace search "query"
mempalace search "query" --wing myapp
mempalace search "query" --wing myapp --room auth
mempalace search "query" --results 10
```

| Option | Default | Description |
|--------|---------|-------------|
| `"query"` | — | What to search for |
| `--wing` | all | Filter by wing |
| `--room` | all | Filter by room |
| `--results` | `5` | Number of results |

## `mempalace split`

Split concatenated transcript mega-files into per-session files.

```bash
mempalace split <dir>
mempalace split <dir> --dry-run
mempalace split <dir> --min-sessions 3
mempalace split <dir> --output-dir ~/split-output/
```

| Option | Default | Description |
|--------|---------|-------------|
| `<dir>` | — | Directory with transcript files |
| `--output-dir` | same dir | Write split files here |
| `--dry-run` | — | Preview without writing |
| `--min-sessions` | `2` | Only split files with N+ sessions |

## `mempalace wake-up`

Show L0 + L1 wake-up context (~600–900 tokens).

```bash
mempalace wake-up
mempalace wake-up --wing driftwood
```

| Option | Description |
|--------|-------------|
| `--wing` | Project-specific wake-up |

## `mempalace compress`

Compress drawers using AAAK Dialect.

```bash
mempalace compress --wing myapp
mempalace compress --wing myapp --dry-run
mempalace compress --config entities.json
```

| Option | Description |
|--------|-------------|
| `--wing` | Wing to compress (default: all) |
| `--dry-run` | Preview without storing |
| `--config` | Entity config JSON file |

## `mempalace status`

Show what's been filed — drawer count, wing/room breakdown.

```bash
mempalace status
```

`status` reads `chroma.sqlite3` first and falls back to the Chroma collection
only when SQLite metadata is unavailable. This keeps status usable when the
persisted HNSW vector segment is damaged, quarantined, or unavailable in a lean
runtime.

## `mempalace repair-status`

Compare SQLite metadata counts with persisted HNSW element counts without
opening a Chroma client.

```bash
mempalace repair-status
```

Use this before any repair. A large `DIVERGED` drawers result means vector
search is missing drawers and `repair --mode sqlite-replay --dry-run` is the
next safe command.

## `mempalace repair`

Rebuild palace vector index from stored data. Fixes segfaults after database corruption.

```bash
mempalace repair
mempalace repair --mode sqlite-replay --dry-run --batch-size 1000 --max-rows 100000
mempalace repair --mode sqlite-replay --batch-size 1000 --max-batches 820 --yes --confirm-large-reembed
```

| Option | Description |
|--------|-------------|
| `--mode legacy` | Legacy full-palace rebuild through the Chroma collection layer. Avoid this when `repair-status` says HNSW is diverged. |
| `--mode sqlite-replay` | Rebuild the drawers collection from `chroma.sqlite3` metadata rows. |
| `--dry-run` | Print the replay plan without deleting or rewriting the collection. |
| `--batch-size` | Per-upsert replay chunk size. This is not a total row limit or bounded maintenance window. |
| `--max-rows` | Abort before mutation when the planned replay would exceed this many document rows. |
| `--max-batches` | Abort before mutation when the planned replay would exceed this many replay batches. |
| `--artifact-dir` | Write `result.json` and `events.jsonl` to an explicit directory. Defaults to `<palace>/.mempalace/repair-runs/<run>`. |
| `--json` | Print machine-readable run output; human console lines are captured under `stdout`. |
| `--backup` / `--no-backup` | Historical toggle. SQLite replay still creates an immutable source snapshot before mutation, even when `--no-backup` is supplied, because replay cannot safely read from the live DB after deleting the target collection. |
| `--confirm-large-reembed` | Required when SQLite replay would re-embed more than 100,000 documents. This can run for hours. |

For diverged HNSW incidents, run `repair-status` and the SQLite replay dry-run
first. Schedule the non-dry replay only after a full palace backup and a quiet
maintenance window. `--max-rows` and `--max-batches` are preflight gates, not
partial replay controls: if either bound is lower than the planned full replay,
the command aborts before deleting the target collection.

## `mempalace mcp`

Helper command that outputs setup syntax (like `claude mcp add...`) to connect MemPalace to your AI client, automatically handling paths.

```bash
mempalace mcp
mempalace mcp --palace ~/.custom-palace
```

## `mempalace hook`

Run hook logic for Claude Code / Codex integration.

```bash
mempalace hook run --hook stop --harness claude-code
mempalace hook run --hook precompact --harness claude-code
mempalace hook run --hook session-start --harness codex
```

| Option | Values | Description |
|--------|--------|-------------|
| `--hook` | `session-start`, `stop`, `precompact` | Hook name |
| `--harness` | `claude-code`, `codex` | Harness type |

## `mempalace instructions`

Output skill instructions to stdout.

```bash
mempalace instructions init
mempalace instructions search
mempalace instructions mine
mempalace instructions help
mempalace instructions status
```
