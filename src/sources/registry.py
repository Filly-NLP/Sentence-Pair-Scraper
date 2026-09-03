import yaml
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

from src.crawler.url_normalizer import normalize_url
from src.crawler.discovery_reporting import ScopeDecision, ScopeReason


class ConfigValidationError(ValueError):
    """Raised when configuration fails schema, format, or range validation."""
    pass


@dataclass
class RSSFeedConfig:
    url: str
    category: str = "general"


@dataclass
class SitemapConfig:
    url: str


@dataclass(frozen=True)
class LinkDiscoveryConfig:
    """Per-source opt-in for Stage 4A article-link discovery."""

    enabled: bool = False
    article_priority: int = 100


@dataclass(frozen=True)
class CommonCrawlSourceConfig:
    """Per-source gate and narrow Common Crawl URL query patterns."""

    enabled: bool = False
    url_patterns: List[str] = field(default_factory=list)

    @property
    def patterns(self) -> List[str]:
        """Compatibility alias for callers that use the shorter spelling."""

        return list(self.url_patterns)


@dataclass(frozen=True)
class TrafilaturaSourceConfig:
    """Per-source gate for the optional body-only fallback."""

    enabled: bool = False


@dataclass
class ArchiveConfig:
    enabled: bool = False
    granularity: str = "day"  # "year", "month", "day"
    url_template: Optional[str] = None
    templates: List[str] = field(default_factory=list)
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    article_link_selector: str = "a[href]"
    allowed_hosts: List[str] = field(default_factory=list)
    pagination_mode: str = "none"  # "none", "page_template", "next_link"
    page_template: Optional[str] = None
    next_link_selector: Optional[str] = None
    max_periods: int = 100
    max_pages_per_period: int = 10
    max_total_pages: int = 500
    max_response_bytes: int = 10 * 1024 * 1024
    max_candidates: int = 100000
    url_pattern: Optional[str] = None
    pagination_limit: Optional[int] = None
    date_format: Optional[str] = None


@dataclass(frozen=True)
class ExtractionConfig:
    type: str
    content_selector: Optional[str] = None
    date_selector: Optional[str] = None
    trafilatura: TrafilaturaSourceConfig = field(default_factory=TrafilaturaSourceConfig)


@dataclass
class SourceConfig:
    id: str
    name: str
    domain: str
    enabled: bool = True
    language: str = "filipino"
    crawl_delay_seconds: int = 5
    max_concurrent: int = 1
    url_prefix: Optional[str] = None
    article_path_patterns: List[str] = field(default_factory=list)
    allowed_hosts: List[str] = field(default_factory=list)
    allowed_sitemap_hosts: List[str] = field(default_factory=list)
    rss: List[RSSFeedConfig] = field(default_factory=list)
    sitemap: List[SitemapConfig] = field(default_factory=list)
    archive: Optional[ArchiveConfig] = None
    extraction: Optional[ExtractionConfig] = None
    treat_lastmod_as_publication: bool = False
    policy_profile: Optional[str] = None
    min_tokens: Optional[int] = None
    max_tokens: Optional[int] = None
    include_quotes: Optional[bool] = None
    include_headlines: Optional[bool] = None
    accepted_languages: Optional[List[str]] = None
    min_language_confidence: Optional[float] = None
    allow_mixed_language: Optional[bool] = None
    min_quality_score: Optional[float] = None
    noise_patterns: Optional[List[str]] = None
    link_discovery: LinkDiscoveryConfig = field(default_factory=LinkDiscoveryConfig)
    common_crawl: CommonCrawlSourceConfig = field(default_factory=CommonCrawlSourceConfig)

    def accepts_sitemap_url(self, url: str) -> bool:
        """Return whether url is permitted as a sitemap document location."""
        try:
            parsed = urlparse(url)
        except Exception:
            return False
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        expected = self.domain.lower().split(":", 1)[0].removeprefix("www.")
        actual = parsed.hostname.lower().removeprefix("www.")
        if actual == expected:
            return True
        allowed = {h.lower().split(":", 1)[0].removeprefix("www.") for h in (self.allowed_hosts + self.allowed_sitemap_hosts)}
        return actual in allowed

    def _is_shared_domain_cross_brand(self, path: str) -> bool:
        """Recognize known shared-publisher sections without broadening scope."""
        normalized = (path or "/").rstrip("/").lower() or "/"
        domain = self.domain.lower().removeprefix("www.")
        if domain != "philstar.com":
            return False
        source_id = self.id.lower()
        if source_id in {"pilipino_star_ngayon", "psn"}:
            return normalized == "/pang-masa" or normalized.startswith("/pang-masa/") or normalized in {
                "/punto-mo", "/police-metro",
            } or normalized.startswith("/punto-mo/") or normalized.startswith("/police-metro/")
        if source_id in {"pang_masa", "pang-masa"}:
            return normalized == "/pilipino-star-ngayon" or normalized.startswith("/pilipino-star-ngayon/") or normalized in {
                "/bansa", "/metro", "/probinsiya",
            } or normalized.startswith("/bansa/") or normalized.startswith("/metro/") or normalized.startswith("/probinsiya/")
        return False

    @staticmethod
    def _is_non_html_asset_or_utility(path: str, query: str = "") -> bool:
        clean_path = unquote(path or "/").lower()
        asset_suffixes = (
            ".7z", ".avi", ".css", ".csv", ".doc", ".docx", ".gif", ".ico",
            ".jpeg", ".jpg", ".js", ".json", ".mp3", ".mp4", ".pdf", ".png",
            ".svg", ".tar", ".txt", ".webm", ".webp", ".woff", ".woff2", ".xml",
            ".zip",
        )
        if clean_path.endswith(asset_suffixes):
            return True
        utility_segments = {
            "admin", "author", "feed", "feeds", "login", "logout", "search",
            "share", "sitemap", "tag", "tags", "wp-admin", "wp-json",
        }
        segments = {segment for segment in clean_path.split("/") if segment}
        if segments & utility_segments:
            return True
        query_lower = (query or "").lower()
        return any(query_lower.startswith(prefix) for prefix in ("s=", "q=", "search="))

    def classify_url(self, url: str) -> ScopeDecision:
        """Classify an article candidate using the stable, ordered contract."""
        if not isinstance(url, str) or not url.strip():
            return ScopeDecision(False, ScopeReason.INVALID_URL, detail="empty_or_non_string")
        try:
            parsed = urlparse(url.strip())
            scheme = parsed.scheme.lower()
            hostname = parsed.hostname
        except (TypeError, ValueError):
            return ScopeDecision(False, ScopeReason.INVALID_URL, detail="urlparse_failed")

        if not scheme or not hostname:
            return ScopeDecision(False, ScopeReason.INVALID_URL, detail="missing_scheme_or_host")
        if scheme not in {"http", "https"}:
            return ScopeDecision(False, ScopeReason.UNSUPPORTED_SCHEME, detail=scheme)

        normalized = normalize_url(url.strip()) or url.strip()
        expected = self.domain.lower().split(":", 1)[0].removeprefix("www.")
        actual = hostname.lower().removeprefix("www.")
        if actual != expected:
            return ScopeDecision(False, ScopeReason.HOST_MISMATCH, normalized_url=normalized, detail=actual)

        path = parsed.path or "/"
        if self._is_non_html_asset_or_utility(path, parsed.query):
            return ScopeDecision(False, ScopeReason.NON_ARTICLE_ASSET, normalized_url=normalized)

        clean_path = path.rstrip("/") or "/"
        prefix = ("/" + self.url_prefix.strip("/")) if self.url_prefix else None
        landing_paths = {"/"}
        if prefix:
            landing_paths.add(prefix.rstrip("/") or "/")
        # GMA navigation roots are explicitly navigation even though their
        # shared news host also serves article URLs.
        if self.id.lower() in {"gma_filipino", "gma"}:
            landing_paths.update({"/news", "/news/balitambayan", "/news/serbisyopubliko"})
        if clean_path in landing_paths:
            return ScopeDecision(False, ScopeReason.SECTION_LANDING_PAGE, normalized_url=normalized)

        if self._is_shared_domain_cross_brand(clean_path):
            return ScopeDecision(False, ScopeReason.CROSS_BRAND, normalized_url=normalized)

        if self.article_path_patterns:
            matches = []
            for pattern in self.article_path_patterns:
                try:
                    matches.append(re.fullmatch(pattern, path) is not None)
                except re.error:
                    matches.append(False)
            if not any(matches):
                return ScopeDecision(False, ScopeReason.PATH_PATTERN_MISMATCH, normalized_url=normalized)
        elif prefix:
            if not clean_path.startswith(prefix.rstrip("/") + "/"):
                return ScopeDecision(False, ScopeReason.PATH_PATTERN_MISMATCH, normalized_url=normalized)

        return ScopeDecision(True, ScopeReason.ACCEPTED, normalized_url=normalized)

    def accepts_url(self, url: str) -> bool:
        """Return whether *url* belongs to this source's article scope.

        Domains are compared without a leading ``www.`` so canonical and
        feed-host variants remain usable.  ``url_prefix`` is an article path
        boundary, not a substring: ``/pilipino-star-ngayon-extra`` must not be
        attributed to the Pilipino Star Ngayon adapter.
        """
        return self.classify_url(url).accepted

    # Adapter-facing spelling used by discovery integrations.
    def accept_url(self, url: str) -> bool:
        return self.accepts_url(url)


def validate_source_dict(src: Dict[str, Any]) -> None:
    """Validate a raw source configuration dictionary with precise error messages."""
    source_id = src.get("id")
    if not source_id or not isinstance(source_id, str) or not source_id.strip():
        raise ConfigValidationError("Source configuration missing required string 'id'")
    source_id = source_id.strip()

    name = src.get("name")
    if not name or not isinstance(name, str) or not name.strip():
        raise ConfigValidationError(f"Source '{source_id}' invalid 'name': must be a non-empty string")

    domain = src.get("domain")
    if not domain or not isinstance(domain, str) or not domain.strip():
        raise ConfigValidationError(f"Source '{source_id}' invalid 'domain': must be a non-empty string")
    if "://" in domain or "/" in domain:
        raise ConfigValidationError(f"Source '{source_id}' invalid 'domain': must be a bare hostname, got '{domain}'")

    if "crawl_delay_seconds" in src:
        delay = src["crawl_delay_seconds"]
        if not isinstance(delay, (int, float)) or delay < 0:
            raise ConfigValidationError(f"Source '{source_id}' invalid 'crawl_delay_seconds': must be a non-negative number, got {delay!r}")

    if "max_concurrent" in src:
        concurrent = src["max_concurrent"]
        if not isinstance(concurrent, int) or concurrent < 1:
            raise ConfigValidationError(f"Source '{source_id}' invalid 'max_concurrent': must be an integer >= 1, got {concurrent!r}")

    patterns = src.get("article_path_patterns")
    if patterns is not None:
        if isinstance(patterns, str):
            patterns = [patterns]
        elif not isinstance(patterns, list):
            raise ConfigValidationError(f"Source '{source_id}' invalid 'article_path_patterns': must be a list of regex strings")
        for pattern in patterns:
            if not isinstance(pattern, str):
                raise ConfigValidationError(f"Source '{source_id}' invalid 'article_path_patterns': pattern must be a string, got {pattern!r}")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ConfigValidationError(f"Source '{source_id}' invalid 'article_path_patterns': regex compilation failed for '{pattern}': {exc}")

    allowed_hosts = src.get("allowed_hosts")
    if allowed_hosts is not None:
        if isinstance(allowed_hosts, str):
            allowed_hosts = [allowed_hosts]
        elif not isinstance(allowed_hosts, list):
            raise ConfigValidationError(f"Source '{source_id}' invalid 'allowed_hosts': must be a list of host strings")
        for host in allowed_hosts:
            if not isinstance(host, str) or not host.strip():
                raise ConfigValidationError(f"Source '{source_id}' invalid 'allowed_hosts': host must be a non-empty string, got {host!r}")

    budgets = src.get("budgets") or src.get("discovery_budget")
    if budgets is not None:
        if not isinstance(budgets, dict):
            raise ConfigValidationError(f"Source '{source_id}' invalid 'budgets': must be a dictionary")
        for field_name in ("max_candidates", "max_depth", "max_documents", "max_response_bytes"):
            if field_name in budgets:
                val = budgets[field_name]
                if not isinstance(val, int) or val < 1:
                    raise ConfigValidationError(f"Source '{source_id}' invalid budget '{field_name}': must be a positive integer, got {val!r}")

    archive = src.get("archive")
    if archive is not None:
        if not isinstance(archive, dict):
            raise ConfigValidationError(f"Source '{source_id}' invalid 'archive': must be a dictionary")
        granularity = archive.get("granularity")
        if granularity is not None and granularity not in ("year", "month", "day"):
            raise ConfigValidationError(f"Source '{source_id}' invalid archive 'granularity': expected 'year', 'month', or 'day', got {granularity!r}")
        pagination_mode = archive.get("pagination_mode")
        if pagination_mode is not None and pagination_mode not in ("none", "page_template", "next_link"):
            raise ConfigValidationError(f"Source '{source_id}' invalid archive 'pagination_mode': expected 'none', 'page_template', or 'next_link', got {pagination_mode!r}")
        for field_name in ("max_periods", "max_pages_per_period", "max_total_pages", "max_response_bytes", "max_candidates", "pagination_limit"):
            if field_name in archive:
                val = archive[field_name]
                if not isinstance(val, int) or val < 1:
                    raise ConfigValidationError(f"Source '{source_id}' invalid archive '{field_name}': must be an integer >= 1, got {val!r}")
        start_date = archive.get("start_date")
        end_date = archive.get("end_date")
        s_dt, e_dt = None, None
        if start_date:
            try:
                s_dt = datetime.strptime(str(start_date), "%Y-%m-%d")
            except ValueError:
                raise ConfigValidationError(f"Source '{source_id}' invalid archive 'start_date': expected YYYY-MM-DD, got {start_date!r}")
        if end_date:
            try:
                e_dt = datetime.strptime(str(end_date), "%Y-%m-%d")
            except ValueError:
                raise ConfigValidationError(f"Source '{source_id}' invalid archive 'end_date': expected YYYY-MM-DD, got {end_date!r}")
        if s_dt and e_dt and s_dt > e_dt:
            raise ConfigValidationError(f"Source '{source_id}' invalid archive date range: 'start_date' ({start_date}) cannot be after 'end_date' ({end_date})")
        if "url_pattern" in archive:
            upat = archive["url_pattern"]
            if not isinstance(upat, str) or not upat.strip():
                raise ConfigValidationError(f"Source '{source_id}' invalid archive 'url_pattern': must be a non-empty string")

    extraction = src.get("extraction")
    if extraction is not None:
        if not isinstance(extraction, dict):
            raise ConfigValidationError(
                f"Source '{source_id}' invalid 'extraction': must be a dictionary"
            )
        trafilatura = extraction.get("trafilatura")
        if trafilatura is not None:
            if not isinstance(trafilatura, dict):
                raise ConfigValidationError(
                    f"Source '{source_id}' invalid 'extraction.trafilatura': must be a dictionary"
                )
            enabled = trafilatura.get("enabled", False)
            if not isinstance(enabled, bool):
                raise ConfigValidationError(
                    f"Source '{source_id}' invalid 'extraction.trafilatura.enabled': must be a boolean"
                )

    link_discovery = src.get("link_discovery")
    if link_discovery is not None:
        if not isinstance(link_discovery, dict):
            raise ConfigValidationError(f"Source '{source_id}' invalid 'link_discovery': must be a dictionary")
        if "enabled" in link_discovery and not isinstance(link_discovery["enabled"], bool):
            raise ConfigValidationError(f"Source '{source_id}' invalid 'link_discovery.enabled': must be a boolean")
        if "article_priority" in link_discovery:
            priority = link_discovery["article_priority"]
            if not isinstance(priority, int) or isinstance(priority, bool) or not 0 <= priority <= 10000:
                raise ConfigValidationError(
                    f"Source '{source_id}' invalid 'link_discovery.article_priority': must be an integer between 0 and 10000, got {priority!r}"
                )

    common_crawl = src.get("common_crawl")
    if common_crawl is not None:
        if not isinstance(common_crawl, dict):
            raise ConfigValidationError(
                f"Source '{source_id}' invalid 'common_crawl': must be a dictionary"
            )
        cc_enabled = common_crawl.get("enabled", False)
        if not isinstance(cc_enabled, bool):
            raise ConfigValidationError(
                f"Source '{source_id}' invalid 'common_crawl.enabled': must be a boolean"
            )
        patterns = common_crawl.get("url_patterns", [])
        if not isinstance(patterns, list):
            raise ConfigValidationError(
                f"Source '{source_id}' invalid 'common_crawl.url_patterns': must be a list of URL patterns"
            )
        if cc_enabled and not patterns:
            raise ConfigValidationError(
                f"Source '{source_id}' invalid 'common_crawl.url_patterns': at least one pattern is required when enabled"
            )

        allowed_pattern_hosts = {
            str(host).lower().split(":", 1)[0].removeprefix("www.")
            for host in [domain, *(allowed_hosts or [])]
            if isinstance(host, str) and host.strip()
        }
        for pattern in patterns:
            if not isinstance(pattern, str) or not pattern.strip():
                raise ConfigValidationError(
                    f"Source '{source_id}' invalid 'common_crawl.url_patterns': each pattern must be a non-empty string, got {pattern!r}"
                )
            parsed_pattern = urlparse(pattern.strip())
            if parsed_pattern.scheme not in {"http", "https"} or not parsed_pattern.hostname:
                raise ConfigValidationError(
                    f"Source '{source_id}' invalid 'common_crawl.url_patterns': pattern must be an absolute http(s) URL, got {pattern!r}"
                )
            actual_pattern_host = parsed_pattern.hostname.lower().removeprefix("www.")
            if actual_pattern_host not in allowed_pattern_hosts:
                raise ConfigValidationError(
                    f"Source '{source_id}' invalid 'common_crawl.url_patterns': host must match the configured source host, got {pattern!r}"
                )
            if parsed_pattern.query or parsed_pattern.fragment or parsed_pattern.params:
                raise ConfigValidationError(
                    f"Source '{source_id}' invalid 'common_crawl.url_patterns': query, fragment, and params are not allowed, got {pattern!r}"
                )
            path = parsed_pattern.path or "/"
            # A Common Crawl URL wildcard at the domain root is effectively an
            # entire-domain crawl. Shared publisher hosts must remain section
            # constrained; a dedicated publisher host may use a bounded
            # host-specific wildcard such as ``/*/*``.
            literal_path = re.sub(r"[*?]", "", path).strip("/")
            normalized_domain = str(domain).lower().removeprefix("www.").split(":", 1)[0]
            if not literal_path and normalized_domain in {"philstar.com"}:
                raise ConfigValidationError(
                    f"Source '{source_id}' invalid 'common_crawl.url_patterns': broad/shared-domain patterns require a literal path constraint, got {pattern!r}"
                )

    if "policy_profile" in src:
        prof = src["policy_profile"]
        if prof is not None and prof not in ("strict", "balanced", "recall", "default"):
            raise ConfigValidationError(f"Source '{source_id}' invalid 'policy_profile': expected 'strict', 'balanced', 'recall', or 'default', got {prof!r}")

    if "min_tokens" in src:
        mt = src["min_tokens"]
        if mt is not None and (not isinstance(mt, int) or mt < 1):
            raise ConfigValidationError(f"Source '{source_id}' invalid 'min_tokens': must be an integer >= 1, got {mt!r}")

    if "max_tokens" in src:
        mxt = src["max_tokens"]
        if mxt is not None and (not isinstance(mxt, int) or mxt < 1):
            raise ConfigValidationError(f"Source '{source_id}' invalid 'max_tokens': must be an integer >= 1, got {mxt!r}")
        if src.get("min_tokens") is not None and mxt is not None and mxt < src["min_tokens"]:
            raise ConfigValidationError(f"Source '{source_id}' invalid 'max_tokens': cannot be less than 'min_tokens' ({mxt} < {src['min_tokens']})")

    if "min_language_confidence" in src:
        mlc = src["min_language_confidence"]
        if mlc is not None and (not isinstance(mlc, (int, float)) or not (0.0 <= float(mlc) <= 1.0)):
            raise ConfigValidationError(f"Source '{source_id}' invalid 'min_language_confidence': must be between 0.0 and 1.0, got {mlc!r}")

    if "min_quality_score" in src:
        mqs = src["min_quality_score"]
        if mqs is not None and (not isinstance(mqs, (int, float)) or not (0.0 <= float(mqs) <= 1.0)):
            raise ConfigValidationError(f"Source '{source_id}' invalid 'min_quality_score': must be between 0.0 and 1.0, got {mqs!r}")

    if "noise_patterns" in src:
        npats = src["noise_patterns"]
        if npats is not None:
            if not isinstance(npats, list):
                raise ConfigValidationError(f"Source '{source_id}' invalid 'noise_patterns': must be a list of regex strings")
            for pat in npats:
                if not isinstance(pat, str):
                    raise ConfigValidationError(f"Source '{source_id}' invalid 'noise_patterns': pattern must be string")
                try:
                    re.compile(pat)
                except re.error as exc:
                    raise ConfigValidationError(f"Source '{source_id}' invalid 'noise_patterns': regex compilation failed for '{pat}': {exc}")

    for feed in src.get("rss", []):
        if not isinstance(feed, dict) or "url" not in feed or not feed["url"]:
            raise ConfigValidationError(f"Source '{source_id}' invalid 'rss': entry must have non-empty 'url'")

    for sm in src.get("sitemap", []):
        if not isinstance(sm, dict) or "url" not in sm or not sm["url"]:
            raise ConfigValidationError(f"Source '{source_id}' invalid 'sitemap': entry must have non-empty 'url'")


class SourceRegistry:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.sources: Dict[str, SourceConfig] = {}
        self.load()

    def load(self) -> None:
        if not self.config_path.exists():
            raise FileNotFoundError(f"Source configuration file not found at: {self.config_path}")
        
        with open(self.config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        sources_data = data.get("sources", [])
        if not isinstance(sources_data, list):
            raise ConfigValidationError("Top-level 'sources' in configuration must be a list")

        for src in sources_data:
            validate_source_dict(src)
            rss_feeds = [RSSFeedConfig(url=feed["url"], category=feed.get("category", "general")) 
                         for feed in src.get("rss", [])]
            sitemaps = [SitemapConfig(url=sm["url"]) for sm in src.get("sitemap", [])]
            
            ext_data = src.get("extraction")
            ext_cfg = None
            if ext_data:
                trafilatura_data = ext_data.get("trafilatura") or {}
                ext_cfg = ExtractionConfig(
                    type=ext_data.get("type", "generic"),
                    content_selector=ext_data.get("content_selector"),
                    date_selector=ext_data.get("date_selector"),
                    trafilatura=TrafilaturaSourceConfig(
                        enabled=trafilatura_data.get("enabled", False)
                    ),
                )

            arch_data = src.get("archive")
            arch_cfg = None
            if arch_data:
                templates_raw = arch_data.get("templates") or []
                if isinstance(templates_raw, str):
                    templates_list = [templates_raw]
                else:
                    templates_list = list(templates_raw)
                allowed_arch_hosts = arch_data.get("allowed_hosts") or []
                if isinstance(allowed_arch_hosts, str):
                    allowed_arch_hosts = [allowed_arch_hosts]
                arch_cfg = ArchiveConfig(
                    enabled=bool(arch_data.get("enabled", False)),
                    granularity=str(arch_data.get("granularity", "day")),
                    url_template=arch_data.get("url_template"),
                    templates=templates_list,
                    start_date=arch_data.get("start_date"),
                    end_date=arch_data.get("end_date"),
                    article_link_selector=arch_data.get("article_link_selector", "a[href]"),
                    allowed_hosts=list(allowed_arch_hosts),
                    pagination_mode=str(arch_data.get("pagination_mode", "none")),
                    page_template=arch_data.get("page_template"),
                    next_link_selector=arch_data.get("next_link_selector"),
                    max_periods=int(arch_data.get("max_periods", 100)),
                    max_pages_per_period=int(arch_data.get("max_pages_per_period", 10)),
                    max_total_pages=int(arch_data.get("max_total_pages", 500)),
                    max_response_bytes=int(arch_data.get("max_response_bytes", 10 * 1024 * 1024)),
                    max_candidates=int(arch_data.get("max_candidates", 100000)),
                    url_pattern=arch_data.get("url_pattern"),
                    pagination_limit=arch_data.get("pagination_limit"),
                    date_format=arch_data.get("date_format"),
                )

            link_data = src.get("link_discovery") or {}
            link_cfg = LinkDiscoveryConfig(
                enabled=bool(link_data.get("enabled", False)),
                article_priority=int(link_data.get("article_priority", 100)),
            )

            common_crawl_data = src.get("common_crawl") or {}
            common_crawl_cfg = CommonCrawlSourceConfig(
                enabled=common_crawl_data.get("enabled", False),
                url_patterns=list(common_crawl_data.get("url_patterns", [])),
            )

            patterns_raw = src.get("article_path_patterns") or []
            if isinstance(patterns_raw, str):
                patterns_list = [patterns_raw]
            else:
                patterns_list = list(patterns_raw)

            allowed_hosts_raw = src.get("allowed_hosts") or []
            if isinstance(allowed_hosts_raw, str):
                allowed_hosts_list = [allowed_hosts_raw]
            else:
                allowed_hosts_list = list(allowed_hosts_raw)

            allowed_sitemap_raw = src.get("allowed_sitemap_hosts") or []
            if isinstance(allowed_sitemap_raw, str):
                allowed_sitemap_list = [allowed_sitemap_raw]
            else:
                allowed_sitemap_list = list(allowed_sitemap_raw)

            src_cfg = SourceConfig(
                id=src["id"],
                name=src["name"],
                domain=src["domain"],
                enabled=src.get("enabled", True),
                language=src.get("language", "filipino"),
                crawl_delay_seconds=src.get("crawl_delay_seconds", 5),
                max_concurrent=src.get("max_concurrent", 1),
                url_prefix=src.get("url_prefix"),
                article_path_patterns=patterns_list,
                allowed_hosts=allowed_hosts_list,
                allowed_sitemap_hosts=allowed_sitemap_list,
                rss=rss_feeds,
                sitemap=sitemaps,
                archive=arch_cfg,
                link_discovery=link_cfg,
                common_crawl=common_crawl_cfg,
                extraction=ext_cfg,
                treat_lastmod_as_publication=bool(src.get("treat_lastmod_as_publication", False)),
                policy_profile=src.get("policy_profile"),
                min_tokens=src.get("min_tokens"),
                max_tokens=src.get("max_tokens"),
                include_quotes=src.get("include_quotes"),
                include_headlines=src.get("include_headlines"),
                accepted_languages=src.get("accepted_languages"),
                min_language_confidence=src.get("min_language_confidence"),
                allow_mixed_language=src.get("allow_mixed_language"),
                min_quality_score=src.get("min_quality_score"),
                noise_patterns=src.get("noise_patterns"),
            )
            self.sources[src_cfg.id] = src_cfg

    def get_source(self, source_id: str) -> Optional[SourceConfig]:
        return self.sources.get(source_id)

    def list_sources(self, enabled_only: bool = False) -> List[SourceConfig]:
        if enabled_only:
            return [src for src in self.sources.values() if src.enabled]
        return list(self.sources.values())
