# Filipino News Corpus Scraper

A resumable, research-grade web scraper for collecting high-quality Filipino-language sentences from Philippine news articles published from **January 1, 2022 onward**. Built for Filipino Grammar Error Correction (GEC) research.

> **Scope:** This program *only* collects and stores Filipino sentences. It does **not** perform grammar-error generation, sentence corruption, synthetic data generation, or any annotation.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Supported News Sources](#supported-news-sources)
- [Requirements](#requirements)
- [Installation & Setup](#installation--setup)
- [First-Time Database Initialization](#first-time-database-initialization)
- [Running the Scraper](#running-the-scraper)
  - [Step 1: Discover URLs](#step-1-discover-urls)
  - [Step 2: Crawl & Extract](#step-2-crawl--extract)
  - [Step 3: Monitor Progress](#step-3-monitor-progress)
  - [Step 4: Export Corpus](#step-4-export-corpus)
- [Resuming After Shutdown](#resuming-after-shutdown)
- [Configuration Reference](#configuration-reference)
  - [sources.yaml](#sourcesyaml)
  - [crawler.yaml](#crawleryaml)
- [Output Format](#output-format)
  - [CSV Schema](#csv-schema)
  - [JSONL Schema](#jsonl-schema)
- [Running Tests](#running-tests)
- [Project Structure](#project-structure)
- [Ethical Crawling Policy](#ethical-crawling-policy)

---

## Overview

The scraper implements a multi-stage pipeline:

```
RSS / Sitemap Discovery
    → robots.txt check
    → Per-domain rate limiting
    → HTTP Fetch (conditional requests / cache)
    → Article extraction (JSON-LD → OpenGraph → CSS selectors)
    → Date filter (≥ 2022-01-01)
    → Sentence segmentation (Filipino-aware)
    → Language detection (lingua, sentence-level)
    → Quality filtering (token bounds, noise rejection)
    → Exact deduplication (SHA-256)
    → SQLite persistence
    → CSV / JSONL export
```

The program is **fully resumable** — if it is interrupted or shut down, it will pick up exactly where it left off on the next run by reading the `DISCOVERED` URL queue from the database.

---

## Architecture

```
src/
├── cli/            # Click command-line interface
├── crawler/        # Discovery, Common Crawl seeding, fetching, rate limiting, robots, pipeline
├── extraction/     # Article extractor, date filter
├── language/       # Lingua-based sentence-level Filipino language detector
├── sentence/       # Sentence segmenter and quality filter
├── deduplication/  # SHA-256 exact hashing, SimHash near-duplicate detection
└── storage/        # SQLAlchemy ORM models, DatabaseManager, Exporter
config/
├── sources.yaml    # News source registry (RSS, sitemaps, selectors)
└── crawler.yaml    # HTTP, rate limits, language, sentence, storage settings
data/
├── corpus.db       # SQLite database (auto-created on first run)
├── cache/          # HTTP response cache
├── exports/        # CSV / JSONL output files
└── logs/           # Application logs
tests/              # Offline unit tests (no live HTTP calls)
```

---

## Supported News Sources

| Publisher | Language | Method | Status |
|:---|:---|:---|:---|
| Abante | 🇵🇭 Filipino | RSS + Sitemap | ✅ Enabled |
| Bandera (Inquirer) | 🇵🇭 Filipino | RSS + Sitemap | ✅ Enabled |
| Pilipino Star Ngayon (Philstar) | 🇵🇭 Filipino | RSS + Sitemap | ✅ Enabled |
| Pang-Masa (Philstar) | 🇵🇭 Filipino | RSS + Sitemap | ✅ Enabled |
| GMA News – Balitambayan | 🇵🇭 Filipino | RSS + Sitemap | ✅ Enabled |

To add or disable sources, edit `config/sources.yaml`.

---

## Requirements

- **Python 3.11+** (tested on Python 3.14)
- **pip** (or any compatible package manager)
- **Internet connection** (for discovery and crawling)
- **~500 MB disk space** for database + cache during a long run

---

## Installation & Setup

### 1. Clone the repository

```bash
git clone https://github.com/Filly-NLP/Sentence-Pair-Scraper.git
cd Sentence-Pair-Scraper
```

### 2. Create and activate a virtual environment (recommended)

```bash
# Windows
python -m venv .venv
.venv\Scripts\activate

# macOS / Linux
python -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```powershell
pip install -r requirements.txt
```

> **Note for Windows users:** The `\` line-continuation syntax used in bash does not work in PowerShell. Always use `pip install -r requirements.txt` — never copy multi-line `pip install` commands from bash guides.

### 4. Verify installation

```bash
python -m src.cli.main --help
```

Expected output:

```
Usage: main [OPTIONS] COMMAND [ARGS]...

  Filipino News Corpus Scraper - CLI Control Panel

Commands:
  crawl     Crawl, extract, filter and store sentences.
  discover  Discover URLs from feeds/sitemaps.
  export    Export clean Filipino sentence corpus.
  stats     Display Filipino News Corpus statistics.
```

---

## First-Time Database Initialization

The database is created automatically when you first run `discover` or `crawl`. You can also initialize it manually:

```bash
python -m src.storage.migrations
```

This creates `data/corpus.db` with all required tables (WAL mode enabled for safe concurrent access).

---

## Running the Scraper

### Step 1: Discover URLs

Fetches RSS feeds and sitemaps from all enabled sources and populates the URL queue. This is fast (seconds to minutes).

```bash
# Dry run — lists sources, feeds, and sitemaps without making changes
python -m src.cli.main discover --dry-run

# Dry run for a single source
python -m src.cli.main discover --source pilipino_star_ngayon --dry-run

# Live discovery — populates the crawl queue from RSS/sitemaps
python -m src.cli.main discover

# Live discovery for a single source
python -m src.cli.main discover --source pilipino_star_ngayon

# Archive discovery dry run (opt-in archive traversal)
python -m src.cli.main discover --mode archive --source abante --from-date 2026-08-01 --to-date 2026-08-07 --granularity day --dry-run

# Live archive discovery (only for an explicitly enabled archive)
python -m src.cli.main discover --mode archive --source abante --from-date 2026-08-01 --to-date 2026-08-07 --granularity day

# Disabled-config override is explicit and remains bounded to one source/date range
python -m src.cli.main discover --mode archive --override-disabled --source abante --from-date 2026-08-01 --to-date 2026-08-07 --granularity day

# Common Crawl index dry run (always starts disabled)
python -m src.cli.main discover --mode common-crawl --source abante --dry-run
```

Example output:

```
Discovering sources...
Executing discovery on 5 enabled sources...
Discovery complete. Found 124 total URLs (124 new) across enabled sources.
```

### Step 2: Crawl & Extract

Processes every URL in the queue: fetches the page, extracts article text, segments sentences, filters for Filipino language, deduplicates, and stores results.

```bash
# Crawl all enabled sources
python -m src.cli.main crawl

# Crawl a single source only
python -m src.cli.main crawl --source bandera
python -m src.cli.main crawl --source pilipino_star_ngayon
python -m src.cli.main crawl --source abante
python -m src.cli.main crawl --source pang_masa
python -m src.cli.main crawl --source gma_filipino
```

This command is designed to run for **hours or days**. It respects per-domain rate limits automatically (e.g., 5–10 second delays between requests).

### Step 3: Monitor Progress

Check corpus statistics at any time (safe to run while crawl is running):

```bash
python -m src.cli.main stats
```

Example output:

```
Corpus Stats Summary
Discovered URL queue: 1,240
Downloaded Articles:  347
Accepted Filipino sentences: 4,821
```

### Step 4: Export Corpus

Export the collected sentences to CSV or JSONL for downstream use:

```bash
# Export all sentences to CSV (rich 19-column schema)
python -m src.cli.main export --output data/exports/corpus.csv --format csv

# Export to JSONL
python -m src.cli.main export --output data/exports/corpus.jsonl --format jsonl

# Filter by date range
python -m src.cli.main export \
  --output data/exports/corpus_aug2026.csv \
  --format csv \
  --from-date 2026-08-01 \
  --to-date 2026-08-31

# Filter by specific source
python -m src.cli.main export \
  --output data/exports/bandera_only.csv \
  --format csv \
  --source bandera

# Append new sentences to existing CSV (no duplicate header)
python -m src.cli.main export \
  --output data/exports/corpus.csv \
  --format csv \
  --append

# Auto-versioned filename (adds timestamp)
python -m src.cli.main export \
  --output data/exports/corpus.csv \
  --format csv \
  --auto-version
# Creates: data/exports/corpus_2026-08-11T120000.csv
```

### Stage 0–2 discovery audit

Stages 0–2 add structured, bounded evidence for RSS and sitemap discovery while
preserving the existing source regexes and URL queue API. Use the separate
read-only audit command to inspect configured roots, stored queue counts, and
persisted diagnostic aggregates without fetching publishers:

```bash
python -m src.cli.main audit-discovery --method all
python -m src.cli.main audit-discovery --source pang_masa --method rss
```

`discovery.diagnostics.enabled` is `false` in checked-in configuration. When
explicitly enabled, aggregate rows are bounded by `sample_cap` and
`retention_runs`; retention removes diagnostic rows only after completed runs
and never removes corpus URLs, articles, or sentences. Discovery reports
reconcile raw candidates, normalization failures, unique URLs, scope/date
rejections, database duplicates, cross-method duplicates, would-queue, and
actually-queued counts. The existing `discover --dry-run` remains a planning
display and is not an alias for this read-only audit.

Archive execution is implemented with opt-in per-source gates and remains
disabled by default. Trafilatura fallback and WARC response preservation are
implemented as opt-in sidecars, but remain disabled by default. Common Crawl
index seeding is implemented but remains disabled by two
independent gates. Stage 4A link-frontier code is implemented but remains
disabled by both gates. Archive execution fails closed when a source
has no archive config or its archive is disabled; a disabled archive can only
be overridden with an explicit source and both dates.

### Stage 4A link frontier (bounded and opt-in)

Stage 4A extracts only `<a href>` links from successfully fetched HTML article
pages. It resolves links against the final redirect URL, applies the existing
URL normalizer and source article classifier, and persists one URL row plus
durable `LINK` provenance edges for each accepted relationship. Existing URL
ownership, status, and discovery method are preserved; only depth and priority
may improve.

Runtime use requires both gates to be enabled:

```yaml
# config/crawler.yaml
link_discovery:
  enabled: false
  max_depth: 1
  max_parent_pages_per_source_run: 25
  max_candidates_per_source_run: 500
  max_links_per_page: 100
  allow_query_parameters: false
```

```yaml
# config/sources.yaml (only after a source has been reviewed)
link_discovery:
  enabled: false
  article_priority: 100
```

The checked-in global and per-source defaults keep the frontier inactive.
When enabled, crawl claims are ordered by depth, priority, and normalized URL;
robots checks, rate limits, extraction, date filtering, language filtering,
and sentence deduplication remain unchanged. Link analysis is isolated from
parent extraction failures and does not issue an additional HTTP request.

Use the offline audit to inspect a local fixture without opening SQLite or
making HTTP requests. It runs even when both runtime gates are disabled and
prints reconciled counters, rejection reasons, truncation details, and
candidates:

```bash
python -m src.cli.main audit-link-frontier \
  --source abante \
  --html-file tests/fixtures/discovery/link_frontier.html \
  --base-url https://www.abante.com.ph/redirected/parent
```

Deferred boundaries for this slice are intentional: no discovery-page role,
no arbitrary non-article frontier pages, no `discover --mode link`, and no live
source enablement. The offline audit performs no database writes; mutable
startup may still apply the project's additive schema migrations as documented
below.

### Stage 5 Common Crawl index seeding (implemented, disabled by default)

Common Crawl is an optional seed provider, not an article-text authority. It
queries only the pinned Common Crawl index endpoint and reads newline-delimited
index metadata; it never follows `filename`, `offset`, or `length` fields and
never requests WARC records or publisher bodies. Candidates are normalized,
classified, deduplicated, and queued through the existing discovery contract.
Publisher fetching remains exclusively in the robots-aware crawler.

Enablement requires both gates plus a pinned collection such as
`CC-MAIN-2026-30`; `latest` and missing collections are rejected. Each source
must provide narrow absolute URL patterns, especially for shared domains such
as Philstar. Capture timestamps are retrieval hints only and never populate
publication or sitemap-lastmod date fields.

The provider is bounded by requests, index pages, raw candidates, bytes per
response, and total response bytes per source run. Complete successful index
responses are cached in a versioned, SHA-256-checked envelope using an atomic
same-directory temporary-file replacement. Cache hits still count toward page,
byte, and candidate budgets. Configuration and source gates in the checked-in
files are intentionally false.

```yaml
# config/crawler.yaml
common_crawl:
  enabled: false
  index_collection: null       # explicit CC-MAIN-YYYY-WW when reviewed
  max_requests_per_source_run: 6
  max_index_pages: 5
  max_candidates_per_source_run: 5000
  max_response_bytes: 2097152
  max_total_response_bytes_per_source_run: 10485760
  timeout_seconds: 30
  cache_dir: data/cache/common-crawl
```

```yaml
# config/sources.yaml
common_crawl:
  enabled: false
  url_patterns:
    - https://www.abante.com.ph/20*/*/*/*
```

Inspect the plan without opening SQLite or making HTTP requests:

```bash
python -m src.cli.main discover --mode common-crawl --source abante --dry-run
```

After an explicitly approved offline/staged configuration review, the future
execution surface is:

```bash
python -m src.cli.main discover --mode common-crawl --source abante
```

The checked-in configuration remains fail-closed; no production rollout is
implied by this implementation.

### Stage 6 optional Trafilatura fallback

The publisher-aware extractor remains authoritative. When both the global
fallback and a source gate are enabled, the optional Trafilatura extractor is
called only for empty or short primary bodies. It supplies a body only; the
existing metadata, date precedence, sentence quality, language, and
deduplication pipeline are unchanged. The fallback body replaces rather than
merges with the primary body, and bounded outcomes are retained in
`urls.extraction_diagnostics`.

The base installation does not install Trafilatura. Install the optional extra
only after an offline precision review:

```powershell
pip install ".[trafilatura]"
```

```yaml
# config/crawler.yaml
extraction:
  fallback:
    trafilatura:
      enabled: false
      trigger: "empty_or_short_primary"
      min_body_chars: 200
      favor_precision: true
```

Each source has a separate `extraction.trafilatura.enabled` gate. Missing
optional dependencies are recorded as a diagnostic outcome and do not fail a
crawl. No checked-in source is enabled.

### Stage 7 optional WARC response preservation

WARC preservation is an independent, opt-in debugging/reproducibility
sidecar. It can retain selected failure responses and a deterministic sample
of successful wire responses without affecting extraction or retry behavior.
Records are gzip-compressed, written through atomic same-directory
replacement, rotated by size, bounded by response/run/disk budgets, and redact
credential-like URL query values plus sensitive request/response headers.
Retention is manual; the corpus database is never deleted by this feature.

The writer uses the standard library, so no WARC dependency is required for the
base installation. Enable it only after an explicit storage review:

```yaml
# config/crawler.yaml
warc:
  enabled: false
  directory: "data/warc"
  preserve_failures: true
  success_sample_rate: 0.0
  max_response_bytes: 10485760
  max_total_bytes_per_run: 1073741824
  max_disk_bytes: 10737418240
  rotate_bytes: 1073741824
  retention:
    mode: manual
```

WARC write and finalization failures are isolated from crawl outcomes. Each
URL keeps only a bounded sidecar reference, digest, record ID, timestamp, and
status in extraction diagnostics. Disable with `warc.enabled: false`; no live
rollout is implied by the implementation.

### Stage 8 offline coverage matrix

Stage 8 verification is offline-only, fixture-backed, and isolated from the
active `data/corpus.db` (including its WAL/SHM files). The checked-in
configuration remains fail-closed; tests do not activate production sources.

| Stage | Coverage/status | Activation |
| --- | --- | --- |
| 8A | In-memory fixture integration for RSS, nested gzip sitemap, archive, link frontier, extraction, lifecycle, and deduplication | Offline tests only |
| 8B | Common Crawl metadata plus paginated NDJSON overlap/scope checks, temporary cache, durable SQLite close/reopen restart checks, transaction-safe archive cancellation, cross-article sentence deduplication, diagnostics-failure isolation, and copied-legacy migration idempotency | Offline tests only; implemented in `tests/test_stage8_offline_integration.py`, `tests/test_archive_discovery.py`, and `tests/test_stage8b_closure.py` |
| 6 | Trafilatura fallback | Implemented; optional dependency and both gates disabled by default |
| 7 | WARC capture/replay | Implemented; bounded sidecar and gate disabled by default |
| 9 | Staged live rollout and production activation | Deferred; requires separate approval |

Stage 8B validates Common Crawl as metadata-only URL seeding. It performs no
WARC or article-body fetches through that provider and uses only temporary
test databases/cache paths.

---

## Resuming After Shutdown

The scraper is **fully resumable by design**. When you restart after an interruption:

1. **Re-run `discover`** — it will add any new articles from RSS/sitemaps since the last run (already-queued URLs are skipped automatically).
2. **Re-run `crawl`** — it processes due URLs in bounded database batches (`queue_batch_size: 100`), skipping those already marked `ACCEPTED`, `NO_SENTENCES`, `REJECTED`, `BLOCKED`, or `TERMINAL_FAILED`.

> **Note on Batches and Streaming:** `queue_batch_size: 100` in `crawler.yaml` is the database query batch size per loop iteration to conserve memory, **not** a total crawl limit. Similarly, `yield_per(1000)` in the exporter is database cursor streaming, **not** an export row cap.

### URL State Lifecycle

The scraper tracks URLs through explicit lifecycle states:
- `DISCOVERED`: Discovered from RSS feeds or sitemaps, pending crawl.
- `PROCESSING`: Currently being fetched and processed by the crawler.
- `RETRY_WAIT`: Transient failure waiting for backoff cooldown.
- `DOWNLOADED`: HTML fetched successfully, extraction in progress.
- `ACCEPTED`: Article extracted and one or more valid sentences accepted.
- `NO_SENTENCES`: Article extracted, but zero sentences passed quality/language filters.
- `REJECTED`: Article rejected due to date cutoff, empty body, or length constraints.
- `BLOCKED`: Disallowed by robots.txt or domain policy.
- `TERMINAL_FAILED`: Non-retryable error or exhausted retry budget.
- `FAILED`: Legacy status for records from previous scraper versions.

### Database Migrations and Audit

Mutable database startup initializes the schema and applies the project's
additive, idempotent migrations through `DatabaseManager.init_db()`. This is
independent of Stage 4A runtime behavior: the link frontier remains inactive
until both its global and per-source gates are enabled. The commands below
are operational mutations unless `--dry-run` is used.

```bash
# Audit pending schema migrations without applying changes
python -m src.cli.main init-db --dry-run

# Apply additive migrations, index creation, and sentence count backfills
python -m src.cli.main init-db
```

### Requeuing URLs (Dry-Run First)

To safely requeue failed or retry-waiting URLs:

```bash
# Dry run: view matching URLs without mutating state
python -m src.cli.main requeue --due --source pilipino_star_ngayon

# Execute requeue for due retries
python -m src.cli.main requeue --due --source pilipino_star_ngayon --execute

# Explicitly include terminal or blocked records
python -m src.cli.main requeue --include-terminal --include-blocked --execute
```

### Policy Profiles & Yield Tuning

The scraper supports named policy profiles and per-source policy overrides for fine-tuning precision vs. recall without modifying code:

| Profile | Token Bounds | Quotes | Headlines | Min Lang Conf | Allowed Languages | Min Quality | Description |
|:---|:---|:---|:---|:---|:---|:---|:---|
| `default` | 3 – 100 | `false` | `false` | `0.0` | `FILIPINO` | `0.0` | **Migration default:** preserves existing baseline corpus behavior. |
| `strict` | 6 – 60 | `false` | `false` | `0.85` | `FILIPINO` | `0.9` | High precision, strictly non-quoted standard news sentences. |
| `balanced` | 4 – 80 | `true` | `false` | `0.70` | `FILIPINO` | `0.7` | Balanced yield including direct quotations. |
| `recall` | 3 – 120 | `true` | `true` | `0.50` | `FILIPINO`, `MIXED` | `0.5` | Maximum yield including headlines and Taglish/mixed sentences. |

Policy profiles can be set globally in `config/crawler.yaml` (under `policy.profile`) or per-source in `config/sources.yaml` (under `policy_profile`). Specific attributes (e.g. `min_tokens`, `include_quotes`, `min_quality_score`, `noise_patterns`) can also be overridden per-source.

### Policy Evaluation (Non-Mutating)

Evaluate how different policy profiles impact yield over existing stored articles without modifying the database or crawl state:

```bash
# Evaluate balanced profile over 100 latest stored articles
python -m src.cli.main evaluate-policy --profile balanced --limit 100

# Evaluate strict profile for a specific source
python -m src.cli.main evaluate-policy --profile strict --source abante --limit 50
```

---

## Configuration Reference

### `config/sources.yaml`

Defines each news source. Key fields per source:

| Field | Description |
|:---|:---|
| `id` | Machine key (used with `--source` flag) |
| `name` | Human-readable publisher name |
| `domain` | Base domain (used for rate limiting) |
| `enabled` | `true`/`false` — whether to include in crawls |
| `language` | Expected language (`filipino`, `english`, `mixed`) |
| `crawl_delay_seconds` | Minimum seconds between requests to this domain |
| `max_concurrent` | Max parallel requests (always 1 for respectful crawling) |
| `rss[].url` | RSS/Atom feed URL |
| `sitemap[].url` | XML sitemap URL |
| `common_crawl.enabled` | Per-source Stage 5 gate; disabled by default |
| `common_crawl.url_patterns` | Narrow absolute URL patterns queried in the pinned Common Crawl index |
| `extraction.type` | Extractor adapter (`wordpress`, `philstar`, `gma`, `generic`) |
| `extraction.content_selector` | CSS selector for article body |
| `extraction.trafilatura.enabled` | Per-source Stage 6 fallback gate; disabled by default |
| `link_discovery.enabled` | Per-source Stage 4A gate; disabled by default |
| `link_discovery.article_priority` | Priority assigned to accepted linked article URLs |
| `policy_profile` | Optional named profile override (`strict`, `balanced`, `recall`) |
| `min_tokens` / `max_tokens` | Optional per-source token length overrides |
| `include_quotes` | Optional per-source boolean override for quotes |
| `include_headlines` | Optional per-source boolean override for headlines |

**To add a new source**, append an entry to `sources.yaml`. **To disable a source**, set `enabled: false`.

### `config/crawler.yaml`

Key settings (reconciled with active configuration):

| Setting | Default | Status | Description |
|:---|:---|:---|:---|
| `crawler.date_cutoff` | `2022-01-01` | Active | Reject articles published before this date |
| `crawler.queue_batch_size` | `100` | Active | Database query chunk size (not a crawl cap) |
| `crawler.processing_timeout_seconds` | `3600` | Active | Stale processing recovery timeout |
| `policy.profile` | `default` | Active | Global policy profile (`default`, `strict`, `balanced`, `recall`) |
| `http.user_agent` | `FilipinoCorpusResearchBot/1.0` | Active | Identifies the bot in HTTP headers |
| `http.timeout_seconds` | `30` | Active | Per-request timeout |
| `http.max_retries` | `3` | Active | Retry count on network failures |
| `rate_limiting.default_delay_seconds` | `5` | Active | Default inter-request delay |
| `rate_limiting.default_max_concurrent` | `1` | Active | Default per-domain concurrency |
| `rate_limiting.default_requests_per_minute` | `10` | Informational | Deprecated ceiling; pacing is enforced by delay and robots |
| `rate_limiting.default_requests_per_hour` | `300` | Informational | Deprecated ceiling; pacing is enforced by delay and robots |
| `link_discovery.enabled` | `false` | Inactive | Global Stage 4A gate; source gate must also be enabled |
| `common_crawl.enabled` | `false` | Inactive | Global Stage 5 gate; source gate and explicit collection are also required |
| `common_crawl.index_collection` | `null` | Inactive | Explicit `CC-MAIN-YYYY-WW` collection; never use `latest` |
| `common_crawl.max_requests_per_source_run` | `6` | Inactive | Outbound Common Crawl index request budget |
| `common_crawl.max_index_pages` | `5` | Inactive | Index page budget per source run |
| `common_crawl.max_candidates_per_source_run` | `5000` | Inactive | Raw NDJSON candidate budget per source run |
| `common_crawl.max_response_bytes` | `2097152` | Inactive | Per-response byte limit |
| `common_crawl.max_total_response_bytes_per_source_run` | `10485760` | Inactive | Total response byte limit |
| `common_crawl.cache_dir` | `data/cache/common-crawl` | Inactive | Atomic integrity-checked index-response cache |
| `extraction.fallback.trafilatura.enabled` | `false` | Inactive | Global optional body-only fallback gate; source gate is also required |
| `extraction.fallback.trafilatura.min_body_chars` | `200` | Inactive | Primary/fallback trigger and fallback acceptance threshold |
| `warc.enabled` | `false` | Inactive | Optional WARC sidecar gate; retention remains manual |
| `warc.preserve_failures` | `true` | Inactive | Select wire HTTP failures for preservation |
| `warc.success_sample_rate` | `0.0` | Inactive | Deterministic sample rate for successful wire responses |
| `warc.max_response_bytes` | `10485760` | Inactive | Per-response body cap |
| `warc.max_total_bytes_per_run` | `1073741824` | Inactive | Per-run body budget |
| `warc.max_disk_bytes` | `10737418240` | Inactive | WARC directory budget |
| `link_discovery.max_depth` | `1` | Inactive | Maximum parent-to-child link depth |
| `link_discovery.max_parent_pages_per_source_run` | `25` | Inactive | Parent HTML pages analyzed per source run |
| `link_discovery.max_candidates_per_source_run` | `500` | Inactive | Accepted linked candidates per source run |
| `link_discovery.max_links_per_page` | `100` | Inactive | Anchor links considered per parent page |
| `link_discovery.allow_query_parameters` | `false` | Inactive | Reject meaningful query strings by default; tracking-only parameters are normalized |
| `language.min_confidence` | `0.0` | Active | Minimum Lingua confidence to accept a sentence as Filipino |
| `sentence.min_tokens` | `3` | Active | Minimum words per accepted sentence |
| `sentence.max_tokens` | `100` | Active | Maximum words per accepted sentence |
| `sentence.include_quotes` | `false` | Active | Whether to accept quoted sentences |
| `sentence.include_headlines` | `false` | Active | Whether to extract headlines as sentences |
| `deduplication.exact_hash` | `true` | Active | SHA-256 exact sentence/article deduplication |
| `deduplication.near_duplicate` | `false` | Inactive | SimHash near-duplicate offline clustering (inactive in live pipeline) |
| `dataset.max_source_percentage` | `null` | Inactive | Post-processing balance setting (inactive in live pipeline) |
| `storage.database_url` | `sqlite:///data/corpus.db` | Active | SQLAlchemy connection string |

---

## Output Format

### CSV Schema

Produced by `export --format csv`. Contains 19 columns:

| Column | Type | Description |
|:---|:---|:---|
| `sentence_id` | string | Unique sentence identifier (`SENT_...`) |
| `sentence` | string | Original Filipino sentence text |
| `normalized_sentence` | string | Lowercased, whitespace-normalized form |
| `source_name` | string | Publisher name (e.g. `Pilipino Star Ngayon`) |
| `source_id` | string | Machine source key (e.g. `pilipino_star_ngayon`) |
| `article_id` | string | Unique article identifier (`ART_...`) |
| `headline` | string | Article headline |
| `author` | string | Article author |
| `publication_date` | string | `YYYY-MM-DD` |
| `url` | string | Source article URL |
| `language` | string | `FILIPINO` / `ENGLISH` / `MIXED` |
| `language_confidence` | float | Lingua confidence score (0.0–1.0) |
| `token_count` | int | Word count of the sentence |
| `quality_score` | float | Internal quality score (currently `1.0` for accepted) |
| `paragraph_index` | int | Paragraph position within article (0-indexed) |
| `sentence_index` | int | Sentence position within article (0-indexed) |
| `is_quote` | int | `1` if the sentence is a direct quote, `0` otherwise |
| `date_source` | string | Evidence source selected for the article publication date |
| `canonical_url` | string | Canonical URL when supplied by the article |

Export opens the corpus database read-only. CSV `--append` skips sentence IDs
already present in the target file, so repeating the same export is
idempotent; it does not remove unrelated rows already in that file.

`audit.py`, `audit-discovery`, `stats`, `evaluate-policy`, and requeue dry-runs
also use read-only database access. On a legacy database, they report the
unavailable schema-dependent fields and leave migration to an explicitly
approved copy.

### JSONL Schema

Produced by `export --format jsonl`. One JSON object per line:

```json
{
  "sentence_id": "SENT_db536ff0572b8390",
  "sentence": "Nasa 55 porsiyento ng mga Filipino ang nagsabing nais muna nilang makita ang mga ebidensiya.",
  "source": "Pilipino Star Ngayon",
  "article_id": "ART_d4f0b15d8d85",
  "publication_date": "2026-08-11",
  "date_source": "html_jsonld",
  "url": "https://www.philstar.com/bansa/2026/08/11/2548446/...",
  "canonical_url": null,
  "language": "FILIPINO",
  "language_confidence": 1.0
}
```

---

## Running Tests

The test suite runs entirely **offline** — no live HTTP requests are made.

```bash
# Run all tests with verbose output
python -m pytest tests/ -v

# Run with coverage report
python -m pytest tests/ -v --cov=src --cov-report=term-missing

# Run a single test file
python -m pytest tests/test_segmenter.py -v
```

Current test coverage:

| Module | Tests |
|:---|:---|
| `url_normalizer` | UTM stripping, fragment removal, host normalization, query sorting |
| `date_filter` | ISO 8601, RFC 2822, boundary dates, invalid inputs |
| `segmenter` | Abbreviations, decimals, quotes, multi-paragraph, edge cases |
| `quality_filter` | Token bounds, URL/email rejection, HTML artifacts, noise patterns |
| `language detector` | Filipino, English, empty input |
| `deduplication` | Exact hash match, whitespace/case normalization, SimHash distance |

---

## Project Structure

```
Sentence-Pair-Scraper/
├── config/
│   ├── sources.yaml         # News source registry
│   └── crawler.yaml         # Runtime configuration
├── src/
│   ├── cli/main.py          # CLI entry point (discover, crawl, stats, export)
│   ├── crawler/
│   │   ├── pipeline.py      # Main orchestration loop
│   │   ├── discovery.py     # RSS + Sitemap URL discovery
│   │   ├── common_crawl_discovery.py # Optional metadata-only Common Crawl seeding
│   │   ├── fetcher.py       # Async HTTP fetcher with caching
│   │   ├── robots.py        # robots.txt parser and cache
│   │   ├── rate_limiter.py  # Per-domain async rate limiting
│   │   ├── backoff.py       # Exponential backoff with jitter
│   │   ├── rss_parser.py    # RSS/Atom feed parser
│   │   ├── sitemap_parser.py# XML sitemap parser
│   │   ├── url_normalizer.py# URL normalization and tracking param stripping
│   │   ├── cache.py         # HTTP response cache (ETag/Last-Modified)
│   │   └── config.py        # crawler.yaml loader (dot-notation)
│   ├── extraction/
│   │   ├── base.py          # Article extractor (JSON-LD → OG → CSS)
│   │   ├── trafilatura_fallback.py # Optional body-only recovery extractor
│   │   └── date_filter.py   # Publication date validation
│   ├── language/
│   │   └── detector.py      # Lingua sentence-level language detector
│   ├── sentence/
│   │   ├── segmenter.py     # Filipino-aware sentence splitter
│   │   └── quality_filter.py# Token bounds, noise, symbol ratio checks
│   ├── deduplication/
│   │   └── exact.py         # SHA-256 + SimHash fingerprinting
│   └── storage/
│       ├── models.py        # SQLAlchemy ORM (sources, urls, articles, sentences)
│       ├── warc_store.py     # Optional bounded WARC response sidecar
│       ├── database.py      # DatabaseManager (WAL mode, session factory)
│       ├── migrations.py    # Schema creation script
│       └── exporter.py      # CSV and JSONL export with provenance
├── data/
│   ├── corpus.db            # SQLite database (auto-created)
│   ├── cache/               # HTTP response cache files
│   ├── exports/             # Output CSV / JSONL files
│   └── logs/                # Application logs
├── tests/
│   ├── conftest.py          # Shared pytest fixtures
│   ├── test_url_normalizer.py
│   ├── test_date_filter.py
│   ├── test_segmenter.py
│   ├── test_quality_filter.py
│   ├── test_language.py
│   └── test_deduplication.py
└── pyproject.toml           # Project metadata and dependencies
```

---

## Staged Rollout & Operations Guide

### 1. Pre-Rollout Backup & Migrations
Before deploying new crawler configurations or executing batch operations:
1. **Back up database:**
   ```bash
   # Windows PowerShell
   Copy-Item "data/corpus.db" "data/corpus_backup_$(Get-Date -Format 'yyyyMMdd_HHmmss').db"
   ```
2. **Apply schema migrations idempotently:**
   ```bash
   python -m src.cli.main init-db
   ```
   Inspect logs to verify new columns (`date_hint_source`, `sitemap_lastmod_hint`, `extraction_diagnostics`) and tables (`crawl_runs`, `crawl_events`) are created without touching existing rows.

### 2. Staged Expansion Procedure
1. **Discovery Verification (Dry-Run first, then execute):** Run discovery for a single low-impact source first:
   ```bash
   # Dry-run audit
   python -m src.cli.main discover --source remate --dry-run
   # Live discovery
   python -m src.cli.main discover --source remate
   ```
2. **Archive Discovery Verification (Dry-Run first, then execute):** Run archive discovery across a narrow date range (e.g. 7 days):
   ```bash
   # Dry-run audit of archive periods and planned pagination
   python -m src.cli.main discover --mode archive --source abante --from-date 2026-08-01 --to-date 2026-08-07 --dry-run
   # Live archive discovery
   python -m src.cli.main discover --mode archive --source abante --from-date 2026-08-01 --to-date 2026-08-07
   ```
3. **Crawl Execution with Bounded Concurrency:**
   ```bash
   python -m src.cli.main crawl --source remate
   ```
   Verify logs confirm robots compliance, configured inter-request crawl delay, and single per-domain worker isolation.
4. **Stale Record Recovery:**
   If a crawl terminates abruptly, subsequent crawl runs automatically reclaim stale `PROCESSING` and `DOWNLOADED` URLs whose processing timestamp exceeds `crawler.processing_timeout_seconds`.
5. **Reconciliation & Export:**
   ```bash
   python -m src.cli.main stats
   python -m src.cli.main export --format jsonl --output data/exports/corpus.jsonl
   ```
   Confirm that exported eligible sentence counts match database counts.

---

## Ethical Crawling Policy

This scraper is built to be a **respectful, well-behaved research bot**:

- ✅ Identifies itself transparently via `User-Agent` header
- ✅ Reads and respects each site's `robots.txt` before every request
- ✅ Honors `Crawl-delay` directives from publishers
- ✅ Uses conditional HTTP requests (`ETag`, `If-Modified-Since`) to reduce server load
- ✅ Exponential backoff on errors (429, 503)
- ✅ Single-threaded per domain (`max_concurrent: 1`)
- ❌ **Never** bypasses CAPTCHAs, Cloudflare, or paywalls
- ❌ **Never** circumvents `robots.txt` rules
- ❌ **Never** uses residential proxies or bot-masking techniques

Sources that explicitly block automated access (e.g., ABS-CBN — 403) are excluded by default.
