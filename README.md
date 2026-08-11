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
├── crawler/        # Discovery, fetching, rate limiting, robots, pipeline
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
# Dry run — lists sources without making any changes
python -m src.cli.main discover --dry-run

# Live discovery — populates the crawl queue
python -m src.cli.main discover
```

Example output:

```
Discovering sources...
Executing discovery on 5 enabled sources...
Discovery complete. Added 124 new URLs to crawl queue.
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
# Export all sentences to CSV (rich 17-column schema)
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

---

## Resuming After Shutdown

The scraper is **fully resumable by design**. When you restart after an interruption:

1. **Re-run `discover`** — it will add any new articles from RSS/sitemaps since the last run (already-queued URLs are skipped automatically).
2. **Re-run `crawl`** — it picks up only `DISCOVERED` URLs, skipping those already marked `ACCEPTED`, `FAILED`, or `BLOCKED`.

```bash
# Standard resume workflow
python -m src.cli.main discover
python -m src.cli.main crawl
```

No flags or resets needed. The SQLite database (`data/corpus.db`) preserves all state.

> **Tip:** For long-running sessions (days), run both commands in a loop or schedule them with a task scheduler (Windows Task Scheduler / cron).

### Resetting the Queue (Optional)

If you want to re-crawl previously failed URLs:

```bash
# Open the database and reset failed URLs (run in Python REPL)
from src.storage.database import DatabaseManager
from src.storage.models import URL
from pathlib import Path

db = DatabaseManager("sqlite:///data/corpus.db")
with db.get_session() as s:
    s.query(URL).filter(URL.status == "FAILED").update({"status": "DISCOVERED"})
    s.commit()
    print("Reset complete.")
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
| `extraction.type` | Extractor adapter (`wordpress`, `philstar`, `gma`, `generic`) |
| `extraction.content_selector` | CSS selector for article body |

**To add a new source**, append an entry to `sources.yaml`. **To disable a source**, set `enabled: false`.

### `config/crawler.yaml`

Key settings:

| Setting | Default | Description |
|:---|:---|:---|
| `crawler.date_cutoff` | `2022-01-01` | Reject articles published before this date |
| `http.user_agent` | `FilipinoCorpusResearchBot/1.0` | Identifies the bot in HTTP headers |
| `http.timeout_seconds` | `30` | Per-request timeout |
| `http.max_retries` | `3` | Retry count on network failures |
| `rate_limiting.default_delay_seconds` | `5` | Default inter-request delay |
| `language.min_confidence` | `0.7` | Minimum Lingua confidence to accept a sentence as Filipino |
| `sentence.min_tokens` | `5` | Minimum words per accepted sentence |
| `sentence.max_tokens` | `80` | Maximum words per accepted sentence |
| `storage.database_url` | `sqlite:///data/corpus.db` | SQLAlchemy connection string |

---

## Output Format

### CSV Schema

Produced by `export --format csv`. Contains 17 columns:

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

### JSONL Schema

Produced by `export --format jsonl`. One JSON object per line:

```json
{
  "sentence_id": "SENT_db536ff0572b8390",
  "sentence": "Nasa 55 porsiyento ng mga Filipino ang nagsabing nais muna nilang makita ang mga ebidensiya.",
  "source": "Pilipino Star Ngayon",
  "article_id": "ART_d4f0b15d8d85",
  "publication_date": "2026-08-11",
  "url": "https://www.philstar.com/bansa/2026/08/11/2548446/...",
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
