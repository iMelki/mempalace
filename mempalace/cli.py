#!/usr/bin/env python3
"""
MemPalace — Give your AI a memory. No API key required.

Two ways to ingest:
  Projects:      mempalace mine ~/projects/my_app          (code, docs, notes)
  Conversations: mempalace mine <convo-dir> --mode convos     (Claude Code, Claude.ai, ChatGPT, Slack exports)

Same palace. Same search. Different ingest strategies.

Commands:
    mempalace init <dir>                  Detect rooms from folder structure
    mempalace split <dir>                 Split concatenated mega-files into per-session files
    mempalace mine <dir>                  Mine project files (default)
    mempalace mine <dir> --mode convos    Mine conversation exports
    mempalace search "query"              Find anything, exact words
    mempalace mcp                         Show MCP setup command
    mempalace wake-up                     Show L0 + L1 wake-up context
    mempalace wake-up --wing my_app       Wake-up for a specific project
    mempalace status                      Show what's been filed

Examples:
    mempalace init ~/projects/my_app
    mempalace mine ~/projects/my_app
    mempalace mine ~/.claude/projects/-Users-you-Projects-my_app --mode convos --wing my_app
    mempalace search "why did we switch to GraphQL"
    mempalace search "pricing discussion" --wing my_app --room costs
"""

import json
import os
import sys
import shlex
import argparse
from pathlib import Path

from .config import MempalaceConfig
from .corpus_origin import detect_origin_heuristic, detect_origin_llm
from .llm_client import LLMError, get_provider
from .version import __version__


_MEMPALACE_PROJECT_FILES = ("mempalace.yaml", "entities.json")

# Pass 0 corpus-origin sampling caps. Tier 1 reads FULL file content (no
# front-bias sampling) but bounds total memory on enormous corpora. Tier 2
# trims to a smaller view because LLM context windows are finite.
_PASS_ZERO_MAX_FILES = 30
_PASS_ZERO_PER_FILE_CAP = 100_000  # 100KB per file is generous for prose
_PASS_ZERO_TOTAL_CAP = 5_000_000  # 5MB total ceiling — bounds memory
_PASS_ZERO_LLM_PER_SAMPLE = 2_000  # for Tier 2 LLM call only
_PASS_ZERO_LLM_MAX_SAMPLES = 20  # caps the LLM-tier sample count


def _gather_origin_samples(project_dir) -> list:
    """Collect Tier-1 samples for corpus-origin detection.

    Reads FULL file content (capped at ``_PASS_ZERO_PER_FILE_CAP`` per file
    and ``_PASS_ZERO_TOTAL_CAP`` overall). No front-bias sampling — AI
    signal that lives past the first N chars of a file must still trip
    detection, so we read the whole file up to the cap.

    Skips mempalace's own per-project artifacts (``entities.json``,
    ``mempalace.yaml``) so a re-run of ``mempalace init`` produces the
    same classification result it did on the first run. Without this
    filter, the first run writes entities.json into the corpus, the
    second run picks it up as a sample, and the Tier-1 density math
    drifts (different total_chars). That makes init non-idempotent.

    Returns a list of strings (one per readable file). Empty list when
    the project has no readable text.
    """
    from .entity_detector import scan_for_detection

    files = scan_for_detection(project_dir, max_files=_PASS_ZERO_MAX_FILES)
    samples: list = []
    total_chars = 0
    for filepath in files:
        if filepath.name in _MEMPALACE_PROJECT_FILES:
            continue
        if total_chars >= _PASS_ZERO_TOTAL_CAP:
            break
        try:
            with open(filepath, encoding="utf-8", errors="replace") as f:
                content = f.read(_PASS_ZERO_PER_FILE_CAP)
        except OSError:
            continue
        if not content:
            continue
        samples.append(content)
        total_chars += len(content)
    return samples


def _trim_samples_for_llm(samples: list) -> list:
    """Reduce Tier-1 full-content samples to LLM-friendly size.

    Tier 2 hits an LLM with a finite context window — we trim each sample
    to ``_PASS_ZERO_LLM_PER_SAMPLE`` chars and cap the overall sample
    count at ``_PASS_ZERO_LLM_MAX_SAMPLES``.
    """
    return [s[:_PASS_ZERO_LLM_PER_SAMPLE] for s in samples[:_PASS_ZERO_LLM_MAX_SAMPLES]]


def _run_pass_zero(project_dir, palace_dir, llm_provider) -> dict:
    """Pass 0: detect whether the corpus is AI-dialogue and persist the
    result to ``<palace>/.mempalace/origin.json``.

    Returns the wrapped result dict (same shape as origin.json) on success,
    or ``None`` when there are no readable samples to detect from. The
    return value is what cmd_init forwards to ``discover_entities`` via
    the ``corpus_origin`` kwarg.

    File-write failures (e.g. read-only palace) are caught and reported on
    stderr; init never blocks on them.
    """
    import json
    from datetime import datetime, timezone
    from pathlib import Path

    samples = _gather_origin_samples(project_dir)
    if not samples:
        print("  Skipping corpus-origin detection — no readable samples.")
        return None

    # Tier 1 — always runs. Cheap regex grep, no API.
    result = detect_origin_heuristic(samples)

    # Tier 2 — runs only when an LLM provider is available. The provider
    # contract is best-effort: corpus_origin internally falls back to a
    # conservative default on transport/parse failure, so we don't need a
    # try/except here, but we still keep one for any unforeseen exception.
    #
    # MERGE-FIELDS, NOT REPLACE: Tier 2's persona/user/platform extraction
    # is the whole reason to run it, but a weak local model (e.g. Ollama
    # gemma4:e4b) can return a wrong likely_ai_dialogue/confidence call
    # that overrides a confident heuristic answer. Per @igorls's review of
    # PR #1211: keep the heuristic's likely_ai_dialogue + confidence
    # (don't let a weak LLM flip a confident regex answer), and merge in
    # LLM's persona-related fields + combined evidence.
    if llm_provider is not None:
        try:
            llm_result = detect_origin_llm(_trim_samples_for_llm(samples), llm_provider)
            # Heuristic owns: likely_ai_dialogue, confidence (do NOT touch).
            # LLM contributes: primary_platform, user_name, agent_persona_names
            # (heuristic doesn't extract any of these).
            if llm_result.primary_platform:
                result.primary_platform = llm_result.primary_platform
            if llm_result.user_name:
                result.user_name = llm_result.user_name
            if llm_result.agent_persona_names:
                result.agent_persona_names = list(llm_result.agent_persona_names)
            # Combine evidence — keep both signal trails for the audit record,
            # prefixed so the on-disk origin.json says which tier produced
            # each entry. Idempotent: re-prefixing an already-tagged entry
            # is a no-op.
            tier1_prefix = "Tier-1 heuristic: "
            tier2_prefix = "Tier-2 LLM: "
            heuristic_evidence = [
                s if s.startswith(tier1_prefix) else f"{tier1_prefix}{s}"
                for s in (str(e) for e in result.evidence)
            ]
            llm_evidence = [
                s if s.startswith(tier2_prefix) else f"{tier2_prefix}{s}"
                for s in (str(e) for e in llm_result.evidence)
            ]
            result.evidence = heuristic_evidence + llm_evidence
        except Exception as exc:  # noqa: BLE001 — never block init on LLM failure
            print(f"  LLM corpus-origin tier failed ({exc}); using heuristic only.")

    wrapped = {
        "schema_version": 1,
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "result": result.to_dict(),
    }

    origin_path = Path(palace_dir).expanduser() / ".mempalace" / "origin.json"
    try:
        origin_path.parent.mkdir(parents=True, exist_ok=True)
        with open(origin_path, "w", encoding="utf-8") as f:
            json.dump(wrapped, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        print(f"  Could not write {origin_path}: {exc}", file=sys.stderr)
        # Return the wrapped dict anyway so the in-memory pipeline still
        # benefits from the detection result this run.
        return wrapped

    # Banner — one line, two-space indent matching existing init style.
    res = result
    if res.likely_ai_dialogue:
        platform = res.primary_platform or "AI dialogue (platform unidentified)"
        user = res.user_name or "—"
        agents = ", ".join(res.agent_persona_names) if res.agent_persona_names else "—"
        print(f"  Detected: {platform} (user: {user}, agents: {agents})")
    else:
        print(f"  Corpus origin: not AI-dialogue (confidence: {res.confidence:.2f})")

    return wrapped


def _ensure_mempalace_files_gitignored(project_dir) -> bool:
    """If project_dir is a git repo, ensure MemPalace's per-project files
    are listed in .gitignore so they don't get committed by accident.

    Returns True if .gitignore was updated, False otherwise. Issue #185:
    `mempalace init` writes mempalace.yaml + entities.json into the
    project root, where they previously had no protection against being
    staged into git.
    """
    from pathlib import Path

    project_path = Path(project_dir).expanduser().resolve()
    if not (project_path / ".git").exists():
        return False
    gitignore = project_path / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""
    existing_lines = {line.strip() for line in existing.splitlines()}
    missing = [p for p in _MEMPALACE_PROJECT_FILES if p not in existing_lines]
    if not missing:
        return False
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    block = prefix + "\n# MemPalace per-project files (issue #185)\n" + "\n".join(missing) + "\n"
    with open(gitignore, "a") as f:
        f.write(block)
    print(f"  Added {', '.join(missing)} to {gitignore.name}")
    return True


def cmd_init(args):
    import json
    from pathlib import Path
    from .entity_detector import confirm_entities
    from .project_scanner import discover_entities
    from .room_detector_local import detect_rooms_local

    # Honor --palace (issue #1313): without this, init silently ignored the
    # flag and always used ~/.mempalace. Mirror the env-var pattern used by
    # mcp_server.py so every downstream read of ``cfg.palace_path`` (Pass 0,
    # cfg.init(), the post-init mine) routes to the user-specified location.
    if getattr(args, "palace", None):
        os.environ["MEMPALACE_PALACE_PATH"] = os.path.abspath(os.path.expanduser(args.palace))

    cfg = MempalaceConfig()

    # Resolve entity-detection languages: --lang overrides config.
    lang_arg = getattr(args, "lang", None)
    if lang_arg:
        languages = [s.strip() for s in lang_arg.split(",") if s.strip()] or ["en"]
        cfg.set_entity_languages(languages)
    else:
        languages = cfg.entity_languages
    languages_tuple = tuple(languages)

    # --llm is ON by default. --no-llm is the explicit opt-out. Provider
    # precedence is unchanged (Ollama localhost first, then openai-compat,
    # then anthropic). Never block init on a missing LLM: when no provider
    # responds, print a one-line message pointing at --no-llm and fall
    # through to heuristics-only.
    llm_provider = None
    if not getattr(args, "no_llm", False):
        provider_name = getattr(args, "llm_provider", "ollama") or "ollama"
        provider_model = getattr(args, "llm_model", "gemma4:e4b") or "gemma4:e4b"
        try:
            candidate = get_provider(
                name=provider_name,
                model=provider_model,
                endpoint=getattr(args, "llm_endpoint", None),
                api_key=getattr(args, "llm_api_key", None),
            )
            ok, msg = candidate.check_available()
            if ok:
                llm_provider = candidate
                print(f"  LLM enabled: {provider_name}/{provider_model}")
                # Privacy warning (issue #24): if the configured endpoint
                # sends data off the user's machine/network, surface that
                # before init proceeds. URL-based — Ollama on localhost,
                # LM Studio on LAN, etc. won't trigger; Anthropic /
                # cloud OpenAI-compat / any non-local endpoint will.
                if candidate.is_external_service:
                    print(
                        f"  ⚠ {provider_name} is an EXTERNAL API. Your folder "
                        f"content will be sent to the provider during init. "
                        f"MemPalace does not control how the provider logs, "
                        f"retains, or uses your data. Pass --no-llm to keep "
                        f"init fully local."
                    )
                    # Consent gate (issue #26): block init when the api_key
                    # was acquired via env-fallback (stray credential in
                    # shell env). Explicit --llm-api-key (api_key_source ==
                    # "flag") means the user already opted in.
                    # --accept-external-llm bypasses for CI / non-interactive.
                    api_key_source = getattr(candidate, "api_key_source", None)
                    accept_flag = getattr(args, "accept_external_llm", False)
                    if api_key_source == "env" and not accept_flag:
                        try:
                            answer = (
                                input(
                                    "  Your API key was loaded from the environment "
                                    "(not passed via --llm-api-key). Continue with "
                                    "external LLM? [y/N] "
                                )
                                .strip()
                                .lower()
                            )
                        except EOFError:
                            answer = ""
                        if answer != "y":
                            print(
                                "  Declined — falling back to heuristics-only. "
                                "Pass --llm-api-key explicitly or "
                                "--accept-external-llm to skip this prompt."
                            )
                            llm_provider = None
            else:
                print(
                    f"  No LLM provider reachable ({msg}). "
                    f"Running heuristics-only — pass --no-llm to silence this."
                )
        except LLMError as e:
            print(
                f"  LLM init failed ({e}). Running heuristics-only — pass --no-llm to silence this."
            )

    # Pass 0: detect whether the corpus is AI-dialogue. Writes
    # <palace>/.mempalace/origin.json and supplies corpus context to the
    # entity classifier so it can correctly handle agent persona names
    # (e.g. "Echo", "Sparrow") without misclassifying them as people.
    corpus_origin = _run_pass_zero(
        project_dir=args.dir,
        palace_dir=cfg.palace_path,
        llm_provider=llm_provider,
    )

    # Pass 1: discover entities — manifests + git authors first, prose detection
    # as supplement for names mentioned only in docs/notes. Optional phase-2
    # LLM refinement runs inside discover_entities when llm_provider is given.
    print(f"\n  Scanning for entities in: {args.dir}")
    if languages_tuple != ("en",):
        print(f"  Languages: {', '.join(languages_tuple)}")
    detected = discover_entities(
        args.dir,
        languages=languages_tuple,
        llm_provider=llm_provider,
        corpus_origin=corpus_origin,
    )
    total = (
        len(detected["people"])
        + len(detected["projects"])
        + len(detected.get("topics", []))
        + len(detected["uncertain"])
    )
    if total > 0:
        confirmed = confirm_entities(detected, yes=getattr(args, "yes", False))
        # Save confirmed entities to <project>/entities.json (per-project
        # audit trail — user can inspect or hand-edit) AND merge into the
        # global registry the miner reads at mine time. Topics are kept
        # separately so the miner can later compute cross-wing tunnels
        # from shared topics (see palace_graph.compute_topic_tunnels).
        if confirmed["people"] or confirmed["projects"] or confirmed.get("topics"):
            project_path = Path(args.dir).expanduser().resolve()
            entities_path = project_path / "entities.json"
            with open(entities_path, "w", encoding="utf-8") as f:
                json.dump(confirmed, f, indent=2, ensure_ascii=False)
            print(f"  Entities saved: {entities_path}")

            from .config import normalize_wing_name
            from .miner import add_to_known_entities

            # Match the slug ``room_detector_local`` writes into
            # ``mempalace.yaml`` so the miner's tunnel lookup hits the
            # same key in ``topics_by_wing`` at mine time (issue #1194 —
            # without this, hyphenated dirnames silently lose tunnels).
            wing = normalize_wing_name(project_path.name)
            registry_path = add_to_known_entities(confirmed, wing=wing)
            print(f"  Registry updated: {registry_path}")
    else:
        print("  No entities detected — proceeding with directory-based rooms.")

    # Pass 2: detect rooms from folder structure
    detect_rooms_local(project_dir=args.dir, yes=getattr(args, "yes", False))
    cfg.init()

    # Pass 3: protect git repos from accidentally committing per-project files
    _ensure_mempalace_files_gitignored(args.dir)

    # Pass 4: offer to run mine immediately. The directory just had its
    # rooms + entities set up, so 99% of users will mine next anyway —
    # asking here removes the "remember to type the next command" friction.
    # `--auto-mine` skips the prompt and mines automatically; `--yes` is
    # SCOPED to entity auto-accept and does NOT imply mining.
    _maybe_run_mine_after_init(args, cfg)


def _format_size_mb(num_bytes: int) -> str:
    """Render a byte count as a human-readable size for the mine estimate.

    < 1 MB rounds up to ``<1 MB`` so users never see a misleading ``0 MB``
    on small projects. Otherwise reports an integer megabyte count.
    """
    if num_bytes <= 0:
        return "<1 MB"
    mb = num_bytes / (1024 * 1024)
    if mb < 1:
        return "<1 MB"
    return f"{mb:.0f} MB"


def _maybe_run_mine_after_init(args, cfg) -> None:
    """Prompt the user to mine the directory just initialised, or auto-mine
    when ``--auto-mine`` was passed. Extracted so the prompt path is
    unit-testable.

    Behaviour matrix:

    - default (no flags) — prompt, default Yes, mine in-process if accepted
    - ``--yes`` — entity auto-accept only; STILL prompts for the mine step
    - ``--auto-mine`` — skip the mine prompt and mine directly
    - ``--yes --auto-mine`` — fully non-interactive

    Mine errors are surfaced (not swallowed): a failing mine exits with a
    non-zero status via :func:`sys.exit` so downstream scripts can see it.
    The pre-scan that produces the file-count estimate is reused as the
    mine input so we never walk the corpus twice.
    """
    from .miner import mine, scan_project

    project_dir = args.dir
    auto_mine = bool(getattr(args, "auto_mine", False))

    # Single corpus walk: this scan feeds BOTH the "what would be mined"
    # estimate the user sees in the prompt AND the file list mine() will
    # process. We pass the result into mine() via the `files` kwarg so it
    # doesn't re-walk the tree.
    try:
        scanned_files = scan_project(project_dir)
        file_count = len(scanned_files)
        total_bytes = 0
        for fp in scanned_files:
            try:
                total_bytes += fp.stat().st_size
            except OSError:
                # Skip files that vanished between scan and stat — mine()
                # will skip them too.
                continue
        size_str = _format_size_mb(total_bytes)
    except Exception:
        scanned_files = None
        file_count = None
        size_str = None

    # Show the scope estimate BEFORE the prompt so the user knows what
    # they are agreeing to. On a real corpus mine takes minutes; hitting
    # Enter on a default-Y prompt with no size cue is a footgun.
    if isinstance(file_count, int):
        if size_str:
            print(f"  ~{file_count} files (~{size_str}) would be mined into this palace.\n")
        else:
            print(f"  ~{file_count} files would be mined into this palace.\n")

    if not auto_mine:
        try:
            answer = input("  Mine this directory now? [Y/n] ").strip().lower()
        except EOFError:
            # Non-interactive stdin (e.g. piped) — treat like decline so
            # we don't block. User can re-run with --auto-mine to opt in.
            answer = "n"
        if answer not in ("", "y", "yes"):
            print(f"\n  Skipped. Run `mempalace mine {shlex.quote(project_dir)}` when ready.")
            return

    palace_path = cfg.palace_path
    try:
        mine(
            project_dir=project_dir,
            palace_path=palace_path,
            files=scanned_files,
        )
    except KeyboardInterrupt:
        # mine() handles its own SIGINT summary + sys.exit(130); re-raise
        # any KeyboardInterrupt that escapes (shouldn't happen) so the
        # shell still sees a clean interrupt rather than a swallowed one.
        raise
    except Exception as e:
        print(f"\n  ERROR: mine failed: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_mine(args):
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    plan_out = getattr(args, "plan_out", None)
    plan_progress_jsonl = getattr(args, "plan_progress_jsonl", None)
    manifest_path = getattr(args, "manifest", None)
    start_index = getattr(args, "start_index", None)
    progress_jsonl = getattr(args, "progress_jsonl", None)
    include_ignored = []
    for raw in args.include_ignored or []:
        include_ignored.extend(part.strip() for part in raw.split(",") if part.strip())

    # --redetect-origin re-runs corpus_origin on the current corpus state
    # and overwrites <palace>/.mempalace/origin.json before mining proceeds.
    # Heuristic-only by design — full LLM detection lives on `mempalace init`.
    if getattr(args, "redetect_origin", False):
        _run_pass_zero(
            project_dir=args.dir,
            palace_dir=palace_path,
            llm_provider=None,
        )

    if args.mode == "convos":
        if any(
            value is not None
            for value in (
                plan_out,
                plan_progress_jsonl,
                manifest_path,
                start_index,
                progress_jsonl,
            )
        ):
            print(
                "  ERROR: deterministic source manifests currently support projects mode only",
                file=sys.stderr,
            )
            sys.exit(2)
        from .convo_miner import mine_convos
        from .miner import MINE_LOCK_CONFLICT_EXIT_CODE
        from .palace import MineAlreadyRunning

        try:
            mine_convos(
                convo_dir=args.dir,
                palace_path=palace_path,
                wing=args.wing,
                agent=args.agent,
                limit=args.limit,
                dry_run=args.dry_run,
                extract_mode=args.extract,
                raise_on_lock_conflict=True,
            )
        except MineAlreadyRunning:
            print(
                "mempalace: another mine already holds the requested palace; retry later.",
                file=sys.stderr,
            )
            sys.exit(MINE_LOCK_CONFLICT_EXIT_CODE)
    else:
        from .miner import MINE_LOCK_CONFLICT_EXIT_CODE, mine
        from .palace import MineAlreadyRunning

        try:
            mine(
                project_dir=args.dir,
                palace_path=palace_path,
                wing_override=args.wing,
                agent=args.agent,
                limit=args.limit,
                dry_run=args.dry_run,
                respect_gitignore=not args.no_gitignore,
                include_ignored=include_ignored,
                plan_out=plan_out,
                plan_progress_jsonl=plan_progress_jsonl,
                manifest_path=manifest_path,
                start_index=start_index,
                progress_jsonl=progress_jsonl,
                raise_on_lock_conflict=True,
                report_variants=not getattr(args, "no_variant_report", False),
            )
        except MineAlreadyRunning:
            print(
                "mempalace: another mine already holds the requested palace; retry later.",
                file=sys.stderr,
            )
            sys.exit(MINE_LOCK_CONFLICT_EXIT_CODE)


def cmd_variants(args):
    """Report backup/variant directory candidates for operator confirmation.

    Read-only and report-only by design (#36): this command never excludes,
    deletes, or mines anything. A directory named ``backup`` can hold the
    only surviving copy of something, so the decision stays with the
    operator — confirmed names go into ``exclude.dirs`` in ``mempalace.yaml``.
    """
    from .mine_exclusions import (
        detect_variant_directories,
        format_variant_report,
        load_variant_settings,
        read_project_config,
        resolve_exclusion_policy,
    )
    from .miner import SKIP_FILENAMES

    target = os.path.expanduser(args.dir)
    if not os.path.isdir(target):
        print(f"ERROR: Directory not found: {target}", file=sys.stderr)
        sys.exit(2)

    config = read_project_config(target)
    settings = load_variant_settings(config)
    max_depth = args.max_depth if args.max_depth is not None else settings["max_depth"]
    if max_depth < 1:
        print("ERROR: --max-depth must be at least 1", file=sys.stderr)
        sys.exit(2)

    candidates = detect_variant_directories(
        target,
        max_depth=max_depth,
        policy=resolve_exclusion_policy(target, artifact_files=SKIP_FILENAMES),
        globs=settings["globs"],
    )

    if args.json:
        print(
            json.dumps(
                {
                    "schema": "mempalace-variant-candidates/v1",
                    "root": os.path.abspath(target),
                    "max_depth": max_depth,
                    "applied": False,
                    "candidate_count": len(candidates),
                    "candidates": [candidate.to_dict() for candidate in candidates],
                },
                indent=2,
            )
        )
        return

    for line in format_variant_report(candidates):
        print(line)


def cmd_exclusions(args):
    """Print the effective mine-time exclusion policy for a directory."""
    from .mine_exclusions import read_project_config
    from .miner import resolve_mine_exclusion_policy

    target = os.path.expanduser(args.dir)
    if not os.path.isdir(target):
        print(f"ERROR: Directory not found: {target}", file=sys.stderr)
        sys.exit(2)

    config = read_project_config(target)
    policy = resolve_mine_exclusion_policy(target, config)
    effective = policy.as_dict()

    if args.json:
        print(
            json.dumps(
                {
                    "root": os.path.abspath(target),
                    "configured": bool(config.get("exclude")),
                    "digest": policy.digest(),
                    "policy": effective,
                },
                indent=2,
            )
        )
        return

    print(f"\n  Effective mine exclusions for {os.path.abspath(target)}")
    print(f"  Source: {'mempalace.yaml exclude: block' if config.get('exclude') else 'defaults'}")
    print(f"  Digest: {policy.digest()}")
    for key in (
        "generated_files",
        "generated_file_globs",
        "generated_dirs",
        "generated_dir_globs",
        "extra_files",
        "extra_file_globs",
        "extra_dirs",
        "extra_dir_globs",
        "allow_files",
        "allow_dirs",
        "artifact_files",
    ):
        values = effective.get(key) or []
        if values:
            print(f"\n  {key} ({len(values)}):")
            print(f"    {', '.join(values)}")
    print(
        "\n  Reverse any default in mempalace.yaml — set `exclude.generated_files: false`\n"
        "  or list individual names under `exclude.allow_files` / `exclude.allow_dirs`.\n"
    )


def cmd_sweep(args):
    """Sweep a transcript file or directory.

    Each JSONL file replaces its complete message-level representation under
    a managed receipt. The sweeper uses a separate source lane from the
    file-level miners, so their chunked rows can coexist without either path
    claiming the other's provenance.
    """
    from .sweeper import sweep, sweep_directory

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    target = os.path.expanduser(args.target)
    allow_zero_output = bool(getattr(args, "allow_zero_output", False))

    if os.path.isfile(target):
        result = sweep(target, palace_path, allow_zero_output=allow_zero_output)
        print(
            f"  Swept {target}: +{result['drawers_added']} new, "
            f"~{result['drawers_updated']} updated, "
            f"={result['drawers_semantically_unchanged']} semantically unchanged, "
            f"-{result['drawers_removed']} removed, "
            f"{result['drawers_rebound']} receipt rebindings, "
            f"{result['drawers_physical_mutations']} physical mutations; "
            f"{result['drawers_represented']}/{result['drawers_expected']} "
            "represented/expected; "
            f"receipt {result['receipt_id']} ({result['verification_status']})."
        )
        if result["verification_status"] != "represented":
            print(
                "  WARNING: the sweep committed, but terminal verification or recovery "
                "finalization is incomplete: "
                f"{result['verification_error']}",
                file=sys.stderr,
            )
            sys.exit(3)
    elif os.path.isdir(target):
        result = sweep_directory(
            target,
            palace_path,
            allow_zero_output=allow_zero_output,
        )
        print(
            f"  Swept {result['files_succeeded']}/{result['files_attempted']} "
            f"files from {target}: +{result['drawers_added']} new, "
            f"~{result['drawers_updated']} updated, "
            f"={result['drawers_semantically_unchanged']} semantically unchanged, "
            f"-{result['drawers_removed']} removed, "
            f"{result['drawers_rebound']} receipt rebindings, "
            f"{result['drawers_physical_mutations']} physical mutations, "
            f"{result['drawers_represented']} whole-run represented, "
            f"{result['drawers_verifier_confirmed']}/{result['drawers_expected']} "
            "per-file verifier-confirmed/expected."
        )
        failures = result.get("failures") or []
        if failures:
            print(
                f"  WARNING: {len(failures)} file(s) failed to sweep - see stderr / logs for details.",
                file=sys.stderr,
            )
            sys.exit(2)
        if result.get("files_committed_unverified"):
            print(
                "  WARNING: one or more files committed without complete terminal "
                "verification/finalization; inspect per-file verification_error values.",
                file=sys.stderr,
            )
            sys.exit(3)
    else:
        print(f"  ERROR: Not a file or directory: {target}", file=sys.stderr)
        sys.exit(1)


def cmd_search(args):
    from .searcher import search, SearchError

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    try:
        search(
            query=args.query,
            palace_path=palace_path,
            wing=args.wing,
            room=args.room,
            n_results=args.results,
        )
    except SearchError:
        sys.exit(1)


def cmd_wakeup(args):
    """Show L0 (identity) + L1 (essential story) — the wake-up context."""
    from .layers import MemoryStack

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    stack = MemoryStack(palace_path=palace_path)

    text = stack.wake_up(wing=args.wing)
    tokens = len(text) // 4
    print(f"Wake-up text (~{tokens} tokens):")
    print("=" * 50)
    print(text)


def cmd_split(args):
    """Split concatenated transcript mega-files into per-session files."""
    from .split_mega_files import main as split_main
    import sys

    # Rebuild argv for split_mega_files argparse
    # Expand ~ and resolve to absolute path so split_mega_files sees a real path
    argv = ["--source", str(Path(args.dir).expanduser().resolve())]
    if args.output_dir:
        argv += ["--output-dir", args.output_dir]
    if args.dry_run:
        argv.append("--dry-run")
    if args.min_sessions != 2:
        argv += ["--min-sessions", str(args.min_sessions)]

    old_argv = sys.argv
    sys.argv = ["mempalace split"] + argv
    try:
        split_main()
    finally:
        sys.argv = old_argv


def cmd_migrate(args):
    """Migrate palace from a different ChromaDB version."""
    from .migrate import migrate

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    migrate(
        palace_path=palace_path,
        dry_run=args.dry_run,
        confirm=getattr(args, "yes", False),
    )


def cmd_status(args):
    from .status import status

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    status(palace_path=palace_path)


def cmd_repair_status(args):
    """Read-only HNSW capacity health check (#1222, #18)."""
    from .repair import status as repair_status

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    repair_status(
        palace_path=palace_path,
        as_json=getattr(args, "json", False),
        artifact_dir=getattr(args, "artifact_dir", None),
    )


def cmd_backup_snapshot(args):
    """Clean-client lease + staged consistent snapshot + content identity (#33).

    Recursively tarring a live palace is not crash-consistent: ``chroma.sqlite3``
    is a multi-gigabyte live SQLite database and the HNSW segment files are
    mutated as a group. This command is the safe replacement -- it holds the
    exclusive palace lock every miner and MCP managed write already honors,
    copies the catalog with SQLite's online backup API, copies only the segment
    directories the snapshot's own catalog references, and emits a verifiable
    content-identity receipt. It never mutates the palace.
    """
    import json

    from .backup_snapshot import PalaceSnapshotError, stage_palace_snapshot, verify_snapshot_receipt

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    as_json = getattr(args, "json", False)

    try:
        if getattr(args, "verify_receipt", None):
            result = verify_snapshot_receipt(
                args.verify_receipt,
                staged_root=getattr(args, "staged_root", None) or None,
                verify_hashes=not getattr(args, "skip_hash_verification", False),
            )
            if as_json:
                print(json.dumps(result, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
            else:
                print(f"MemPalace snapshot verification: valid={result['valid']}")
                print(f"  Files: {result['fileCount']}  hashed={result['hashesVerified']}")
                for problem in result["problems"]:
                    print(f"  PROBLEM {problem}")
            if not result["valid"]:
                sys.exit(2)
            return

        if not getattr(args, "staging_dir", None):
            print(
                "backup-snapshot requires --staging-dir (an empty directory outside the palace)",
                file=sys.stderr,
            )
            sys.exit(2)

        receipt = stage_palace_snapshot(
            palace_path,
            args.staging_dir,
            use_maintenance_marker=not getattr(args, "no_maintenance_marker", False),
            progress=getattr(args, "progress", False),
        )
    except PalaceSnapshotError as exc:
        if as_json:
            print(
                json.dumps(
                    {
                        "schema": "mempalace-backup-snapshot-receipt/v1",
                        "status": "error",
                        "leaseProven": False,
                        "snapshotConsistencyProven": False,
                        "contentIdentityProven": False,
                        "message": str(exc),
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        else:
            print(f"MemPalace snapshot failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if as_json:
        print(json.dumps(receipt, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
        return
    identity = receipt.get("contentIdentity", {})
    print("MemPalace staged backup snapshot: complete")
    print(f"  Staged root:  {receipt['stagedRoot']}")
    print(f"  Receipt:      {receipt['receiptPath']}")
    print(f"  Files staged: {identity.get('fileCount')}")
    print(f"  Bytes staged: {identity.get('totalBytes'):,}")
    for label, entry in sorted((identity.get("counts") or {}).items()):
        print(f"  {label:<12} sqlite={entry.get('sqliteCount')} hnsw={entry.get('hnswCount')}")
    print(f"  Identity:     {receipt['contentIdentityDigest']}")
    print(f"  Duration:     {receipt['durationSeconds']}s")


def cmd_warm(args):
    """Pre-warm the vector search stack (embedding model + HNSW + any pending
    post-mutation work), so the next reader pays seconds, not minutes.

    Bulk mutations (dedup --apply, sqlite-replay) can leave the palace in a
    state where the FIRST subsequent open does heavy one-time work — measured
    at 1,004s after a 42,606-drawer dedup on 2026-07-06 (vs 4.6s once warm).
    Running `mempalace warm` right after any bulk mutation moves that cost to
    mutation time instead of ambushing the next bridge start or agent query.
    See iMelki/mempalace#19 and iMelki/agent-settings#209.
    """
    import json as _json
    import time as _time

    from .searcher import search_memories

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    as_json = getattr(args, "json", False)
    if not as_json:
        print(f"Warming palace at {palace_path} (model + HNSW + pending work)...")
    t0 = _time.time()
    out = search_memories("warmup", palace_path, n_results=1)
    elapsed = round(_time.time() - t0, 1)
    error = out.get("error") if isinstance(out, dict) else f"unexpected result type {type(out)}"
    payload = {
        "schema": "mempalace.warm.v1",
        "palace_path": palace_path,
        "warm_seconds": elapsed,
        "ok": error is None,
        "error": error,
    }
    if as_json:
        print(_json.dumps(payload))
    elif error:
        print(f"Warm FAILED after {elapsed}s: {error}")
    else:
        print(f"Palace warm in {elapsed}s.")
    if error:
        sys.exit(1)


def cmd_repair(args):
    """Rebuild palace vector index from SQLite metadata."""
    import shutil
    from .migrate import confirm_destructive_action, contains_palace_database
    from .repair import TruncationDetected, check_extraction_safety

    palace_path = os.path.abspath(
        os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    )

    if getattr(args, "mode", "legacy") == "max-seq-id":
        from .repair import repair_max_seq_id

        repair_max_seq_id(
            palace_path,
            segment=getattr(args, "segment", None),
            from_sidecar=getattr(args, "from_sidecar", None),
            backup=getattr(args, "backup", True),
            dry_run=getattr(args, "dry_run", False),
            assume_yes=getattr(args, "yes", False),
        )
        return

    if getattr(args, "mode", "legacy") == "sqlite-replay":
        from .repair import repair_sqlite_replay

        kwargs = {
            "dry_run": getattr(args, "dry_run", False),
            "assume_yes": getattr(args, "yes", False),
            "backup": getattr(args, "backup", True),
            "batch_size": getattr(args, "batch_size", 1000),
            "confirm_large_reembed": getattr(args, "confirm_large_reembed", False),
            "max_rows": getattr(args, "max_rows", None),
            "max_batches": getattr(args, "max_batches", None),
            "artifact_dir": getattr(args, "artifact_dir", None),
        }
        if getattr(args, "json", False):
            import contextlib
            import io
            import json

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = repair_sqlite_replay(palace_path, **kwargs)
            payload = dict(result)
            payload["stdout"] = stdout.getvalue().splitlines()
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            repair_sqlite_replay(palace_path, **kwargs)
        return

    db_path = os.path.join(palace_path, "chroma.sqlite3")

    if not os.path.isdir(palace_path):
        print(f"\n  No palace found at {palace_path}")
        return
    if not contains_palace_database(palace_path):
        print(f"\n  No palace database found at {db_path}")
        return

    print(f"\n{'=' * 55}")
    print("  MemPalace Repair")
    print(f"{'=' * 55}\n")
    print(f"  Palace: {palace_path}")

    from .backends.chroma import ChromaBackend

    backend = ChromaBackend()

    # Try to read existing drawers
    try:
        col = backend.get_collection(palace_path, "mempalace_drawers")
        total = col.count()
        print(f"  Drawers found: {total}")
    except Exception as e:
        print(f"  Error reading palace: {e}")
        print("  Cannot recover — palace may need to be re-mined from source files.")
        return

    if total == 0:
        print("  Nothing to repair.")
        return

    if not confirm_destructive_action(
        "Repair", palace_path, assume_yes=getattr(args, "yes", False)
    ):
        return

    # Extract all drawers in batches
    print("\n  Extracting drawers...")
    batch_size = 5000
    all_ids = []
    all_docs = []
    all_metas = []
    offset = 0
    while offset < total:
        batch = col.get(limit=batch_size, offset=offset, include=["documents", "metadatas"])
        if not batch["ids"]:
            break
        all_ids.extend(batch["ids"])
        all_docs.extend(batch["documents"])
        all_metas.extend(batch["metadatas"])
        offset += len(batch["ids"])
    print(f"  Extracted {len(all_ids)} drawers")

    # ── #1208 guard ──────────────────────────────────────────────────
    # Cross-check against the SQLite ground truth before doing anything
    # destructive. Catches the user-reported case where chromadb's
    # collection-layer get() silently caps at 10,000 rows even on much
    # larger palaces (e.g. after manual HNSW quarantine). Override with
    # --confirm-truncation-ok only after independently verifying the
    # extraction count is real.
    try:
        check_extraction_safety(
            palace_path,
            len(all_ids),
            confirm_truncation_ok=getattr(args, "confirm_truncation_ok", False),
        )
    except TruncationDetected as e:
        print(e.message)
        return

    # Backup and rebuild
    palace_path = os.path.normpath(palace_path)
    backup_path = palace_path + ".backup"
    if os.path.exists(backup_path):
        if not contains_palace_database(backup_path):
            print(
                "  Backup validation failed: backup path exists but does not contain chroma.sqlite3. "
                f"Please remove or rename: {backup_path}"
            )
            return
        shutil.rmtree(backup_path)
    print(f"  Backing up to {backup_path}...")
    shutil.copytree(palace_path, backup_path)

    print("  Rebuilding collection...")
    backend.delete_collection(palace_path, "mempalace_drawers")
    new_col = backend.create_collection(palace_path, "mempalace_drawers")

    filed = 0
    for i in range(0, len(all_ids), batch_size):
        batch_ids = all_ids[i : i + batch_size]
        batch_docs = all_docs[i : i + batch_size]
        batch_metas = all_metas[i : i + batch_size]
        new_col.add(documents=batch_docs, ids=batch_ids, metadatas=batch_metas)
        filed += len(batch_ids)
        print(f"  Re-filed {filed}/{len(all_ids)} drawers...")

    print(f"\n  Repair complete. {filed} drawers rebuilt.")
    print(f"  Backup saved at {backup_path}")
    print(f"\n{'=' * 55}\n")


def cmd_hook(args):
    """Run hook logic: reads JSON from stdin, outputs JSON to stdout."""
    from .hooks_cli import run_hook

    run_hook(hook_name=args.hook, harness=args.harness)


def cmd_instructions(args):
    """Output skill instructions to stdout."""
    from .instructions_cli import run_instructions

    run_instructions(name=args.name)


def cmd_mcp(args):
    """Show how to wire MemPalace into MCP-capable hosts."""
    base_server_cmd = "mempalace-mcp"

    if args.palace:
        resolved_palace = str(Path(args.palace).expanduser())
        server_cmd = f"{base_server_cmd} --palace {shlex.quote(resolved_palace)}"
    else:
        server_cmd = base_server_cmd

    print("MemPalace MCP quick setup:")
    print(f"  claude mcp add mempalace -- {server_cmd}")
    print("\nRun the server directly:")
    print(f"  {server_cmd}")

    if not args.palace:
        print("\nOptional custom palace:")
        print(f"  claude mcp add mempalace -- {base_server_cmd} --palace /path/to/palace")
        print(f"  {base_server_cmd} --palace /path/to/palace")


def cmd_compress(args):
    """Compress drawers in a wing using AAAK Dialect."""
    from .backends.chroma import ChromaBackend
    from .dialect import Dialect

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path

    # Load dialect (with optional entity config)
    config_path = args.config
    if not config_path:
        for candidate in ["entities.json", os.path.join(palace_path, "entities.json")]:
            if os.path.exists(candidate):
                config_path = candidate
                break

    if config_path and os.path.exists(config_path):
        dialect = Dialect.from_config(config_path)
        print(f"  Loaded entity config: {config_path}")
    else:
        dialect = Dialect()

    # Connect to palace
    backend = ChromaBackend()
    try:
        col = backend.get_collection(palace_path, "mempalace_drawers")
    except Exception:
        print(f"\n  No palace found at {palace_path}")
        print("  Run: mempalace init <dir> then mempalace mine <dir>")
        sys.exit(1)

    # Query drawers in batches to avoid SQLite variable limit (~999)
    where = {"wing": args.wing} if args.wing else None
    _BATCH = 500
    docs, metas, ids = [], [], []
    offset = 0
    while True:
        try:
            kwargs = {
                "include": ["documents", "metadatas"],
                "limit": _BATCH,
                "offset": offset,
            }
            if where:
                kwargs["where"] = where
            batch = col.get(**kwargs)
        except Exception as e:
            if not docs:
                print(f"\n  Error reading drawers: {e}")
                sys.exit(1)
            break
        batch_docs = batch.get("documents", [])
        if not batch_docs:
            break
        docs.extend(batch_docs)
        metas.extend(batch.get("metadatas", []))
        ids.extend(batch.get("ids", []))
        offset += len(batch_docs)
        if len(batch_docs) < _BATCH:
            break

    if not docs:
        wing_label = f" in wing '{args.wing}'" if args.wing else ""
        print(f"\n  No drawers found{wing_label}.")
        return

    print(
        f"\n  Compressing {len(docs)} drawers"
        + (f" in wing '{args.wing}'" if args.wing else "")
        + "..."
    )
    print()

    total_original = 0
    total_compressed = 0
    compressed_entries = []

    for doc, meta, doc_id in zip(docs, metas, ids):
        compressed = dialect.compress(doc, metadata=meta)
        stats = dialect.compression_stats(doc, compressed)

        total_original += stats["original_chars"]
        total_compressed += stats["summary_chars"]

        compressed_entries.append((doc_id, compressed, meta, stats))

        if args.dry_run:
            wing_name = meta.get("wing", "?")
            room_name = meta.get("room", "?")
            source = Path(meta.get("source_file", "?")).name
            print(f"  [{wing_name}/{room_name}] {source}")
            print(
                f"    {stats['original_tokens_est']}t -> {stats['summary_tokens_est']}t ({stats['size_ratio']:.1f}x)"
            )
            print(f"    {compressed}")
            print()

    # Store compressed versions (unless dry-run)
    if not args.dry_run:
        try:
            comp_col = backend.get_or_create_collection(palace_path, "mempalace_closets")
            for doc_id, compressed, meta, stats in compressed_entries:
                comp_meta = dict(meta)
                comp_meta["compression_ratio"] = round(stats["size_ratio"], 1)
                comp_meta["original_tokens"] = stats["original_tokens_est"]
                comp_col.upsert(
                    ids=[doc_id],
                    documents=[compressed],
                    metadatas=[comp_meta],
                )
            print(
                f"  Stored {len(compressed_entries)} compressed drawers in 'mempalace_closets' collection."
            )
        except Exception as e:
            print(f"  Error storing compressed drawers: {e}")
            sys.exit(1)

    # Summary
    ratio = total_original / max(total_compressed, 1)
    # Estimate tokens from char count (~3.8 chars/token for English text)
    orig_tokens = max(1, int(total_original / 3.8))
    comp_tokens = max(1, int(total_compressed / 3.8))
    print(f"  Total: {orig_tokens:,}t -> {comp_tokens:,}t ({ratio:.1f}x compression)")
    if args.dry_run:
        print("  (dry run -- nothing stored)")


def main():
    version_label = f"MemPalace {__version__}"
    parser = argparse.ArgumentParser(
        description="MemPalace — Give your AI a memory. No API key required.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"{version_label}\n\n{__doc__}",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=version_label,
        help="Show version and exit",
    )
    parser.add_argument(
        "--palace",
        default=None,
        help="Where the palace lives (default: from ~/.mempalace/config.json or ~/.mempalace/palace)",
    )

    sub = parser.add_subparsers(dest="command")

    # init
    p_init = sub.add_parser("init", help="Detect rooms from your folder structure")
    p_init.add_argument("dir", help="Project directory to set up")
    p_init.add_argument(
        "--yes",
        action="store_true",
        help="Auto-accept all detected entities (non-interactive)",
    )
    p_init.add_argument(
        "--auto-mine",
        action="store_true",
        help=(
            "Skip the post-init mine prompt and run mine automatically. "
            "Combine with --yes for a fully non-interactive setup."
        ),
    )
    p_init.add_argument(
        "--lang",
        default=None,
        help=(
            "Comma-separated language codes for entity detection "
            "(e.g. 'en' or 'en,pt-br'). Defaults to value from config "
            "(MEMPALACE_ENTITY_LANGUAGES env var or config.json), or 'en'. "
            "When given, the value is also persisted to config.json."
        ),
    )
    p_init.add_argument(
        "--llm",
        action="store_true",
        help=(
            "DEPRECATED — LLM-assisted entity refinement is now ON by default. "
            "This flag is preserved for backward compatibility; pass --no-llm "
            "to opt out instead."
        ),
    )
    p_init.add_argument(
        "--no-llm",
        action="store_true",
        help=(
            "Disable LLM-assisted entity refinement. Run init in heuristics-only "
            "mode (no provider acquisition, no LLM calls). Use when running "
            "without a local LLM and you don't want the graceful-fallback message."
        ),
    )
    p_init.add_argument(
        "--llm-provider",
        default="ollama",
        choices=["ollama", "openai-compat", "anthropic"],
        help="LLM provider (default: ollama). Pass --no-llm to disable LLM-assisted refinement entirely.",
    )
    p_init.add_argument(
        "--llm-model",
        default="gemma4:e4b",
        help="Model name for the chosen provider (default: gemma4:e4b for Ollama).",
    )
    p_init.add_argument(
        "--llm-endpoint",
        default=None,
        help=(
            "Provider endpoint URL. Default for Ollama: http://localhost:11434. "
            "Required for openai-compat."
        ),
    )
    p_init.add_argument(
        "--llm-api-key",
        default=None,
        help=(
            "API key for the provider. For anthropic, defaults to $ANTHROPIC_API_KEY; "
            "for openai-compat, defaults to $OPENAI_API_KEY."
        ),
    )
    p_init.add_argument(
        "--accept-external-llm",
        action="store_true",
        help=(
            "Bypass the interactive consent prompt that fires when an external "
            "LLM is configured via an environment-variable API key (issue #26). "
            "Use this in CI / non-interactive runs where you've already decided "
            "the external send is acceptable."
        ),
    )

    # mine
    p_mine = sub.add_parser("mine", help="Mine files into the palace")
    p_mine.add_argument("dir", help="Directory to mine")
    p_mine.add_argument(
        "--mode",
        choices=["projects", "convos"],
        default="projects",
        help="Ingest mode: 'projects' for code/docs (default), 'convos' for chat exports",
    )
    p_mine.add_argument("--wing", default=None, help="Wing name (default: directory name)")
    p_mine.add_argument(
        "--no-gitignore",
        action="store_true",
        help="Don't respect .gitignore files when scanning project files",
    )
    p_mine.add_argument(
        "--include-ignored",
        action="append",
        default=[],
        help="Always scan these project-relative paths even if ignored; repeat or pass comma-separated paths",
    )
    p_mine.add_argument(
        "--agent",
        default="mempalace",
        help="Your name — recorded on every drawer (default: mempalace)",
    )
    p_mine.add_argument("--limit", type=int, default=0, help="Max files to process (0 = all)")
    p_mine.add_argument(
        "--plan-out",
        default=None,
        help=(
            "Create or reuse an immutable deterministic project-source manifest "
            "at this path before mining"
        ),
    )
    p_mine.add_argument(
        "--plan-progress-jsonl",
        default=None,
        help=(
            "Persist a hash-chained directory/file cursor while building "
            "--plan-out so a killed planner resumes at the next exact file"
        ),
    )
    p_mine.add_argument(
        "--manifest",
        default=None,
        help="Consume an existing immutable project-source manifest",
    )
    p_mine.add_argument(
        "--start-index",
        type=int,
        default=None,
        help=(
            "Expected next zero-based source index; must equal the contiguous "
            "verified progress prefix"
        ),
    )
    p_mine.add_argument(
        "--progress-jsonl",
        default=None,
        help=(
            "Append a sanitized durable cursor after each source receipt is "
            "terminal and exactly represented"
        ),
    )
    p_mine.add_argument(
        "--no-variant-report",
        action="store_true",
        help=(
            "Suppress the report-only backup/variant directory advisory in the "
            "mine header. The advisory never excludes anything (#36)"
        ),
    )
    p_mine.add_argument(
        "--redetect-origin",
        action="store_true",
        help=(
            "Re-run corpus_origin detection on this directory and overwrite "
            "<palace>/.mempalace/origin.json. Useful when the corpus has grown "
            "since `mempalace init` and the stored origin may be stale. "
            "Heuristic-only (no LLM call) — re-run `mempalace init --llm` for "
            "Tier 2 refinement."
        ),
    )
    p_mine.add_argument(
        "--dry-run", action="store_true", help="Show what would be filed without filing"
    )
    p_mine.add_argument(
        "--extract",
        choices=["exchange", "general"],
        default="exchange",
        help="Extraction strategy for convos mode: 'exchange' (default) or 'general' (5 memory types)",
    )

    # sweep
    p_sweep = sub.add_parser(
        "sweep",
        help="Tandem miner: catch anything the primary miner missed "
        "(message-level, timestamp-coordinated, idempotent)",
    )
    p_sweep.add_argument(
        "target",
        help="A .jsonl transcript file, or a directory to scan recursively",
    )
    p_sweep.add_argument(
        "--allow-zero-output",
        action="store_true",
        help="Allow a reviewed source version to remove every managed sweeper row",
    )

    # search
    p_search = sub.add_parser("search", help="Find anything, exact words")
    p_search.add_argument("query", help="What to search for")
    p_search.add_argument("--wing", default=None, help="Limit to one project")
    p_search.add_argument("--room", default=None, help="Limit to one room")
    p_search.add_argument("--results", type=int, default=5, help="Number of results")

    # compress
    p_compress = sub.add_parser(
        "compress", help="Compress drawers using AAAK Dialect (~30x reduction)"
    )
    p_compress.add_argument("--wing", default=None, help="Wing to compress (default: all wings)")
    p_compress.add_argument(
        "--dry-run", action="store_true", help="Preview compression without storing"
    )
    p_compress.add_argument(
        "--config", default=None, help="Entity config JSON (e.g. entities.json)"
    )

    # wake-up
    p_wakeup = sub.add_parser("wake-up", help="Show L0 + L1 wake-up context (~600-900 tokens)")
    p_wakeup.add_argument("--wing", default=None, help="Wake-up for a specific project/wing")

    # split
    p_split = sub.add_parser(
        "split",
        help="Split concatenated transcript mega-files into per-session files (run before mine)",
    )
    p_split.add_argument("dir", help="Directory containing transcript files")
    p_split.add_argument(
        "--output-dir",
        default=None,
        help="Write split files here (default: same directory as source files)",
    )
    p_split.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be split without writing files",
    )
    p_split.add_argument(
        "--min-sessions",
        type=int,
        default=2,
        help="Only split files containing at least N sessions (default: 2)",
    )

    # hook
    p_hook = sub.add_parser(
        "hook",
        help="Run hook logic (reads JSON from stdin, outputs JSON to stdout)",
    )
    hook_sub = p_hook.add_subparsers(dest="hook_action")
    p_hook_run = hook_sub.add_parser("run", help="Execute a hook")
    p_hook_run.add_argument(
        "--hook",
        required=True,
        choices=["session-start", "stop", "precompact"],
        help="Hook name to run",
    )
    p_hook_run.add_argument(
        "--harness",
        required=True,
        choices=["claude-code", "codex"],
        help="Harness type (determines stdin JSON format)",
    )

    # instructions
    p_instructions = sub.add_parser(
        "instructions",
        help="Output skill instructions to stdout",
    )
    instructions_sub = p_instructions.add_subparsers(dest="instructions_name")
    for instr_name in ["init", "search", "mine", "help", "status"]:
        instructions_sub.add_parser(instr_name, help=f"Output {instr_name} instructions")

    # repair
    p_repair = sub.add_parser(
        "repair",
        help=("Rebuild palace vector index, replay from SQLite, or un-poison max_seq_id rows"),
    )
    p_repair.add_argument(
        "--yes", action="store_true", help="Skip confirmation for destructive changes"
    )
    p_repair.add_argument(
        "--confirm-truncation-ok",
        action="store_true",
        help=(
            "Override the #1208 safety guard. Required when chromadb's collection-layer "
            "extraction returns exactly 10,000 drawers and the SQLite ground-truth check "
            "either matches or can't be read. Use only after independently confirming "
            "the palace really contains that count."
        ),
    )
    p_repair.add_argument(
        "--mode",
        choices=["legacy", "sqlite-replay", "max-seq-id"],
        default="legacy",
        help=(
            "legacy: full-palace rebuild (default). "
            "sqlite-replay: rebuild drawers from chroma.sqlite3 metadata when HNSW is diverged. "
            "max-seq-id: un-poison max_seq_id rows corrupted by the legacy 0.6.x shim."
        ),
    )
    p_repair.add_argument(
        "--segment",
        default=None,
        help="Segment UUID filter for --mode max-seq-id (repairs only that segment).",
    )
    p_repair.add_argument(
        "--from-sidecar",
        default=None,
        help=(
            "Path to a pre-corruption chroma.sqlite3 sidecar (for --mode max-seq-id); "
            "clean values are copied from its max_seq_id table verbatim."
        ),
    )
    p_repair.add_argument(
        "--backup",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Back up SQLite before mutation (default: on)",
    )
    p_repair.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Replay batch size for --mode sqlite-replay (default: 1000)",
    )
    p_repair.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help=(
            "Abort --mode sqlite-replay before mutation when planned replay rows exceed this bound"
        ),
    )
    p_repair.add_argument(
        "--max-batches",
        type=int,
        default=None,
        help=(
            "Abort --mode sqlite-replay before mutation when planned replay batches exceed this bound"
        ),
    )
    p_repair.add_argument(
        "--artifact-dir",
        default=None,
        help=(
            "Directory for sqlite-replay result.json and events.jsonl "
            "(default: <palace>/.mempalace/repair-runs/<run>)"
        ),
    )
    p_repair.add_argument(
        "--confirm-large-reembed",
        action="store_true",
        help=(
            "Allow --mode sqlite-replay to re-embed more than 100,000 documents. "
            "This can run for hours and consume substantial CPU/GPU."
        ),
    )
    p_repair.add_argument(
        "--dry-run",
        action="store_true",
        help="Print plan and exit without mutation (--mode sqlite-replay or max-seq-id)",
    )
    p_repair.add_argument(
        "--json",
        action="store_true",
        help="Print sqlite-replay result JSON (human output is captured under stdout)",
    )

    # repair-status — read-only HNSW capacity health check (#1222)
    p_repair_status = sub.add_parser(
        "repair-status",
        help="Compare sqlite vs HNSW element counts (read-only; never opens a chromadb client)",
    )
    p_repair_status.add_argument(
        "--json",
        action="store_true",
        help="Emit a single machine-readable status JSON object to stdout instead of the human summary",
    )
    p_repair_status.add_argument(
        "--artifact-dir",
        default=None,
        help=(
            "Also write the same status JSON to a timestamped repair-status-<UTC>.json "
            "file in this directory (read-only probe; never creates a repair-run directory)"
        ),
    )

    # warm — pre-pay the first-open cost after bulk mutations (#19)
    p_warm = sub.add_parser(
        "warm",
        help=(
            "Pre-warm vector search (model + HNSW + pending post-mutation work). "
            "Run after dedup --apply or sqlite-replay so the next reader pays "
            "seconds, not minutes."
        ),
    )
    p_warm.add_argument(
        "--json",
        action="store_true",
        help="Emit a single machine-readable warm result JSON object to stdout",
    )

    # backup-snapshot — clean-client lease + staged consistent snapshot (#33)
    p_backup_snapshot = sub.add_parser(
        "backup-snapshot",
        help=(
            "Stage a consistent, content-identity-proven palace snapshot under an "
            "exclusive clean-client lease (SQLite online backup; never a live tar)"
        ),
    )
    p_backup_snapshot.add_argument(
        "--staging-dir",
        default=None,
        help="Empty directory outside the palace root that receives the staged restore set",
    )
    p_backup_snapshot.add_argument(
        "--verify-receipt",
        default=None,
        help="Verify an existing backup-snapshot-receipt.json instead of creating a snapshot",
    )
    p_backup_snapshot.add_argument(
        "--staged-root",
        default=None,
        help="Override the staged root recorded in the receipt during verification",
    )
    p_backup_snapshot.add_argument(
        "--skip-hash-verification",
        action="store_true",
        help="Structural verification only (schema, proof flags, inventory, sizes, digest)",
    )
    p_backup_snapshot.add_argument(
        "--no-maintenance-marker",
        action="store_true",
        help="Do not raise the shared MemSys maintenance pause marker for the lease window",
    )
    p_backup_snapshot.add_argument(
        "--progress",
        action="store_true",
        help="Print SQLite online-backup progress to stderr",
    )
    p_backup_snapshot.add_argument(
        "--json",
        action="store_true",
        help="Emit the machine-readable snapshot receipt or verification object to stdout",
    )

    # variants
    p_variants = sub.add_parser(
        "variants",
        help="Report backup/variant directory candidates (read-only, excludes nothing)",
    )
    p_variants.add_argument("dir", help="Directory to inspect")
    p_variants.add_argument(
        "--max-depth",
        type=int,
        default=None,
        help="Directory depth to inspect (default: 3, or `variants.max_depth` in mempalace.yaml)",
    )
    p_variants.add_argument(
        "--json",
        action="store_true",
        help="Emit the machine-readable candidate list to stdout",
    )

    # exclusions
    p_exclusions = sub.add_parser(
        "exclusions",
        help="Show the effective mine-time exclusion policy for a directory",
    )
    p_exclusions.add_argument("dir", nargs="?", default=".", help="Project directory (default: .)")
    p_exclusions.add_argument(
        "--json",
        action="store_true",
        help="Emit the machine-readable effective policy to stdout",
    )

    # mcp
    sub.add_parser(
        "mcp",
        help="Show MCP setup command for connecting MemPalace to your AI client",
    )

    # status
    # migrate
    p_migrate = sub.add_parser(
        "migrate",
        help="Migrate palace from a different ChromaDB version (fixes 3.0.0 → 3.1.0 upgrade)",
    )
    p_migrate.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be migrated without changing anything",
    )
    p_migrate.add_argument(
        "--yes", action="store_true", help="Skip confirmation for destructive changes"
    )

    sub.add_parser("status", help="Show what's been filed")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # Handle two-level subcommands
    if args.command == "hook":
        if not getattr(args, "hook_action", None):
            p_hook.print_help()
            return
        cmd_hook(args)
        return

    if args.command == "instructions":
        name = getattr(args, "instructions_name", None)
        if not name:
            p_instructions.print_help()
            return
        args.name = name
        cmd_instructions(args)
        return

    dispatch = {
        "init": cmd_init,
        "mine": cmd_mine,
        "split": cmd_split,
        "search": cmd_search,
        "sweep": cmd_sweep,
        "mcp": cmd_mcp,
        "compress": cmd_compress,
        "wake-up": cmd_wakeup,
        "repair": cmd_repair,
        "repair-status": cmd_repair_status,
        "backup-snapshot": cmd_backup_snapshot,
        "warm": cmd_warm,
        "migrate": cmd_migrate,
        "status": cmd_status,
        "variants": cmd_variants,
        "exclusions": cmd_exclusions,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
