# Conversation chunk context and identity contract

Date: 2026-08-20
Owners: [mempalace#49](https://github.com/iMelki/mempalace/issues/49) and
[memsys#529](https://github.com/iMelki/memsys/issues/529)
Status: code and synthetic proof implemented; live corpus migration not authorized

## Plain-English outcome

Long ChatGPT answers no longer have to become anonymous 800-character fragments.
The miner can keep each conversation and answer separate, repeat a bounded labelled
copy of the parent question on answer continuations, preserve the assistant answer
exactly, and stamp privacy-safe conversation/message identities into managed receipts.

This change does **not** re-mine or rewrite the live palace. The existing corpus stays
unchanged until a separate supervised migration is approved under `memsys#529`.

## Why the old path was unsafe for retrieval

The prior path flattened every ChatGPT conversation in an export shard into one text
stream. Its exchange parser then:

- guessed whether text was a conversation by counting quoted lines;
- collapsed blank lines and indentation in assistant answers;
- emitted arbitrary character slices whose continuations omitted the question;
- generated drawer identity from a mutable global chunk index; and
- did not bind a conversation-chunk schema to the managed write receipt.

An isolated continuation such as “it must be invalidated first” is therefore hard to
retrieve or interpret correctly. A replay could also move the same semantic chunk to a
different drawer identity merely because another conversation was inserted earlier.

## Research and adopted design

The implementation adapts, but does not blindly cherry-pick, three upstream patterns:

- [upstream PR #2166](https://github.com/MemPalace/mempalace/pull/2166) proves that a
  ChatGPT account export is an array of conversation mapping trees and that each
  conversation must remain independently addressable. Its older first-child traversal
  is not adopted; this fork retains its current-node, parent-pointer, multimodal, voice,
  attachment, and thought-policy behavior.
- [upstream PR #1538](https://github.com/MemPalace/mempalace/pull/1538) supplies the
  bounded-emission principle for large paragraphs and single lines.
- [upstream PR #1137](https://github.com/MemPalace/mempalace/pull/1137) documents the
  retrieval damage caused by stripping indentation, dropping blank lines, and joining
  assistant lines with spaces.

The active default embedding model also matters. The
[all-MiniLM-L6-v2 model card](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2)
states that inputs longer than 256 word pieces are truncated. In this checkout,
ChromaDB 1.5.7 reports `max_tokens() == 256` and rejects an over-limit input before
embedding. The present `structure-aware-chars-v1` budget is therefore an honest bounded
compatibility mode, **not** a token-complete embedding guarantee.

## Contract

### Source representation

`normalize()` remains the compatible flattened-text API. Conversation mining first
tries the additive structured API, which returns:

- one `ConversationUnit` per provider conversation;
- ordered `ConversationTurn` records for selected user and assistant nodes;
- the nearest selected parent user for every assistant branch, including regenerated
  sibling answers; and
- original selected-turn order even when an intermediate user has no usable assistant
  record; and
- no raw provider identifiers.

Alternative branches remain disabled by default. When explicitly enabled, each
assistant sibling retains the same parent-question identity instead of consuming the
question only once.

### Privacy-safe identity

Provider IDs are domain-separated SHA-256 inputs. Message identity is scoped by its
conversation, so two exports that reuse node IDs cannot collide. When the provider
conversation ID is absent, the fallback scope combines the sanitized title with the
selected node, role, and text evidence and sets `identity_fallback=true`. A missing
provider message ID uses a namespaced mapping-node or content fallback and also sets the
same flag; the mapping node is not misreported as a provider message ID.

Title sanitization neutralizes every Unicode control, line-separator, and paragraph-
separator category (`Cc`, `Zl`, and `Zp`), not only ASCII newlines. A title containing
NEL, LS, or PS therefore cannot forge a user turn when a downstream reader uses
`splitlines()`.

Persisted metadata is a scalar allowlist. Raw conversation, root, and message IDs are
not documents, metadata, receipts, diagnostics, or test output.

### Chunk behavior

The chunker applies this order:

1. conversation;
2. user/assistant exchange;
3. complete code-fence, paragraph, newline, or word boundary when available; and
4. an exact character boundary when no suitable structure exists.

The first assistant part carries the full question when it fits. Each continuation
carries a bounded `[user-context]` envelope. Brackets in the derived context copy are
neutralized to full-width characters so source text cannot forge the envelope; the
original user turn remains verbatim in its own first/standalone chunk.

Concatenating the internal assistant payloads reproduces the source answer exactly.
Context envelopes are derived retrieval aids and are not included in that payload.

Legacy non-ChatGPT input now uses exact bounded source slices. The quote-count heuristic
is retained only to prefer exchange boundaries after three apparent user turns; one
ordinary Markdown blockquote cannot cause its preamble or trailing newline to disappear.

### Receipt and replay behavior

`CONVERSATION_CHUNK_SCHEMA_VERSION`, the maximum and minimum chunk sizes, the budget
method, and the resolved `all_branches` / `include_thoughts` booleans are part of the
managed run configuration. The two environment-backed switches are resolved once and
passed explicitly to every source in that run. The adapter version also carries the
schema. Any config mismatch creates a new managed receipt and supersedes the prior
receipt rather than being called unchanged.

The global `NORMALIZE_VERSION` is deliberately unchanged. Publishing code is not an
implicit authorization to rebuild every previously mined source.

## Safety boundary and later migration gate

No configured palace, Chroma collection, MemSys backend, provider, or external model was
opened or mutated for this implementation. Tests use synthetic exports and temporary
receipt stores only.

A future live re-mine must separately:

1. name and verify the active embedding function and a non-truncating tokenizer adapter;
2. freeze source identity, code revision, schema, source counts, and current drawer counts;
3. take and restore-prove a current backup;
4. rehearse the rewrite against a cloned palace;
5. compare source-to-drawer receipts, answer reconstruction, identities, and retrieval;
6. run the supervised acceptance pack; and
7. retain rollback evidence before any production replacement.

Code rollback is an ordinary commit revert. A future data rollback must use its own
verified snapshot and receipts; this code change creates no live data rollback burden.

## Gate proof and validation

The changed continuation-context gate was deliberately bypassed in
`tests/fixtures/conversation_chunk_context_bypass_fixture.py`. The fixture proved the
bypass applied, then failed with exit 1 for the named reason: a continuation was admitted
without inherited user context. Restoring the implementation made the exact caller test
pass. The machine-readable receipt is in `.gate-evidence.json`.

Focused coverage includes long answers/questions, regenerated answers, exact payload
reconstruction, code fences, CJK/emoji, huge lines, hostile context markers, fallback-ID
collisions, export-order stability, metadata allowlisting, legacy reconstruction, and
receipt schema mismatch.

Final local proof on the exact publication tree:

- Ruff format check and lint passed for all changed Python files;
- the focused conversation/normalizer/miner/receipt packet passed 205 tests in 34.61s;
- the full suite passed 2,071 tests, with 8 skipped and 106 intentionally deselected, in
  282.57s;
- Markdown links and `git diff --check` passed under independent review; and
- host memory stayed below 54.1% during the final run, far under the 83% start gate.

The logs are retained under `%LOCALAPPDATA%\MemPalace\test-runs`. Neither proof opened a
configured palace or called a provider.

## Maintainability decision

Before this change, `normalize.py` was 1,054 physical / 900 nonblank lines and
`convo_miner.py` was 1,031 / 935. They are now 1,251 / 1,078 and 1,075 / 982.
The cohesive pure chunking implementation lives in the new
`conversation_chunking.py` (535 physical / 471 nonblank lines), rather than putting that
logic into the already-large miner.

The structured adapter remains in `normalize.py` because it reuses the existing private
ChatGPT branch traversal, multimodal extraction, and sanitization rules. Extracting it
now would either duplicate those policies or expose a second traversal contract. The
largest new normalizer function is 67 lines with a branch-count proxy of 12 and maximum
nesting 4. The new chunking module's largest function is 73 lines with a branch-count
proxy of 8 and maximum nesting 3, below the policy's 80-line independent-review trigger.

`_file_chunks_locked` was already an oversized transaction boundary and grew from 185 to
197 lines to copy the scalar metadata allowlist into the same atomic collection batch.
Extracting that small loop would split the existing drawer-ID/document/metadata batch
construction without reducing the transaction's decisions, so the independently reviewed
cohesion exception is narrower than a refactor.

Two other existing orchestration functions remain above the normal function target.
`_process_conversation_file_locked` grew from 131 to 168 lines (branch-count proxy 21)
because the one source lock now encloses one byte snapshot, receipt reuse/supersession,
structured normalization, chunk preparation, and terminal receipt failure handling.
Provider traversal and chunk mechanics are already extracted into `normalize.py` and the
new pure module; splitting the remaining transaction would make the lock/receipt lifetime
implicit. `_mine_convos_impl` grew from 107 to 116 lines (branch-count proxy 11) so the two
environment-backed policy flags are resolved once beside managed-run creation, included in
the run config, and passed unchanged to every source. Extracting those few lines would hide
the receipt-identity boundary rather than create a cohesive reusable unit. Both growth
exceptions were independently reviewed; a later transaction-orchestrator refactor should
be separate from this behavior fix.

`tests/test_write_receipts.py` grew from 4,699 physical / 4,118 nonblank lines to
4,794 / 4,209. The receipt-config regression stays there because it reuses that module's
private temporary receipt store and in-memory collection harness; moving it would either
duplicate a sensitive write-receipt fixture or turn test-private helpers into production
API. Its 95-line growth and the existing test-module size are explicitly accepted for this
integration slice and independently reviewed. New pure chunk behavior remains isolated in
`tests/test_conversation_chunking.py` (274 physical / 231 nonblank lines).

The independently reviewed exceptions are recorded on `mempalace#49`; the longer-term
source-adapter split remains RFC 002 work.
