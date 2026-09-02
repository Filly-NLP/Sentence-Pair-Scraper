# Sentence-Pair Scraper Handoff

Updated: 2026-09-02

## Current status

The repository has completed the offline safety and regression work for the
Stage 8A slice described by `adaptation.md`. The implementation remains an
offline verification effort; no live discovery rollout has been approved.

The worktree already contains intentional, uncommitted adaptation changes.
Preserve unrelated dirty-worktree changes. Do not commit, push, stash, reset,
clean, or revert them.

## Completed work

The following adaptation stages and supporting work are present in the
worktree:

- Discovery diagnostics and repeatable offline baseline reporting.
- RSS and sitemap auditing, including redirects, namespaces, gzip content,
  nested indexes, budgets, and reconciled telemetry.
- A bounded archive discovery path with fail-closed rollout guards.
- A bounded persistent article-to-article link frontier with URL provenance,
  depth and priority ordering, robots/scope checks, cancellation recovery, and
  disabled-by-default configuration.
- Stage 8A offline safety/regression verification across the combined
  discovery, archive, frontier, extraction, lifecycle, and deduplication
  contracts.

Stage 8A specifically added:

- Fixture-backed integration coverage under `tests/fixtures/integration/` and
  `tests/test_stage8_offline_integration.py`.
- An autouse socket tripwire in `tests/conftest.py` that blocks external TCP
  connections and outbound UDP traffic while allowing only the narrowly scoped
  in-process Windows socketpair fallback used by asyncio.
- Fail-closed configuration assertions in `tests/test_config_validation.py`.
- Documentation of the offline-only scope in `README.md`.

## Verification completed

- Full offline test suite: **131 passed**.
- Python compilation check: passed.
- `git diff --check`: passed. Git emitted only existing LF-to-CRLF working-copy
  warnings; no whitespace errors were reported.
- The active corpus database was not opened for mutation and remains unchanged.

Recorded active database fingerprint:

```text
data/corpus.db
size: 135168 bytes
mtime UTC: 2026-09-01T14:30:25.1989593Z
SHA-256: 44BB68353681184D0D0BE41E17B6BE6B8343CD3C2B8820DB44A159537664CF46
```

The WAL and SHM sidecars were also preserved during the work. Before any
future implementation, re-check their sizes, mtimes, and hashes as required
by `adaptation.md`.

## Important scope clarification

Stage 8A verifies bounded reruns and object reinitialization using isolated
offline fixtures. It is not the complete Stage 8 integration gate. Durable
file-backed close/reopen restart coverage and a fixture that proves distinct
cross-article sentence deduplication remain follow-up work.

No production network request, publisher crawl, migration, or live discovery
was performed.

## Deferred roadmap

The next roadmap stage is Stage 5: optional Common Crawl index seeding.
Common Crawl work has not been implemented in this handoff. The previously
started Stage 5 worker was interrupted and closed before implementation was
completed; do not assume that any Common Crawl module, configuration, CLI
surface, or tests exist.

Stage 5 must follow the constraints in `adaptation.md`:

- Keep Common Crawl globally and per source disabled by default.
- Require an explicit `CC-MAIN-YYYY-WW` collection when enabled.
- Query only Common Crawl index metadata; never fetch WARC/article bodies.
- Reuse `SourceConfig.classify_url()` and existing discovery diagnostics.
- Enforce request, page, response-byte, and candidate budgets.
- Cache only successful complete index pages using atomic writes.
- Keep publisher fetching in the existing robots-aware crawler.
- Add offline `httpx.MockTransport` tests without touching `data/corpus.db`.
- Do not add a dependency or schema migration unless inspection proves it is
  necessary.

Stages 6 and 7 remain deferred:

- Stage 6: optional Trafilatura extraction fallback, enabled only after
  measured primary-extractor failures and offline precision testing.
- Stage 7: optional WARC response preservation, enabled only after storage,
  redaction, atomic-write, and retention decisions are established.

Stage 9 is a separate operational phase requiring explicit approval. It covers
read-only audits and staged live rollout, beginning with one source and strict
budgets. Do not enable production discovery automatically.

## Recommended next steps

1. Re-read `AGENTS.md` and `adaptation.md`.
2. Capture the active database and sidecar fingerprints before edits.
3. Ask a planner for an implementation-ready Stage 5 plan.
4. Dispatch a worker only after the plan is available.
5. Review the worker diff and run focused Common Crawl tests followed by the
   full offline suite.
6. Reconfirm configuration is disabled by default and the active database
   fingerprint is unchanged.
