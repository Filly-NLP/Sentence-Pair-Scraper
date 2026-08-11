import asyncio
from datetime import datetime
import hashlib
from typing import Optional, List
from sqlalchemy.orm import Session
from src.sources.registry import SourceConfig
from src.crawler.config import CrawlerConfig
from src.crawler.discovery import DiscoveryEngine
from src.crawler.fetcher import HTTPFetcher
from src.crawler.robots import RobotsManager
from src.crawler.rate_limiter import RateLimiter
from src.crawler.cache import HTTPCache
from src.extraction.base import ArticleExtractor
from src.extraction.date_filter import DateFilter
from src.language.detector import FilipinoLanguageDetector
from src.sentence.segmenter import SentenceSegmenter
from src.sentence.quality_filter import SentenceQualityFilter
from src.deduplication.exact import DeduplicationEngine
from src.storage.models import URL, Article, Sentence, Source

class CrawlPipeline:
    def __init__(self, db_session: Session, config: CrawlerConfig, user_agent: str):
        self.session = db_session
        self.config = config
        self.user_agent = user_agent
        
        # Parse Cutoff
        cutoff_str = self.config.get("crawler.date_cutoff", "2022-01-01")
        self.cutoff_date = datetime.strptime(cutoff_str, "%Y-%m-%d")

        # Initialize helper modules
        self.robots_mgr = RobotsManager(user_agent=self.user_agent)
        self.rate_limiter = RateLimiter(
            default_delay=float(self.config.get("rate_limiting.default_delay_seconds", 5.0)),
            default_concurrency=int(self.config.get("rate_limiting.default_max_concurrent", 1))
        )
        
        self.cache = HTTPCache(cache_dir=self.config.get("cache.http_cache_dir", "data/cache"))
        self.fetcher = HTTPFetcher(user_agent=self.user_agent, cache=self.cache)
        self.date_filter = DateFilter(cutoff_date=self.cutoff_date)
        
        self.lang_detector = FilipinoLanguageDetector(
            min_confidence=float(self.config.get("language.min_confidence", 0.7))
        )
        
        self.quality_filter = SentenceQualityFilter(
            min_tokens=int(self.config.get("sentence.min_tokens", 5)),
            max_tokens=int(self.config.get("sentence.max_tokens", 80))
        )

    def discover_urls(self, sources: List[SourceConfig]) -> int:
        """Fetch feeds/sitemaps and register discovered article URLs."""
        discovery = DiscoveryEngine(
            db_session=self.session,
            user_agent=self.user_agent,
            date_cutoff=self.cutoff_date
        )
        
        total_discovered = 0
        for src in sources:
            # Sync source metadata table
            exists = self.session.query(Source).filter(Source.source_id == src.id).first()
            if not exists:
                db_src = Source(
                    source_id=src.id,
                    name=src.name,
                    domain=src.domain,
                    enabled=src.enabled,
                    language=src.language
                )
                self.session.add(db_src)
            
            count = discovery.discover_source_urls(src)
            total_discovered += count
            
        self.session.commit()
        return total_discovered

    async def crawl_queued_urls(self, source_id: Optional[str] = None) -> None:
        """Fetch article content, extract body, segment sentences, filter quality, and persist."""
        # Query next queued urls
        query = self.session.query(URL).filter(URL.status == "DISCOVERED")
        if source_id:
            query = query.filter(URL.source_id == source_id)
            
        queued_records = query.all()
        if not queued_records:
            return

        for url_record in queued_records:
            url = url_record.url
            domain = url_record.source_id
            
            # 1. Robots.txt clearance check
            if not self.robots_mgr.is_allowed(url):
                url_record.status = "BLOCKED"
                url_record.error_reason = "robots_disallowed"
                self.session.commit()
                continue

            # 2. Setup domain specific delay overrides if active
            # Spacing delay spacing
            delay = self.robots_mgr.get_crawl_delay(domain) or 5.0
            self.rate_limiter.set_domain_limit(domain, delay=delay, concurrency=1)

            # Wait matching rate limit limits
            await self.rate_limiter.wait_if_needed(url)
            
            url_record.status = "PROCESSING"
            self.session.commit()
            
            # 3. HTTP Get Request
            fetch_result = await self.fetcher.fetch(url)
            if fetch_result["status"] != 200:
                url_record.status = "FAILED"
                url_record.error_reason = fetch_result.get("error") or f"HTTP_{fetch_result['status']}"
                self.session.commit()
                continue
                
            url_record.status = "DOWNLOADED"
            html = fetch_result["html"]
            
            # Fetch source config details
            # Get dummy config or default generic layout adapter
            from src.sources.registry import SourceRegistry
            from pathlib import Path
            registry = SourceRegistry(Path("config/sources.yaml"))
            src_cfg = registry.get_source(url_record.source_id)
            if not src_cfg:
                continue

            # 4. Extract Article content
            extracted = ArticleExtractor.extract(html, src_cfg)
            
            # Parse & Filter by Date (Stage 2 & 3 Date validation)
            pub_date = self.date_filter.parse_date(extracted["publication_date_raw"])
            if not pub_date or not self.date_filter.is_valid(pub_date):
                url_record.status = "REJECTED"
                url_record.error_reason = "date_cutoff_or_invalid"
                self.session.commit()
                continue

            # Check duplication of entire article contents
            body_hash = DeduplicationEngine.compute_sha256(extracted["article_text"])
            art_exists = self.session.query(Article).filter(Article.content_hash == body_hash).first()
            
            if art_exists:
                url_record.status = "ACCEPTED"
                self.session.commit()
                continue

            art_id = f"ART_{hashlib.md5(url.encode()).hexdigest()[:12]}"
            db_art = Article(
                article_id=art_id,
                source_id=url_record.source_id,
                url_id=url_record.url_id,
                url=url,
                headline=extracted["headline"],
                author=extracted["author"],
                publication_date=pub_date,
                article_text=extracted["article_text"],
                content_hash=body_hash,
                is_duplicate=False
            )
            self.session.add(db_art)

            # 5. Segment into sentences
            sentences = SentenceSegmenter.split_sentences(extracted["article_text"])
            
            for sent in sentences:
                sent_text = sent["sentence_text"]
                
                # Check Quality Constraints (Phase 10)
                if not self.quality_filter.is_clean(sent_text):
                    continue

                # Detect language (Phase 8 - sentence level)
                lang_label, lang_conf = self.lang_detector.detect_sentence_language(sent_text)
                
                # Target clean FILIPINO sentence extraction
                if lang_label != "FILIPINO" or lang_conf < 0.7:
                    continue

                # Exact duplicate verification
                sent_hash = DeduplicationEngine.compute_sha256(sent_text)
                sent_exists = self.session.query(Sentence).filter(Sentence.content_hash == sent_hash).first()
                if sent_exists:
                    continue
                
                sent_id = f"SENT_{hashlib.md5(sent_text.encode()).hexdigest()[:16]}"
                db_sent = Sentence(
                    sentence_id=sent_id,
                    article_id=art_id,
                    source_id=url_record.source_id,
                    sentence_index=sent["sentence_index"],
                    paragraph_index=sent["paragraph_index"],
                    sentence_text=sent_text,
                    normalized_text=sent_text.lower(),
                    language=lang_label,
                    language_confidence=lang_conf,
                    token_count=len(sent_text.split()),
                    quality_score=1.0,
                    is_quote=sent["is_quote"],
                    content_hash=sent_hash,
                    is_duplicate=False
                )
                self.session.add(db_sent)
                
            url_record.status = "ACCEPTED"
            self.session.commit()
