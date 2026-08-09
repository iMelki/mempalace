# Mine-time exclusions and backup/variant detection

Tracking issue: [#36](https://github.com/iMelki/mempalace/issues/36)

Two mechanisms, deliberately different in kind:

| | Generated/vendored content | Backup/variant directories |
|---|---|---|
| Behaviour | **Excluded** at ingest | **Reported** for operator confirmation |
| Reversible? | Yes, from `mempalace.yaml` | Nothing to reverse — nothing is applied |
| Why | Machine-generated, near-identical across project copies, close to zero recall value | A directory named `backup` can hold the only surviving copy of something |

## Why exclusions, not deduplication

A `pnpm-lock.yaml` is generated, enormous, and nobody asks the palace what a
transitive dependency hash was. Roughly 6,900+ drawers in one wing were
lockfile chunks; five on-disk copies of a single project produced four
near-identical drawer sets.

Deleting those drawers afterwards is the expensive, irreversible direction in
a store whose stated requirement is verbatim 100% recall
(`"Incremental only — append-only ingest"`, `"Verbatim always"`). Declining to
ingest is free and it stops the problem regenerating on every future mine.

Cleanup of content **already** in the palace is out of scope here — that stays
under [#19](https://github.com/iMelki/mempalace/issues/19) and remains gated
on a verified offsite backup.

## What was already there (and is reused, not replaced)

The miner already had three ignore mechanisms before #36:

1. **`.gitignore` respect** — `GitignoreMatcher` in `mempalace/miner.py`,
   ancestor-chained, last-match-wins, with `--no-gitignore` to disable and
   `--include-ignored PATH` to force-include.
2. **`palace.SKIP_DIRS`** — a hardcoded directory-name set.
3. **`miner.SKIP_FILENAMES`** — a hardcoded filename set.

`.gitignore` never covered lockfiles, because lockfiles are committed on
purpose. That is exactly why they reached the palace. `SKIP_DIRS` was missing
`obj` and `bin`, which is why a `CategoriesAPI.Tests/obj` tree contributed 418
drawers.

`mempalace/mine_exclusions.py` therefore does **not** add a fourth mechanism.
It turns (2) and (3) into one configurable, documented policy, seeded from the
old sets so nothing that was skipped before becomes mineable now, and extends
the default membership. `.gitignore` handling is untouched.

## Configuration

Optional blocks in the project's `mempalace.yaml`:

```yaml
exclude:
  generated_files: true          # master switch for the lockfile/minified set
  generated_dirs: true           # master switch for the build-output set
  files: ["*.snap", "notes.tmp"] # extra names or globs to exclude
  dirs: ["fixtures_huge"]        # extra directory names or globs to exclude
  allow_files: ["poetry.lock"]   # un-exclude — the recall escape hatch
  allow_dirs: ["bin"]            # un-exclude

variants:
  enabled: true                  # report-only advisory in the mine header
  max_depth: 3
  globs: ["*_snapshot"]          # extra backup/variant name patterns
```

Names and globs are matched case-insensitively (Windows filesystems are), and
unknown keys are ignored so an older mempalace still mines a newer project
config.

Inspect the effective policy for any directory:

```bash
mempalace exclusions ~/projects/myapp
mempalace exclusions ~/projects/myapp --json
```

### Default excluded filenames

Dependency lockfiles, one entry per ecosystem:
`package-lock.json`, `npm-shrinkwrap.json`, `pnpm-lock.yaml`, `yarn.lock`,
`bun.lock`, `bun.lockb`, `poetry.lock`, `Pipfile.lock`, `pdm.lock`, `uv.lock`,
`Cargo.lock`, `go.sum`, `composer.lock`, `Gemfile.lock`, `mix.lock`,
`pubspec.lock`, `packages.lock.json`, `Podfile.lock`, `Package.resolved`.

Build products that keep a readable extension: `*.min.js`, `*.min.css`,
`*.bundle.js`.

**The lockfile policy call is the operator's.** The default is exclusion
because a lockfile is machine-generated and its recall value is close to zero,
but if you want dependency-resolution history recallable, one line reverses it:

```yaml
exclude:
  allow_files: ["pnpm-lock.yaml"]
```

### Default excluded directories

Everything previously in `palace.SKIP_DIRS` (`.git`, `node_modules`,
`__pycache__`, `.venv`, `venv`, `env`, `dist`, `build`, `.next`, `coverage`,
`.mempalace`, `.ruff_cache`, `.mypy_cache`, `.pytest_cache`, `.cache`, `.tox`,
`.nox`, `.idea`, `.vscode`, `.ipynb_checkpoints`, `.eggs`, `htmlcov`,
`target`) plus `obj`, `bin`, `out`, `vendor`, `bower_components`,
`jspm_packages`, `Pods`, `site-packages`, `.terraform`, `.gradle`,
`.dart_tool`, `.turbo`, `.parcel-cache`, `.svelte-kit`, `.nuxt`, `.angular`,
`.yarn`, `.pnpm-store`, `.serverless`, `.output`, and `*.egg-info`.

> **`bin` is the entry most likely to need reverting.** It is .NET build
> output, but Unix-style repos keep hand-written entry scripts in `bin/`. For
> those projects add `allow_dirs: ["bin"]`.

### Per-path override

The pre-existing `--include-ignored` flag still wins over the policy, so a
one-off mine can pull in a specific excluded path without editing config:

```bash
mempalace mine ~/projects/myapp --include-ignored pnpm-lock.yaml
```

## Backup/variant detection — report only

```bash
mempalace variants ~/source/EMTS/Repeater_System
mempalace variants ~/source/EMTS/Repeater_System --json
```

This never excludes, deletes, or mines anything. It walks up to `max_depth`
directories (pruning the excluded set so their internals cannot generate
noise) and reports candidates from three independent signals:

| Signal | Meaning |
|---|---|
| `name-pattern` | Name matches a backup/variant glob (`*backup*`, `*git_broke*`, `*alpha-ver*`, `*-fixes`, `*_old`, `*-v[0-9]`, …) |
| `date-stamp` | Name ends in a plausible `yymmdd` / `yyyymmdd` stamp behind a separator (`_250522`) |
| `sibling-prefix` | Name is a longer suffixed form of a sibling directory (`backend` → `backend-backup-git_broke`) |

`confidence` is `high` with two or more signals, `medium` for a name/date
signal alone, `low` for a sibling prefix alone. It is advisory ranking only —
nothing acts on a threshold.

The same summary appears in the `mempalace mine` header (truncated), and
`--no-variant-report` silences it.

**Blanket exclusion of these is unsafe, which is why it is not offered.**
Confirm each candidate yourself, then add the names you agree with to
`exclude.dirs`.

## Provenance

The effective policy has a stable digest (`mempalace exclusions --json`) and
that digest is bound into the deterministic source-plan contract, so a plan
discovered under one policy can never be resumed under another.

It is deliberately **not** part of the per-source write-receipt config digest:
exclusions only ever remove *future* sources and never change how an
already-ingested source was chunked or stored, so existing receipts stay valid
and changing the policy does not force a palace-wide re-mine of content that
is already correct.
