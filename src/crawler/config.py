from datetime import datetime
import re
import yaml
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional
from pathlib import PureWindowsPath


_COMMON_CRAWL_COLLECTION_RE = re.compile(r"^CC-MAIN-\d{4}-(?:0[1-9]|[1-4]\d|5[0-3])$")


@dataclass(frozen=True)
class TrafilaturaFallbackConfig:
    """Validated global settings for the optional body-only fallback."""

    enabled: bool = False
    trigger: str = "empty_or_short_primary"
    min_body_chars: int = 200
    favor_precision: bool = True

    @classmethod
    def from_mapping(
        cls, settings: Optional[Mapping[str, Any]] = None
    ) -> "TrafilaturaFallbackConfig":
        values = dict(settings or {})
        _validate_trafilatura_mapping(values, prefix="extraction.fallback.trafilatura")
        return cls(
            enabled=values.get("enabled", cls.enabled),
            trigger=values.get("trigger", cls.trigger),
            min_body_chars=values.get("min_body_chars", cls.min_body_chars),
            favor_precision=values.get("favor_precision", cls.favor_precision),
        )


@dataclass(frozen=True)
class WarcConfig:
    """Validated global settings for optional WARC response preservation."""

    enabled: bool = False
    directory: str = "data/warc"
    preserve_failures: bool = True
    success_sample_rate: float = 0.0
    max_response_bytes: int = 10 * 1024 * 1024
    max_total_bytes_per_run: int = 1024 * 1024 * 1024
    max_disk_bytes: int = 10 * 1024 * 1024 * 1024
    rotate_bytes: int = 1024 * 1024 * 1024
    retention_mode: str = "manual"

    @classmethod
    def from_mapping(cls, settings: Optional[Mapping[str, Any]] = None) -> "WarcConfig":
        values = dict(settings or {})
        _validate_warc_mapping(values, prefix="warc")
        retention = values.get("retention") or {}
        return cls(
            enabled=values.get("enabled", cls.enabled),
            directory=values.get("directory", cls.directory),
            preserve_failures=values.get("preserve_failures", cls.preserve_failures),
            success_sample_rate=values.get("success_sample_rate", cls.success_sample_rate),
            max_response_bytes=values.get("max_response_bytes", cls.max_response_bytes),
            max_total_bytes_per_run=values.get(
                "max_total_bytes_per_run", cls.max_total_bytes_per_run
            ),
            max_disk_bytes=values.get("max_disk_bytes", cls.max_disk_bytes),
            rotate_bytes=values.get("rotate_bytes", cls.rotate_bytes),
            retention_mode=retention.get("mode", cls.retention_mode),
        )


# Compatibility spelling for integrations that use the acronym as an initialism.
WARCConfig = WarcConfig


@dataclass(frozen=True)
class CommonCrawlConfig:
    """Validated global limits for optional Common Crawl index seeding."""

    enabled: bool = False
    index_collection: Optional[str] = None
    max_requests_per_source_run: int = 6
    max_index_pages: int = 5
    max_candidates_per_source_run: int = 5000
    max_response_bytes: int = 2 * 1024 * 1024
    max_total_response_bytes_per_source_run: int = 10 * 1024 * 1024
    timeout_seconds: float = 30.0
    cache_dir: str = "data/cache/common-crawl"

    @classmethod
    def from_mapping(cls, settings: Optional[Mapping[str, Any]] = None) -> "CommonCrawlConfig":
        values = dict(settings or {})
        for alias, canonical in {
            "max_requests": "max_requests_per_source_run",
            "max_pages": "max_index_pages",
            "max_candidates": "max_candidates_per_source_run",
            "max_total_response_bytes": "max_total_response_bytes_per_source_run",
        }.items():
            if canonical not in values and alias in values:
                values[canonical] = values[alias]
        _validate_common_crawl_mapping(values, prefix="common_crawl")
        return cls(
            enabled=values.get("enabled", cls.enabled),
            index_collection=values.get("index_collection"),
            max_requests_per_source_run=values.get(
                "max_requests_per_source_run", cls.max_requests_per_source_run
            ),
            max_index_pages=values.get("max_index_pages", cls.max_index_pages),
            max_candidates_per_source_run=values.get(
                "max_candidates_per_source_run", cls.max_candidates_per_source_run
            ),
            max_response_bytes=values.get("max_response_bytes", cls.max_response_bytes),
            max_total_response_bytes_per_source_run=values.get(
                "max_total_response_bytes_per_source_run",
                cls.max_total_response_bytes_per_source_run,
            ),
            timeout_seconds=values.get("timeout_seconds", cls.timeout_seconds),
            cache_dir=values.get("cache_dir", cls.cache_dir),
        )


def is_common_crawl_collection(value: Any) -> bool:
    """Return whether *value* is an exact Common Crawl collection name."""

    return isinstance(value, str) and _COMMON_CRAWL_COLLECTION_RE.fullmatch(value) is not None


def _validate_common_crawl_mapping(settings: Mapping[str, Any], *, prefix: str) -> None:
    """Validate global Common Crawl settings without coercing YAML values."""

    normalized = dict(settings)
    for alias, canonical in {
        "max_requests": "max_requests_per_source_run",
        "max_pages": "max_index_pages",
        "max_candidates": "max_candidates_per_source_run",
        "max_total_response_bytes": "max_total_response_bytes_per_source_run",
    }.items():
        if canonical not in normalized and alias in normalized:
            normalized[canonical] = normalized[alias]
    settings = normalized

    enabled = settings.get("enabled", False)
    if not isinstance(enabled, bool):
        raise CrawlerConfigValidationError(f"Invalid '{prefix}.enabled': must be a boolean")

    collection = settings.get("index_collection")
    if collection is not None and not is_common_crawl_collection(collection):
        raise CrawlerConfigValidationError(
            f"Invalid '{prefix}.index_collection': expected CC-MAIN-YYYY-WW, got {collection!r}"
        )
    if enabled and collection is None:
        raise CrawlerConfigValidationError(
            f"Invalid '{prefix}.index_collection': an explicit CC-MAIN-YYYY-WW collection is required when enabled"
        )

    integer_ranges = {
        "max_requests_per_source_run": (1, 10000),
        "max_index_pages": (1, 10000),
        "max_candidates_per_source_run": (1, 1_000_000),
        "max_response_bytes": (1, 1_073_741_824),
        "max_total_response_bytes_per_source_run": (1, 4_294_967_296),
    }
    for field_name, (minimum, maximum) in integer_ranges.items():
        if field_name not in settings:
            continue
        value = settings[field_name]
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise CrawlerConfigValidationError(
                f"Invalid '{prefix}.{field_name}': must be an integer between {minimum} and {maximum}, got {value!r}"
            )

    timeout = settings.get("timeout_seconds")
    if timeout is not None and (
        isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0
    ):
        raise CrawlerConfigValidationError(
            f"Invalid '{prefix}.timeout_seconds': must be a positive number, got {timeout!r}"
        )

    cache_dir = settings.get("cache_dir")
    if cache_dir is not None and (not isinstance(cache_dir, str) or not cache_dir.strip()):
        raise CrawlerConfigValidationError(
            f"Invalid '{prefix}.cache_dir': must be a non-empty string"
        )


def _validate_trafilatura_mapping(settings: Mapping[str, Any], *, prefix: str) -> None:
    """Validate Trafilatura settings without importing the optional package."""

    enabled = settings.get("enabled", False)
    if not isinstance(enabled, bool):
        raise CrawlerConfigValidationError(f"Invalid '{prefix}.enabled': must be a boolean")

    trigger = settings.get("trigger", "empty_or_short_primary")
    if trigger != "empty_or_short_primary":
        raise CrawlerConfigValidationError(
            f"Invalid '{prefix}.trigger': expected 'empty_or_short_primary', got {trigger!r}"
        )

    minimum = settings.get("min_body_chars", 200)
    if isinstance(minimum, bool) or not isinstance(minimum, int) or not 1 <= minimum <= 10_000_000:
        raise CrawlerConfigValidationError(
            f"Invalid '{prefix}.min_body_chars': must be an integer between 1 and 10000000, got {minimum!r}"
        )

    precision = settings.get("favor_precision", True)
    if not isinstance(precision, bool):
        raise CrawlerConfigValidationError(
            f"Invalid '{prefix}.favor_precision': must be a boolean"
        )


def _is_relative_directory(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    candidate = value.strip().replace("\\", "/")
    windows = PureWindowsPath(candidate)
    if windows.is_absolute() or windows.drive:
        return False
    if candidate.startswith("/") or any(part == ".." for part in candidate.split("/")):
        return False
    return True


def _validate_warc_mapping(settings: Mapping[str, Any], *, prefix: str) -> None:
    """Validate WARC budgets and fail-closed manual retention settings."""

    for field_name in ("enabled", "preserve_failures"):
        value = settings.get(field_name, getattr(WarcConfig, field_name))
        if not isinstance(value, bool):
            raise CrawlerConfigValidationError(
                f"Invalid '{prefix}.{field_name}': must be a boolean"
            )

    directory = settings.get("directory", WarcConfig.directory)
    if not _is_relative_directory(directory):
        raise CrawlerConfigValidationError(
            f"Invalid '{prefix}.directory': must be a non-empty relative directory"
        )

    sample_rate = settings.get("success_sample_rate", WarcConfig.success_sample_rate)
    if (
        isinstance(sample_rate, bool)
        or not isinstance(sample_rate, (int, float))
        or not 0.0 <= float(sample_rate) <= 1.0
    ):
        raise CrawlerConfigValidationError(
            f"Invalid '{prefix}.success_sample_rate': must be a number between 0.0 and 1.0, got {sample_rate!r}"
        )

    byte_fields = (
        "max_response_bytes",
        "max_total_bytes_per_run",
        "max_disk_bytes",
        "rotate_bytes",
    )
    values = {}
    for field_name in byte_fields:
        value = settings.get(field_name, getattr(WarcConfig, field_name))
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise CrawlerConfigValidationError(
                f"Invalid '{prefix}.{field_name}': must be a positive integer, got {value!r}"
            )
        values[field_name] = value

    if not (
        values["max_response_bytes"]
        <= values["rotate_bytes"]
        <= values["max_total_bytes_per_run"]
        <= values["max_disk_bytes"]
    ):
        raise CrawlerConfigValidationError(
            f"Invalid '{prefix}': expected max_response_bytes <= rotate_bytes <= max_total_bytes_per_run <= max_disk_bytes"
        )

    retention = settings.get("retention", {})
    if not isinstance(retention, dict):
        raise CrawlerConfigValidationError(f"Invalid '{prefix}.retention': must be a dictionary")
    mode = retention.get("mode", "manual")
    if mode != "manual":
        raise CrawlerConfigValidationError(
            f"Invalid '{prefix}.retention.mode': only 'manual' retention is supported, got {mode!r}"
        )


class CrawlerConfigValidationError(ValueError):
    """Raised when crawler configuration fails schema, format, or range validation."""
    pass


class CrawlerConfig:
    def __init__(self, config_path: Path, validate: bool = True):
        self.config_path = config_path
        self.config: Dict[str, Any] = {}
        self.load(validate=validate)

    def load(self, validate: bool = True) -> None:
        if not self.config_path.exists():
            raise FileNotFoundError(f"Crawler configuration file not found at: {self.config_path}")
        with open(self.config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f) or {}
        if validate:
            self.validate()

    def validate(self) -> None:
        """Validate crawler configuration settings with field-specific errors."""
        cutoff = self.get("crawler.date_cutoff")
        if cutoff is not None:
            try:
                datetime.strptime(str(cutoff), "%Y-%m-%d")
            except ValueError:
                raise CrawlerConfigValidationError(f"Invalid 'crawler.date_cutoff': expected YYYY-MM-DD, got {cutoff!r}")

        batch_size = self.get("crawler.queue_batch_size")
        if batch_size is not None:
            if not isinstance(batch_size, int) or batch_size < 1:
                raise CrawlerConfigValidationError(f"Invalid 'crawler.queue_batch_size': must be an integer >= 1, got {batch_size!r}")

        timeout = self.get("crawler.processing_timeout_seconds")
        if timeout is not None:
            if not isinstance(timeout, (int, float)) or timeout < 1:
                raise CrawlerConfigValidationError(f"Invalid 'crawler.processing_timeout_seconds': must be a number >= 1, got {timeout!r}")

        max_life_retries = self.get("crawler.max_lifecycle_retries")
        if max_life_retries is not None:
            if not isinstance(max_life_retries, int) or max_life_retries < 0:
                raise CrawlerConfigValidationError(f"Invalid 'crawler.max_lifecycle_retries': must be an integer >= 0, got {max_life_retries!r}")

        max_403 = self.get("crawler.max_403_retries")
        if max_403 is not None:
            if not isinstance(max_403, int) or max_403 < 0:
                raise CrawlerConfigValidationError(f"Invalid 'crawler.max_403_retries': must be an integer >= 0, got {max_403!r}")

        http_timeout = self.get("http.timeout_seconds")
        if http_timeout is not None:
            if not isinstance(http_timeout, (int, float)) or http_timeout <= 0:
                raise CrawlerConfigValidationError(f"Invalid 'http.timeout_seconds': must be a positive number, got {http_timeout!r}")

        retries = self.get("http.max_retries")
        if retries is not None:
            if not isinstance(retries, int) or retries < 0:
                raise CrawlerConfigValidationError(f"Invalid 'http.max_retries': must be an integer >= 0, got {retries!r}")

        delay = self.get("rate_limiting.default_delay_seconds")
        if delay is not None:
            if not isinstance(delay, (int, float)) or delay < 0:
                raise CrawlerConfigValidationError(f"Invalid 'rate_limiting.default_delay_seconds': must be >= 0, got {delay!r}")

        concurrency = self.get("rate_limiting.default_max_concurrent")
        if concurrency is not None:
            if not isinstance(concurrency, int) or concurrency < 1:
                raise CrawlerConfigValidationError(f"Invalid 'rate_limiting.default_max_concurrent': must be an integer >= 1, got {concurrency!r}")

        link_discovery = self.get("link_discovery")
        if link_discovery is not None:
            if not isinstance(link_discovery, dict):
                raise CrawlerConfigValidationError("Invalid 'link_discovery': must be a dictionary")
            if "enabled" in link_discovery and not isinstance(link_discovery["enabled"], bool):
                raise CrawlerConfigValidationError("Invalid 'link_discovery.enabled': must be a boolean")
            if "allow_query_parameters" in link_discovery and not isinstance(link_discovery["allow_query_parameters"], bool):
                raise CrawlerConfigValidationError("Invalid 'link_discovery.allow_query_parameters': must be a boolean")
            integer_ranges = {
                "max_depth": (0, 10),
                "max_parent_pages_per_source_run": (1, 10000),
                "max_candidates_per_source_run": (1, 1000000),
                "max_links_per_page": (1, 10000),
            }
            for field_name, (minimum, maximum) in integer_ranges.items():
                if field_name not in link_discovery:
                    continue
                value = link_discovery[field_name]
                if not isinstance(value, int) or isinstance(value, bool) or not (minimum <= value <= maximum):
                    raise CrawlerConfigValidationError(
                        f"Invalid 'link_discovery.{field_name}': must be an integer between {minimum} and {maximum}, got {value!r}"
                    )

        common_crawl = self.get("common_crawl")
        if common_crawl is not None:
            if not isinstance(common_crawl, dict):
                raise CrawlerConfigValidationError("Invalid 'common_crawl': must be a dictionary")
            _validate_common_crawl_mapping(common_crawl, prefix="common_crawl")

        extraction = self.get("extraction")
        if extraction is not None:
            if not isinstance(extraction, dict):
                raise CrawlerConfigValidationError("Invalid 'extraction': must be a dictionary")
            fallback = extraction.get("fallback")
            if fallback is not None:
                if not isinstance(fallback, dict):
                    raise CrawlerConfigValidationError(
                        "Invalid 'extraction.fallback': must be a dictionary"
                    )
                trafilatura = fallback.get("trafilatura")
                if trafilatura is not None:
                    if not isinstance(trafilatura, dict):
                        raise CrawlerConfigValidationError(
                            "Invalid 'extraction.fallback.trafilatura': must be a dictionary"
                        )
                    _validate_trafilatura_mapping(
                        trafilatura,
                        prefix="extraction.fallback.trafilatura",
                    )

        warc = self.get("warc")
        if warc is not None:
            if not isinstance(warc, dict):
                raise CrawlerConfigValidationError("Invalid 'warc': must be a dictionary")
            _validate_warc_mapping(warc, prefix="warc")

        diagnostics = self.get("discovery.diagnostics")
        if diagnostics is not None:
            if not isinstance(diagnostics, dict):
                raise CrawlerConfigValidationError("Invalid 'discovery.diagnostics': must be a dictionary")
            if "enabled" in diagnostics and not isinstance(diagnostics["enabled"], bool):
                raise CrawlerConfigValidationError("Invalid 'discovery.diagnostics.enabled': must be a boolean")
            aggregates = diagnostics.get("aggregates")
            if aggregates is not None and not isinstance(aggregates, (bool, dict)):
                raise CrawlerConfigValidationError("Invalid 'discovery.diagnostics.aggregates': must be a boolean or dictionary")
            for field_name in ("sample_cap", "sample_urls_per_reason", "retention_runs", "retention_runs_per_source"):
                if field_name in diagnostics:
                    value = diagnostics[field_name]
                    if not isinstance(value, int) or value < 0:
                        raise CrawlerConfigValidationError(
                            f"Invalid 'discovery.diagnostics.{field_name}': must be an integer >= 0, got {value!r}"
                        )

        profile = self.get("policy.profile") or self.get("sentence.policy_profile")
        if profile is not None and profile not in ("strict", "balanced", "recall", "default"):
            raise CrawlerConfigValidationError(f"Invalid 'policy.profile': expected 'strict', 'balanced', 'recall', or 'default', got {profile!r}")

        min_tokens = self.get("sentence.min_tokens")
        if min_tokens is not None:
            if not isinstance(min_tokens, int) or min_tokens < 1:
                raise CrawlerConfigValidationError(f"Invalid 'sentence.min_tokens': must be an integer >= 1, got {min_tokens!r}")

        max_tokens = self.get("sentence.max_tokens")
        if max_tokens is not None:
            if not isinstance(max_tokens, int) or max_tokens < 1:
                raise CrawlerConfigValidationError(f"Invalid 'sentence.max_tokens': must be an integer >= 1, got {max_tokens!r}")
            if min_tokens is not None and isinstance(min_tokens, int) and max_tokens < min_tokens:
                raise CrawlerConfigValidationError(f"Invalid 'sentence.max_tokens': cannot be less than 'sentence.min_tokens' ({max_tokens} < {min_tokens})")

        min_quality = self.get("sentence.min_quality_score")
        if min_quality is not None:
            if not isinstance(min_quality, (int, float)) or not (0.0 <= float(min_quality) <= 1.0):
                raise CrawlerConfigValidationError(f"Invalid 'sentence.min_quality_score': must be a float between 0.0 and 1.0, got {min_quality!r}")

        min_conf = self.get("language.min_confidence")
        if min_conf is not None:
            if not isinstance(min_conf, (int, float)) or not (0.0 <= float(min_conf) <= 1.0):
                raise CrawlerConfigValidationError(f"Invalid 'language.min_confidence': must be a float between 0.0 and 1.0, got {min_conf!r}")

        noise_pats = self.get("sentence.noise_patterns")
        if noise_pats is not None:
            if not isinstance(noise_pats, list):
                raise CrawlerConfigValidationError(f"Invalid 'sentence.noise_patterns': must be a list of regex strings")
            for pat in noise_pats:
                if not isinstance(pat, str):
                    raise CrawlerConfigValidationError(f"Invalid 'sentence.noise_patterns': pattern must be string, got {pat!r}")
                try:
                    re.compile(pat)
                except re.error as exc:
                    raise CrawlerConfigValidationError(f"Invalid 'sentence.noise_patterns': regex compilation failed for '{pat}': {exc}")

    def get(self, key: str, default: Any = None) -> Any:
        # Support dot-notation access (e.g. get("http.user_agent"))
        parts = key.split(".")
        val = self.config
        for part in parts:
            if isinstance(val, dict):
                val = val.get(part)
            else:
                return default
            if val is None:
                return default
        return val
