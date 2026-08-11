import httpx
from datetime import datetime
from typing import List, Optional
from sqlalchemy.orm import Session
from src.sources.registry import SourceConfig
from src.crawler.rss_parser import RSSParser
from src.crawler.sitemap_parser import SitemapParser
from src.crawler.url_normalizer import normalize_url
from src.storage.models import URL

class DiscoveryEngine:
    def __init__(self, db_session: Session, user_agent: str, date_cutoff: datetime):
        self.session = db_session
        self.user_agent = user_agent
        self.date_cutoff = date_cutoff
        self.headers = {"User-Agent": user_agent}

    def discover_source_urls(self, source: SourceConfig) -> int:
        """Run discovery workflows for a single source, return total new URLs added."""
        new_urls_count = 0
        
        # 1. Discover via RSS Feeds
        for rss_cfg in source.rss:
            try:
                response = httpx.get(rss_cfg.url, headers=self.headers, timeout=15.0, follow_redirects=True)
                if response.status_code == 200:
                    articles = RSSParser.parse_feed(response.text)
                    for art in articles:
                        # Date filtering (Stage 1 - Discovery)
                        pub_date = art["published_date"]
                        if pub_date and pub_date < self.date_cutoff:
                            continue
                        
                        norm_url = normalize_url(art["url"])
                        if not norm_url:
                            continue
                            
                        # Queue persistence (de-duplicate URL)
                        exists = self.session.query(URL).filter(URL.url == norm_url).first()
                        if not exists:
                            new_url = URL(
                                url=norm_url,
                                source_id=source.id,
                                status="DISCOVERED",
                                discovery_method="RSS",
                                discovered_at=datetime.utcnow()
                            )
                            self.session.add(new_url)
                            new_urls_count += 1
            except Exception:
                pass # Fail silently, record source failures at run level

        # 2. Discover via Sitemaps
        for sm_cfg in source.sitemap:
            try:
                response = httpx.get(sm_cfg.url, headers=self.headers, timeout=20.0, follow_redirects=True)
                if response.status_code == 200:
                    sitemap_entries = SitemapParser.parse_sitemap(response.text)
                    for entry in sitemap_entries:
                        lastmod = entry["lastmod"]
                        if lastmod and lastmod < self.date_cutoff:
                            continue

                        norm_url = normalize_url(entry["url"])
                        if not norm_url:
                            continue
                        
                        exists = self.session.query(URL).filter(URL.url == norm_url).first()
                        if not exists:
                            new_url = URL(
                                url=norm_url,
                                source_id=source.id,
                                status="DISCOVERED",
                                discovery_method="SITEMAP",
                                discovered_at=datetime.utcnow()
                            )
                            self.session.add(new_url)
                            new_urls_count += 1
            except Exception:
                pass

        self.session.commit()
        return new_urls_count
