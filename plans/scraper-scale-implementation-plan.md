# Scraper Scale and Reliability Implementation Plan

Status: Draft awaiting explicit user approval  
Repository: `D:\files\Online Classes\College\4th Year\random\Sentence-Pair-Scraper`  
Execution workflow: prioritize repository-local `agy-loop.md`

## Objective

Increase corpus volume by fixing acquisition bottlenecks while preserving ethical crawling, existing corpus data, source boundaries, and restart safety. Success means the scraper can discover all in-scope URLs exposed through verified sitemaps and configured archives, process them with bounded asynchronous I/O, explain every loss, retry transient failures durably, and export more than 1,000 eligible sentences without truncation. This is a scale objective, not a guarantee of exponential growth.

## Shared preamble for every agy dispatch

- Work only in this repository and only on the current Part.
- Follow the approved plan exactly; do not re-plan or expand scope.
- Preserve unrelated working-tree changes. Never commit, push, stash, reset, clean, or revert.
- Keep robots.txt compliance, configured crawl delays, and a transparent user-agent. Never add CAPTCHA, Cloudflare, paywall, proxy, identity-rotation, or bot-evasion behavior.
- Use additive, idempotent migrations. Do not delete or rewrite existing articles or sentences.
- Keep production per-domain concurrency at `1` until Part 5 passes.
- `queue_batch_size: 100` is a database batch size, not a total cap. `yield_per(1000)` is streaming, not a 1,000-row export cap.
- End with the exact `## Summary of Changes` report required by `agy-loop.md`.

Dispatch and audit each Part separately through the local Low -> Medium -> High loop. Do not start a later Part until the current Part is Satisfied.

## Part 1 - Durable outcomes, telemetry, and configuration contracts

### Expected files

`src/storage/models.py`, `src/storage/migrations.py`, `src/crawler/config.py`, `src/sources/registry.py`, `src/crawler/pipeline.py`, `src/cli/main.py`, both YAML configs, migration/pipeline tests, new `tests/test_config_validation.py`, and `README.md`.

### Scope

1. Centralize URL states: `DISCOVERED`, `PROCESSING`, `RETRY_WAIT`, `DOWNLOADED`, `ACCEPTED`, `NO_SENTENCES`, `REJECTED`, `BLOCKED`, and `TERMINAL_FAILED`. Treat existing `FAILED` as legacy until explicitly migrated or requeued.
2. Add durable diagnostic/scheduling fields: last HTTP status, failure class, last attempt, processing start, sentence count, date-hint source/confidence, sitemap lastmod hint, and bounded extraction diagnostics. Reuse existing retry/date/error fields.
3. Add a due-work index covering state, next retry, source, and URL ID.
4. Record structured outcomes for discovery documents, candidates, HTTP, extraction, zero-sentence articles, and sentence rejection reasons. Remove silent exception/non-200 swallowing.
5. Validate regexes, host lists, delays, concurrency, budgets, archive ranges, and pagination limits at startup with source/field-specific errors.
6. Expand `stats` by source, discovery method, URL state, HTTP status, date source, extraction outcome, and sentence rejection reason.
7. Add an idempotent migration audit/dry-run. Backfill sentence counts; only reclassify old zero-sentence/failed rows through an explicit tested action.
8. Document the batch/streaming non-caps and reconcile documented defaults with active config.

### Acceptance

- Existing databases migrate repeatedly without data loss or changing eligible exports.
- Stats explain why sitemap candidates did not become sentences.
- Invalid source configuration fails precisely.
- Every discovery request has a visible outcome.

### Verify

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_storage_migrations.py tests/test_pipeline_resilience.py tests/test_config_validation.py -q
.\.venv\Scripts\python.exe -m src.cli.main discover --dry-run
.\.venv\Scripts\python.exe -m src.cli.main stats
```

## Part 2 - Durable retry, cooldown, requeue, and zero-sentence semantics

### Expected files

`src/crawler/backoff.py`, `fetcher.py`, `pipeline.py`, storage models, CLI, crawler config, pipeline tests, new `tests/test_retry_policy.py`, new `tests/test_zero_sentence_outcomes.py`, and README.

### Scope

1. Return a typed fetch result with status, headers, body, cache state, attempts, `Retry-After`, and stable error category. The pipeline owns persistent state.
2. Treat network failures and HTTP `202`, `408`, `425`, `429`, `500`, `502`, `503`, and `504` as transient by default, with bounded source overrides.
3. Handle `403` ethically: record it, honor `Retry-After`, apply domain cooldown, allow only a small delayed retry budget, then mark blocked/terminal. Add no evasion.
4. Persist `RETRY_WAIT`, retry count, and next retry. Claim due retry rows with discovered rows; never claim future retries.
5. Separate per-request attempts, per-URL lifecycle retries, and per-domain cooldown controls.
6. Recover stale processing/downloaded rows into retry state with telemetry.
7. Add dry-run-first requeue CLI filters for due time, source, and state. Require an explicit flag for blocked/terminal rows.
8. Mark stored articles with zero surviving sentences `NO_SENTENCES`, record zero count, and break down segmentation, quality, quote, language, and duplicate losses.

### Acceptance

- Observed Abante `202` results become bounded retries instead of permanent losses.
- `403` remains bounded and ethical.
- Restart does not strand work.
- Zero-sentence articles cannot be `ACCEPTED`.

### Verify

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_retry_policy.py tests/test_pipeline_resilience.py tests/test_zero_sentence_outcomes.py -q
.\.venv\Scripts\python.exe -m src.cli.main retry --due --dry-run
```

## Part 3 - Comprehensive and observable sitemap discovery

### Expected files

Discovery, sitemap parser, robots, URL normalizer, source registry, pipeline, both configs, current sitemap/scope tests, new robots/telemetry tests, publisher fixtures under `tests/fixtures/sitemaps/`, and README.

### Scope

1. Combine explicit sitemap roots with `Sitemap:` directives from robots.txt; normalize and deduplicate roots/children.
2. Preserve index, URL-set, namespace, news-date, and gzip support. Detect gzip from bytes and applicable URL/content headers.
3. Add explicit root, document, depth, entry, total-candidate, and response-byte budgets. Emit `discovery:truncated` with source, root, dimension, limit, and pending count.
4. Record malformed XML, content mismatch, non-200, redirect, decompression failure, depth rejection, disallowed host, and out-of-scope candidate outcomes.
5. Separate allowed sitemap/CDN/redirect hosts from strict article host/path scope.
6. Keep article patterns strict, but count and sample rejected path shapes so stale regexes are visible.
7. Verify live publisher robots/sitemap document types during implementation. Replace GMA's navigation-only root only with a verified article-bearing root; otherwise disable it with a visible reason. Do not guess URLs.
8. Keep ordinary `lastmod` as low-confidence metadata; Part 6 owns date precedence.
9. Add a non-mutating report of roots, documents, candidates, duplicates, scope rejection, and truncation.

### Acceptance

- Explicit and robots-declared roots are traversed and deduplicated.
- Fixtures cover nested indexes, gzip, namespaces, malformed XML, budgets, and host rules.
- Capped discovery reports truncation rather than claiming completeness.
- GMA is verified or explicitly disabled as non-article-bearing.

### Verify

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_sitemap_parser.py tests/test_discovery_budget.py tests/test_robots_sitemap_discovery.py tests/test_discovery_telemetry.py tests/test_source_scope.py -q
.\.venv\Scripts\python.exe -m src.cli.main discover --dry-run
```

## Part 4 - Configurable year/month/day archive traversal

### Expected files

New `src/crawler/archive_discovery.py`, discovery, robots, registry, pipeline, CLI, both configs, new archive tests/fixtures, source-scope tests, and README.

### Configuration contract

Per source, add an opt-in archive block with year/month/day templates, start/end dates, article selectors, allowed hosts, pagination mode (`next_link` or page template), next selector/template, and limits for periods, pages, total pages, bytes, and candidates.

### Scope

1. Generate periods lazily within configured bounds and the global cutoff; assume no universal WordPress pattern.
2. Check robots permission and effective delay before every archive-page request.
3. Extract configured article links and pass them through existing normalization, global deduplication, and source scope.
4. Support bounded next-link/page-template pagination with repeated-URL/body and loop detection.
5. Record periods planned/requested/skipped, pages fetched/blocked/failed/truncated, links seen/out-of-scope/queued, and loops.
6. Queue with `discovery_method="ARCHIVE"`; keep the archive period a low-confidence hint, not final publication time.
7. Add CLI source/date/granularity/page-limit/dry-run controls.
8. Keep every source disabled initially. Enable one only after fixtures and a seven-day live dry-run verify template, selectors, and robots policy.

### Acceptance

- Year, month, and day templates work independently.
- Pagination is bounded, deduplicated, and loop-safe.
- Robots-disallowed archive pages are not fetched.
- Archive links share RSS/sitemap normalization, deduplication, and scope.

### Verify

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_archive_discovery.py tests/test_source_scope.py tests/test_discovery_telemetry.py -q
.\.venv\Scripts\python.exe -m src.cli.main discover --source SOURCE_ID --archive --from-date 2022-01-01 --to-date 2022-01-07 --dry-run
```

## Part 5 - Genuine bounded asynchronous HTTP execution

### Expected files

Fetcher, rate limiter, robots, discovery, archive discovery, pipeline, database, registry, configs, new async/rate-limit tests, pipeline resilience tests, and README.

### Architecture

1. Reuse one `httpx.AsyncClient` with connection limits and explicit connect/read/write/pool timeouts.
2. Route RSS, robots, sitemap, archive, and article requests through asynchronous fetching.
3. Add a bounded global semaphore and stable per-domain state containing a semaphore, delay schedule, and optional minute/hour limits.
4. Replace `wait_if_needed()` with an async request-slot context manager held for the full request. Updating limits must not replace live semaphores.
5. Compute the conservative effective delay from global, source, and robots values using a monotonic clock.
6. Use a coordinator/worker design: coordinator claims/commits a bounded batch; workers do network and pure parsing without ORM; one writer persists immutable results. Never share a SQLAlchemy session or ORM object across tasks.
7. Bound pending tasks and isolate failures so one task cannot cancel or lose unrelated outcomes.
8. Keep per-domain concurrency one at rollout; first demonstrate safe cross-domain overlap.

### Acceptance

- Tests prove cross-domain overlap and per-domain bounds.
- Delay applies between request starts and permits cover requests.
- No shared session/ORM use occurs across tasks.
- Cancellation/restart recovers claimed work.
- No blocking `httpx.get` remains in discovery/robots.

### Verify

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_async_concurrency.py tests/test_rate_limiter.py tests/test_pipeline_resilience.py -q
.\.venv\Scripts\python.exe -m pytest -q
```

## Part 6 - Trust-ranked date hints and extraction diagnostics

### Expected files

RSS/sitemap parsers, discovery/archive discovery, pipeline, extractor, date filter, models, configs, date/extraction tests, new precedence/diagnostic tests.

### Scope

1. Use deterministic evidence order: structured/source-specific HTML publication metadata; news-sitemap date; trusted RSS date; date in article URL; archive-period hint; ordinary sitemap `lastmod`.
2. Store chosen value, source, and confidence; keep modification dates separate.
3. `lastmod` may prioritize fetching but cannot alone accept/reject against the cutoff unless source-specific configuration proves it publication-equivalent.
4. Validate impossible/future dates, timezones, malformed values, and material disagreement.
5. Allow a trustworthy RSS/news/URL fallback when HTML metadata is missing and record why.
6. Record selector match, body characters, paragraph count, segmented sentences, rejection categories, duplicates, and inserts using bounded diagnostics.

### Acceptance

- Date precedence is deterministic and tested.
- `lastmod` cannot masquerade as publication time.
- Trustworthy fallbacks prevent avoidable sitemap/RSS date rejection.
- Stats locate selector, segmentation, quality, quote, language, and duplicate losses.

### Verify

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_date_filter.py tests/test_date_hint_precedence.py tests/test_extraction_metadata.py tests/test_extraction_diagnostics.py -q
```

## Part 7 - Yield policy, dead-config cleanup, and export proof

### Expected files

Quality filter, language detector, pipeline, registry, exporter, CLI, configs, quality/language/export tests, new policy and over-1,000 export tests, and README.

### Scope

1. Add global defaults plus per-source overrides for quotes, headlines, token bounds, accepted languages, confidence, mixed language, quality, and boilerplate.
2. Add named `strict`, `balanced`, and `recall` profiles while preserving current behavior as migration default.
3. Record policy/version/config hash and reconcile accepted plus rejected totals.
4. Add a non-mutating policy evaluator over sampled stored article text.
5. Evaluate quote inclusion explicitly; do not loosen globally from count alone.
6. Preserve exact deduplication and export uniqueness.
7. Implement or remove/document dead request-rate, cooldown, headline, near-duplicate, and source-percentage settings. No option may falsely imply active behavior.
8. Prove JSONL and CSV export more than 1,000 eligible unique sentences; keep `yield_per(1000)` as streaming.
9. Reconcile README defaults with active config.

### Acceptance

- Policy changes are source-specific, measurable, and reproducible.
- Current behavior remains available.
- No advertised setting is silently unused.
- Database and exported eligible counts match above 1,000.

### Verify

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_policy_profiles.py tests/test_quality_filter.py tests/test_language.py tests/test_export_filters.py tests/test_export_over_1000.py -q
.\.venv\Scripts\python.exe -m src.cli.main evaluate-policy --profile balanced --source SOURCE_ID --limit 100
```

## Part 8 - End-to-end migration and staged rollout gate

### Expected files

CLI, migrations, new end-to-end/restart tests, README, and existing CI configuration only if needed.

### Required scenarios

1. Robots declares a nested gzip sitemap yielding new and duplicate URLs.
2. Archive overlaps RSS/sitemap without duplicate queue rows.
3. Transient `202` retries then succeeds without duplicate data.
4. Repeated `403` cools down and blocks without bypass.
5. Missing HTML date uses trusted fallback while `lastmod` cannot override it.
6. Zero surviving sentences becomes `NO_SENTENCES` with reconciled reasons.
7. More than 1,000 eligible sentences export completely.
8. Cancellation/restart recovers stale work.
9. Budget exhaustion emits truncation.
10. Request logs prove robots, delay, and concurrency compliance.

### Staged rollout

1. Back up `data/corpus.db` with a documented recoverable procedure.
2. Apply additive migrations and inspect dry-run/backfill counts.
3. Enable telemetry and retries with concurrency one.
4. Verify sitemap discovery for one source.
5. Enable one archive source for seven days, then expand incrementally.
6. Enable async cross-domain execution while per-domain concurrency remains one.
7. Raise a domain only if robots policy and measured errors support it.
8. Evaluate policy profiles before changing defaults.
9. Reconcile database, exported file, and CLI counts.

### Release gate

- Full suite passes; migrations are idempotent; existing corpus rows remain intact.
- URL and sentence outcomes reconcile.
- No robots-disallowed request or delay/concurrency breach occurs.
- Growth is reported by source and discovery method.
- Exported eligible count equals the database count.
- Git status/diff and executor summaries show no unrelated paths.

### Final verification

```powershell
git status --short
.\.venv\Scripts\python.exe -m pytest tests/test_end_to_end_corpus_growth.py tests/test_restart_recovery.py tests/test_export_over_1000.py -q
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m src.cli.main stats
.\.venv\Scripts\python.exe -m src.cli.main export --output data/exports/verification.jsonl --format jsonl
git diff --check
git diff --stat
```

## Approval boundary

This plan is not execution authorization. Before the first `agy` dispatch, show this exact path and SHA-256, restate Part 1 and the Low starting tier, and explain that `--dangerously-skip-permissions` grants unattended shell and file access. Request explicit approval, then follow local `agy-loop.md` one Part at a time.
