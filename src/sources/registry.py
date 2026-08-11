import yaml
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field

@dataclass
class RSSFeedConfig:
    url: str
    category: str

@dataclass
class SitemapConfig:
    url: str

@dataclass
class ExtractionConfig:
    type: str
    content_selector: Optional[str] = None
    date_selector: Optional[str] = None

@dataclass
class SourceConfig:
    id: str
    name: str
    domain: str
    enabled: bool
    language: str
    crawl_delay_seconds: int = 5
    max_concurrent: int = 1
    url_prefix: Optional[str] = None
    rss: List[RSSFeedConfig] = field(default_factory=list)
    sitemap: List[SitemapConfig] = field(default_factory=list)
    extraction: Optional[ExtractionConfig] = None

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
        for src in sources_data:
            rss_feeds = [RSSFeedConfig(url=feed["url"], category=feed.get("category", "general")) 
                         for feed in src.get("rss", [])]
            sitemaps = [SitemapConfig(url=sm["url"]) for sm in src.get("sitemap", [])]
            
            ext_data = src.get("extraction")
            ext_cfg = None
            if ext_data:
                ext_cfg = ExtractionConfig(
                    type=ext_data.get("type", "generic"),
                    content_selector=ext_data.get("content_selector"),
                    date_selector=ext_data.get("date_selector")
                )

            src_cfg = SourceConfig(
                id=src["id"],
                name=src["name"],
                domain=src["domain"],
                enabled=src.get("enabled", True),
                language=src.get("language", "filipino"),
                crawl_delay_seconds=src.get("crawl_delay_seconds", 5),
                max_concurrent=src.get("max_concurrent", 1),
                url_prefix=src.get("url_prefix"),
                rss=rss_feeds,
                sitemap=sitemaps,
                extraction=ext_cfg
            )
            self.sources[src_cfg.id] = src_cfg

    def get_source(self, source_id: str) -> Optional[SourceConfig]:
        return self.sources.get(source_id)

    def list_sources(self, enabled_only: bool = False) -> List[SourceConfig]:
        if enabled_only:
            return [src for src in self.sources.values() if src.enabled]
        return list(self.sources.values())
