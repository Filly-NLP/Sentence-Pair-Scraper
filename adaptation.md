# Discovery and Corpus Coverage Adaptation Plan

Status: Draft awaiting explicit approval  
Repository: `D:\files\Online Classes\College\4th Year\random\Sentence-Pair-Scraper`

## Objective

Improve site mapping and URL discovery without replacing the existing crawler or weakening its corpus-quality controls.

The work is divided into two tracks:

- **URL discovery:** diagnostics, feed/sitemap auditing, archive traversal, link-frontier crawling, and optional Common Crawl seeding.
- **Extraction and reproducibility:** optional Trafilatura fallback and optional WARC preservation.

This plan does **not** promise exponential corpus growth. Each stage must demonstrate measurable gains in valid, unique, in-scope article URLs before expansion.

## Non-negotiable constraints

- Preserve robots.txt enforcement and crawl-delay handling.
- Keep per-domain concurrency at `1`.
- Preserve current publication-date precedence and the `2022-01-01` cutoff.
- Preserve language, sentence-quality, article, and sentence deduplication semantics.
- Keep SQLite as the authoritative persistent queue.
- Use additive, idempotent migrations only.
- Do not overwrite or reset the active corpus database.
- Preserve unrelated dirty-worktree changes.
- Make every new discovery mechanism disabled by default and independently reversible.
- Perform implementation and automated validation offline. No production discovery or crawl occurs until the final staged rollout.

## Baseline and success metrics

Record the current behavior before changing discovery logic.

Observed baseline from the recent empty-corpus discovery run:

| Metric | Count |
|---|---:|
| Feed candidates examined | 156 |
| Unique in-scope URLs queued | 58 |
| Out-of-scope candidates | 98 |
| Queue rate | 37.2% |
| Rejection rate | 62.8% |

Known source concentrations:

- Pang Masa: 8 of 10 candidates rejected.
- GMA Filipino: 90 of 105 candidates rejected.

Add a repeatable, non-mutating baseline command that reports per source and discovery method:

- roots attempted;
- HTTP status, redirect target, content type, and response size;
- entries parsed;
- normalized unique candidates;
- candidates queued;
- database duplicates;
- cross-method duplicates;
- rejection counts by reason;
- bounded rejection samples;
- truncation reason and budget utilization;
- elapsed time.

Primary measures:

```text
scope_acceptance_rate = in_scope_unique / normalized_unique
new_url_rate          = queued_new / normalized_unique
duplicate_rate        = duplicates / normalized_unique
article_success_rate  = accepted_or_no_sentences / fetched_article_urls
sentence_yield        = accepted_sentences / accepted_articles
```

A higher candidate count alone is not success. A stage advances only when it increases valid unique article coverage without unacceptable fetch failures, scope contamination, robots violations, or duplicate growth.

---

## Stage 0 — Freeze contracts and capture a repeatable baseline

### Scope

Establish discovery contracts and offline fixtures before changing behavior.

### Expected files and symbols

- `src/cli/main.py`
  - Add a non-mutating `audit-discovery` or equivalent report command.
- `src/crawler/discovery.py`
  - Introduce a structured discovery-report contract shared by RSS and sitemap discovery.
- `src/crawler/archive_discovery.py`
  - Return the same core counters and rejection classifications.
- `tests/fixtures/discovery/`
  - Add representative RSS, sitemap, redirect, malformed, and rejected-URL fixtures.
- `tests/test_discovery_baseline.py`
- `README.md`

### Contracts

A discovery report must distinguish:

- candidates seen;
- candidates normalized;
- unique within run;
- already stored;
- accepted by scope;
- rejected by scope;
- queued;
- truncated;
- failed to fetch or parse.

Dry-run/audit mode must never insert URLs, update URL states, or change source cooldown state.

### Validation

- Snapshot current URL normalization and `SourceConfig.accepts_url()` behavior.
- Prove report totals reconcile:

```text
normalized_unique =
    accepted_unique
  + rejected_unique

accepted_unique =
    queued_new
  + already_stored
```

- Prove auditing leaves all database row counts and URL states unchanged.

### Acceptance gate

Proceed only when the observed 156/58/98 run can be represented without ambiguous “total URLs” wording and every count reconciles.

### Rollback

CLI/reporting is additive. Remove or disable the command without affecting stored corpus data.

---

## Stage 1 — Persistent discovery diagnostics and scope rejection reasons

### Scope

Replace transient, URL-only rejection samples with bounded, queryable evidence.

### Schema changes

Prefer a dedicated additive table rather than overloading article queue rows:

```text
discovery_observations
- observation_id
- crawl_id
- source_id
- discovery_method
- root_url
- candidate_url
- normalized_url
- outcome
- reason
- http_status
- detail_json
- observed_at
```

Suggested outcomes:

- `QUEUED`
- `ALREADY_STORED`
- `DUPLICATE_IN_RUN`
- `REJECTED_SCOPE`
- `REJECTED_INVALID`
- `REJECTED_DATE_HINT`
- `FETCH_ERROR`
- `PARSE_ERROR`
- `TRUNCATED`

Suggested scope reasons:

- `invalid_url`
- `unsupported_scheme`
- `host_mismatch`
- `path_pattern_mismatch`
- `section_landing_page`
- `cross_brand`
- `non_article_asset`
- `date_before_cutoff`
- `unknown`

Add indexes on `(crawl_id, source_id, outcome)` and `(source_id, reason, observed_at)`. Store at most a configurable number of full sample URLs per reason/run; persist aggregate counters for all observations to prevent unbounded diagnostic growth.

### Expected files and symbols

- `src/storage/models.py`
- `src/storage/migrations.py`
- `src/sources/registry.py`
  - Add a classifier returning a structured decision.
  - Keep `accepts_url()` as a compatibility boolean wrapper.
- `src/crawler/discovery.py`
- `src/crawler/archive_discovery.py`
- `src/crawler/pipeline.py`
- `src/cli/main.py`
- `config/crawler.yaml`
- `tests/test_discovery_diagnostics.py`
- `tests/test_source_scope.py`
- `tests/test_storage_migrations.py`

### Configuration

```yaml
discovery:
  diagnostics:
    enabled: true
    persist_aggregates: true
    sample_urls_per_reason: 10
    retention_runs_per_source: 20
```

Retention must be explicit and must not silently delete corpus URLs, articles, or sentences.

### Algorithm

Normalize once, then classify in this order:

1. URL syntax and scheme.
2. Approved host.
3. Obvious non-HTML asset or utility path.
4. Known landing/navigation shape.
5. Source article-path pattern.
6. Date hint, when trustworthy.
7. Duplicate status.

Do not broaden scope patterns in this stage.

### Telemetry

The CLI must show counts and bounded samples grouped by source, discovery method, and reason. It must not print only a generic “out of scope” total.

### Focused tests

- Host mismatch and path mismatch are distinguishable.
- Philstar cross-brand URLs are attributed correctly.
- GMA navigation and non-Balitambayan URLs are rejected with stable reasons.
- Samples are capped while aggregate totals remain exact.
- Diagnostic persistence survives restart.
- Migration is idempotent.
- Dry-run remains non-mutating.

### Acceptance gate

For the baseline fixture, all 98 rejected candidates must have a reason, and aggregate totals must reconcile exactly. No source pattern is changed until its rejected samples have been reviewed.

### Rollback

`discovery.diagnostics.enabled: false` stops new diagnostic writes. The additive table may remain unused.

---

## Stage 2 — Verified feed and sitemap auditing

### Scope

Determine whether poor discovery comes from invalid roots, redirects, malformed documents, incomplete sitemap traversal, or stale scope patterns.

### Expected files and symbols

- `src/crawler/discovery.py`
  - Split root fetching, document parsing, candidate classification, and persistence into testable units.
- `src/crawler/sitemap_parser.py`
- `src/crawler/rss_parser.py`
- `src/crawler/robots.py`
- `src/cli/main.py`
- `src/sources/registry.py`
- `config/sources.yaml`
- `tests/test_discovery_source_audit.py`
- Existing sitemap, robots, telemetry, and budget tests.
- `README.md`

### Audit contract

For each RSS/sitemap root, report:

- configured versus robots-declared origin;
- final redirect URL;
- status and content type;
- compressed and decoded byte size;
- document type: RSS, Atom, sitemap index, URL set, HTML, or malformed;
- nested sitemap depth;
- entries parsed and rejected;
- lastmod/news-publication-date availability;
- budget truncation.

Continue to support gzip, namespaces, nested indexes, and robots-declared roots.

### Configuration decisions

Only change `config/sources.yaml` when samples prove that a URL shape is a valid article for that exact source. Never weaken a pattern to a generic same-domain match.

For shared Philstar domains, source classification must remain section/brand aware.

### Focused tests

- Redirected sitemap root.
- Sitemap returned with an unexpected but parseable content type.
- HTML error page returned with status 200.
- Nested gzip sitemap index.
- Duplicate roots from config and robots.
- Malformed child sitemap does not discard valid siblings.
- GMA navigation-only sitemap produces zero article candidates with an explicit reason.
- Corrected verified path patterns accept only intended Filipino article shapes.

### Stop/go gate

Proceed when every enabled source has an audit result and either:

- at least one verified productive feed/sitemap root; or
- an explicit documented reason that it requires archive/link/Common Crawl discovery.

Do not proceed merely because a broad regex increases accepted URLs.

### Rollback

Each root and any corrected pattern is a separate configuration change. Revert the affected root/pattern without changing queue state.

---

## Stage 3 — Activate a single bounded archive source

### Dependency

Stages 1 and 2 must be complete so archive candidates are observable and classifiable.

### Scope

Validate archive traversal on exactly one source. Choose the source with:

- a verified date-based archive format;
- clear article links;
- the best audit confidence;
- robots permission;
- low cross-brand risk.

Do not enable all sources together.

### Expected files and symbols

- `config/sources.yaml`
- `src/crawler/archive_discovery.py`
- `src/sources/registry.py`
- `src/cli/main.py`
- `tests/fixtures/archives/`
- `tests/test_archive_discovery.py`
- `tests/test_archive_rollout_contract.py`
- `README.md`

### Initial configuration

```yaml
archive:
  enabled: false
  granularity: day
  start_date: null
  end_date: null
  article_link_selector: "<verified selector>"
  pagination_mode: "<verified mode>"
  max_periods: 7
  max_pages_per_period: 2
  max_total_pages: 14
  max_candidates: 500
  max_response_bytes: 10485760
```

Keep the checked-in default disabled during implementation. The live rollout supplies explicit dates and opt-in execution.

### Safety boundaries

- Robots check every archive and pagination request.
- Per-domain concurrency remains one.
- Stop on repeated normalized pagination URL or repeated body hash.
- Do not follow arbitrary links from archive pages.
- Apply approved host and article-path classification before queue insertion.
- Enforce period, page, byte, and candidate budgets independently.
- Archive period is a low-confidence date hint, not authoritative publication metadata.

### Focused tests

- Day/month/year template generation.
- Pagination loop and repeated-body termination.
- Candidate and page truncation.
- Restart after interruption does not duplicate URLs.
- Out-of-scope archive links retain reasons.
- Archive date hint does not override HTML or news-sitemap publication date.
- Robots denial produces no queue insertion.

### Live rollout gate

In the final rollout, run:

1. One source.
2. Seven days.
3. Dry-run/audit first.
4. Live discovery only after reviewing the audit.
5. Crawl a bounded batch.

Advance to one month only if:

- zero robots violations;
- no pagination loops escaping their budget;
- at least 70% of fetched queued URLs are real articles;
- scope contamination is below 5%;
- terminal fetch failures remain below 10%;
- sentence yield is non-zero.

### Rollback

Set `archive.enabled: false`. Previously queued URLs remain auditable and must not be deleted automatically.

---

## Stage 4 — Bounded persistent BFS/priority link frontier

### Dependency

Stages 1–3 establish scope classification, telemetry, and safe discovery budgets.

### Scope

Extract links from fetched HTML and use a persistent bounded frontier to find articles missing from feeds, sitemaps, and archives.

Use breadth-first or priority traversal, not unrestricted DFS.

### Schema changes

Extend `urls` additively:

```text
url_role              ARTICLE | DISCOVERY_PAGE
parent_url_id         nullable FK to urls.url_id
discovery_depth       integer, default 0
frontier_priority     integer
link_context          nullable
```

Consider a separate provenance table so one normalized URL can retain multiple parents and methods:

```text
url_discovery_edges
- edge_id
- from_url_id
- to_url_id
- crawl_id
- discovery_method
- depth
- link_text
- rel
- discovered_at
- unique(from_url_id, to_url_id, discovery_method)
```

Do not remove the unique constraint on normalized queue URLs. Multiple discovery paths create edges, not duplicate URL records.

### Expected files and symbols

- New `src/crawler/link_discovery.py`
- `src/crawler/pipeline.py`
- `src/crawler/url_normalizer.py`
- `src/crawler/robots.py`
- `src/crawler/rate_limiter.py`
- `src/storage/models.py`
- `src/storage/migrations.py`
- `src/sources/registry.py`
- `src/cli/main.py`
- `config/crawler.yaml`
- `config/sources.yaml`
- New `tests/test_link_frontier.py`
- New `tests/test_frontier_restart.py`
- Existing concurrency, restart, scope, and end-to-end tests.
- `README.md`

### Configuration

Global defaults:

```yaml
link_discovery:
  enabled: false
  max_depth: 2
  max_discovery_pages_per_source_run: 100
  max_candidates_per_source_run: 5000
  max_links_per_page: 250
  allow_query_parameters: false
  repeated_body_stop: true
```

Per-source overrides must define permitted discovery-page patterns separately from article patterns:

```yaml
link_discovery:
  enabled: false
  discovery_page_patterns: []
  article_priority: 100
  archive_priority: 50
  category_priority: 25
```

### Algorithm and contracts

For each successfully fetched HTML page:

1. Extract `<a href>` links.
2. Resolve relative URLs against the final response URL.
3. Normalize and remove fragments/tracking parameters.
4. Reject unsupported schemes, assets, search/login/share URLs, and unapproved hosts.
5. Classify as:
   - verified article;
   - approved discovery page;
   - rejected.
6. Insert unseen article URLs as `DISCOVERED`.
7. Insert or schedule approved discovery pages in the frontier.
8. Persist provenance edges for new and existing URLs.
9. Process lower depth first, then higher source/method priority.
10. Stop at every configured depth/page/link/candidate/byte budget.

Article pages enter the existing extraction pipeline unchanged. Discovery pages must never be treated as articles or produce sentences.

### Priority guidance

Suggested order:

1. RSS/news-sitemap article URLs.
2. Other verified sitemap article URLs.
3. Archive article URLs.
4. Article links found on article pages.
5. Approved category/archive discovery pages.
6. Optional external seeds.

Maintain cross-domain overlap while retaining concurrency one per domain.

### Safety boundaries

- Feature disabled by default.
- Same approved publisher hosts only.
- No arbitrary query crawling.
- No search, tag-cloud, calendar, login, share, AMP duplicate, print, or media traps.
- Robots and rate limiter apply to discovery pages exactly as to article pages.
- Repeated-body and normalized-URL loop detection.
- Cancellation and restart leave durable frontier state.
- No recursive in-memory queue is authoritative.

### Telemetry

Per source and depth:

- links extracted;
- links normalized;
- articles classified;
- discovery pages classified;
- new URLs;
- already-known URLs;
- rejection reasons;
- queue depth;
- page and candidate budget remaining;
- truncation reason;
- parent-to-child branching factor.

### Focused tests

- BFS processes depth 1 before depth 2.
- Priority ordering is stable.
- Same link from many parents creates one URL and multiple provenance edges.
- Relative and canonical links normalize correctly.
- Query/fragment traps are rejected.
- Discovery pages never enter article extraction.
- Shared-domain cross-brand links are rejected or assigned only by verified classification.
- Robots denial, cancellation, restart, and repeated-body termination.
- Per-domain concurrency remains one.
- Date/language/quality/dedup behavior is unchanged.
- End-to-end fixture discovers more articles without duplicate sentences.

### Pilot acceptance gate

Use one source, depth 2, at most 100 discovery pages and 5,000 candidates.

Continue only if:

- at least 100 new verified article URLs are found, or link discovery improves unique coverage by at least 25% over that source’s feed/sitemap baseline;
- at least 70% of fetched article-classified URLs extract as articles;
- scope contamination is below 5%;
- duplicates do not dominate more than 80% after the initial warm-up;
- all robots, delay, and concurrency tests pass.

If the gate fails, tune source classification or stop. Do not increase depth to compensate for poor precision.

### Rollback

Set global or per-source `link_discovery.enabled: false`. Existing queue rows and provenance remain valid audit history.

---

## Stage 5 — Optional Common Crawl Index seeding

### Dependency

Requires the structured classifier and persistent diagnostics from Stages 1–2. It does not require the link frontier, but should be evaluated after local publisher discovery is understood.

### Scope

Query the Common Crawl Index for candidate publisher URLs and feed verified matches into the existing queue. Common Crawl is a seed provider, not the article-text authority.

### Expected files and symbols

- New `src/crawler/common_crawl_discovery.py`
- `src/crawler/pipeline.py`
- `src/sources/registry.py`
- `src/cli/main.py`
- `config/crawler.yaml`
- `config/sources.yaml`
- `requirements.txt` only if an additional dependency is truly necessary.
- New `tests/test_common_crawl_discovery.py`
- `README.md`

### Configuration

```yaml
common_crawl:
  enabled: false
  index_collection: "<explicit collection>"
  max_index_pages: 5
  max_candidates_per_source_run: 5000
  timeout_seconds: 30
  cache_dir: "data/cache/common-crawl"
```

Per source, use specific URL prefixes or patterns. Never query an entire shared publisher domain without section constraints.

### Algorithm

- Query index metadata only.
- Cache responses for reproducibility and service courtesy.
- Normalize and classify every result using the same source classifier.
- Deduplicate against the existing URL table.
- Record `discovery_method = COMMON_CRAWL`.
- Treat index timestamps as retrieval hints, not publication dates.
- Fetch actual articles only through the existing robots-aware crawler.

### Focused tests

- Paginated index response.
- Malformed record isolation.
- Cache reuse.
- Shared-domain scope enforcement.
- Candidate and request budgets.
- External timestamp cannot override publication date.
- Network failure leaves no corrupt queue state.

### Acceptance gate

Keep the provider only if a bounded source query adds meaningful unique, in-scope URLs not already found by sitemaps/archives/link traversal. Disable it if results are mostly duplicates, stale pages, or cross-brand URLs.

### Rollback

`common_crawl.enabled: false`. No existing corpus record depends on continuing Common Crawl access.

---

## Stage 6 — Optional Trafilatura extraction fallback

### Track classification

This stage improves **extraction yield**, not URL discovery or site mapping.

### Dependency

Discovery stages can proceed without it. Add only after diagnostics show a meaningful number of valid fetched articles failing the existing extractor.

### Scope

Keep publisher-specific selectors and structured metadata as primary. Invoke Trafilatura only when the primary body is missing, below the configured minimum, or fails a clearly defined structural check.

### Expected files and symbols

- `src/extraction/base.py`
- Potential new `src/extraction/trafilatura_fallback.py`
- `src/crawler/pipeline.py`
- `src/crawler/config.py`
- `config/crawler.yaml`
- `requirements.txt`
- New `tests/test_trafilatura_fallback.py`
- Existing extraction/date/language/quality tests.
- `README.md`

### Configuration

```yaml
extraction:
  fallback:
    trafilatura:
      enabled: false
      trigger: "empty_or_short_primary"
      min_body_chars: 200
      favor_precision: true
```

Allow per-source enablement.

### Contracts

- Primary extractor remains authoritative when successful.
- Existing metadata/date precedence remains unchanged.
- Trafilatura text still passes the same date, language, sentence-quality, and dedup pipeline.
- Store `extractor_used`, primary failure reason, fallback outcome, and body length in extraction diagnostics.
- Never merge primary and fallback bodies automatically; select one deterministically to avoid duplicate text.

### Focused tests

- Fallback is not called after successful primary extraction.
- Empty/short primary triggers fallback when enabled.
- Disabled fallback preserves current behavior.
- Navigation-heavy fallback output is rejected by existing quality checks.
- Publication date precedence is unchanged.
- Exact article/sentence dedup remains unchanged.

### Acceptance gate

Enable per source only if an offline labeled fixture set shows a meaningful recovery of genuine article bodies without unacceptable navigation/noise contamination. Target at least a 20% recovery of current extraction failures and at least 95% precision on the reviewed fallback sample.

### Rollback

Disable globally or per source. No schema rollback is required beyond optional diagnostic fields.

---

## Stage 7 — Optional WARC response preservation

### Track classification

This stage improves **reproducibility and debugging**, not URL discovery or sentence yield.

### Scope

Optionally preserve selected HTTP request/response records for failed or sampled successful fetches.

### Expected files and symbols

- New `src/storage/warc_store.py`
- `src/crawler/fetcher.py`
- `src/crawler/pipeline.py`
- `src/storage/models.py` and `src/storage/migrations.py` if WARC references are stored.
- `config/crawler.yaml`
- `requirements.txt` for `warcio`, if selected.
- New `tests/test_warc_store.py`
- `.gitignore`
- `README.md`

### Configuration

```yaml
warc:
  enabled: false
  directory: "data/warc"
  preserve_failures: true
  success_sample_rate: 0.0
  max_response_bytes: 10485760
  rotate_bytes: 1073741824
```

### Contracts and safety

- Write atomically to a temporary file before finalizing a segment.
- Do not store credentials, cookies, authorization headers, or unrelated private data.
- Respect response-size and disk-budget limits.
- WARC failure must not fail or retry an otherwise successful crawl.
- Store only a reference, digest, record ID, and capture timestamp in SQLite.
- Define retention explicitly; never silently delete corpus data.

### Focused tests

- Request/response round-trip.
- Header redaction.
- Size truncation.
- Atomic recovery after interrupted write.
- Rotation.
- Disabled mode performs no writes.
- WARC write failure does not alter crawl outcome.

### Acceptance gate

Adopt only if storage estimates and research reproducibility needs justify it.

### Rollback

Set `warc.enabled: false`. WARC files are independent artifacts and may be archived or removed later under a separately approved retention operation.

---

## Stage 8 — Integrated offline verification

### Scope

Validate all enabled code paths without live publisher requests or mutation of the production database.

### Required scenarios

- RSS plus nested gzip sitemap discovery with reconciled telemetry.
- Overlapping RSS, sitemap, archive, link, and Common Crawl candidates produce one URL row.
- Scope rejection reasons remain stable.
- Archive and frontier cancellation/restart.
- Robots denial and crawl-delay enforcement.
- Per-domain concurrency one with cross-domain overlap.
- Pagination, repeated-body, query, and calendar traps.
- Date-hint precedence and sitemap-lastmod isolation.
- Primary extraction and Trafilatura fallback selection.
- `NO_SENTENCES` lifecycle.
- Exact article and sentence deduplication.
- Export above 1,000 sentences.
- Diagnostic and WARC failures do not corrupt crawl state.
- Additive migrations are idempotent on a copied pre-adaptation database.

### Final implementation gate

- Full offline test suite passes.
- `git diff --check` passes.
- Active `data/corpus.db` row counts and checksum/mtime are unchanged during implementation.
- All new mechanisms are disabled by default.
- Documentation identifies which stages affect discovery versus extraction/reproducibility.
- No production source is silently enabled for archive, link, Common Crawl, Trafilatura, or WARC behavior.

---

## Stage 9 — Staged live rollout

Live rollout is a separate, explicitly approved operational phase.

### Phase A — Read-only audits

1. Back up the active database.
2. Run migration dry-run.
3. Run feed/sitemap audit for one source.
4. Review roots, redirects, parsing, rejection reasons, and samples.
5. Correct only verified configuration.

### Phase B — One-source archive trial

1. Seven-day range.
2. Strict page/candidate budgets.
3. Audit/dry-run first.
4. Execute discovery.
5. Crawl a bounded batch.
6. Compare against the stage gates.

### Phase C — One-source link-frontier trial

1. Depth 1, then depth 2 only after review.
2. Maximum 100 discovery pages and 5,000 candidates.
3. Inspect per-depth branching, duplicates, and scope precision.
4. Disable immediately on crawler traps, robots issues, or contamination.

### Phase D — Expand time and source coverage

Expand in this order only after each prior gate passes:

1. Seven days.
2. One month.
3. One year.
4. Additional verified sources.

Never increase date range, depth, page budget, and source count simultaneously.

### Phase E — Optional providers and extraction

- Trial Common Crawl on one tightly scoped source.
- Enable Trafilatura only for a source with measured extraction failures.
- Enable WARC only after confirming disk and retention policy.

### Rollout dashboard

For every trial record:

- new unique article URLs by method;
- overlap between methods;
- scope acceptance and contamination;
- fetch and extraction success;
- terminal failures and HTTP status distribution;
- accepted/no-sentence/rejected article outcomes;
- sentence yield;
- robots denials and applied delays;
- truncation and budget usage;
- database and WARC storage growth.

## Final decision rule

Keep an adaptation only when it produces measurable, valid corpus coverage at acceptable operational cost. If a stage primarily adds duplicates, invalid pages, failures, or noise, disable it and improve source configuration before increasing crawl breadth.
