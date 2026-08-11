from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, Boolean, Float, DateTime, Text, ForeignKey, UniqueConstraint
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

Base = declarative_base()

class Source(Base):
    __tablename__ = "sources"

    source_id = Column(String(50), primary_key=True)
    name = Column(String(100), nullable=False)
    domain = Column(String(100), nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)
    status = Column(String(20), default="ACTIVE", nullable=False) # ACTIVE, COOLDOWN, DISABLED
    language = Column(String(20), default="filipino", nullable=False)
    config_json = Column(Text, nullable=True)
    last_success = Column(DateTime, nullable=True)
    last_failure = Column(DateTime, nullable=True)
    error_count_429 = Column(Integer, default=0, nullable=False)
    error_count_403 = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    urls = relationship("URL", back_populates="source", cascade="all, delete-orphan")
    articles = relationship("Article", back_populates="source", cascade="all, delete-orphan")

class URL(Base):
    __tablename__ = "urls"

    url_id = Column(Integer, primary_key=True, autoincrement=True)
    url = Column(Text, unique=True, nullable=False)
    canonical_url = Column(Text, nullable=True)
    source_id = Column(String(50), ForeignKey("sources.source_id"), nullable=False)
    status = Column(String(20), default="DISCOVERED", nullable=False)
    discovery_method = Column(String(20), default="RSS", nullable=False) # RSS, SITEMAP, ARCHIVE
    discovered_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    fetched_at = Column(DateTime, nullable=True)
    content_hash = Column(String(64), nullable=True)
    error_reason = Column(Text, nullable=True)
    retry_count = Column(Integer, default=0, nullable=False)
    next_retry_at = Column(DateTime, nullable=True)

    source = relationship("Source", back_populates="urls")
    article = relationship("Article", back_populates="url_rel", uselist=False, cascade="all, delete-orphan")

class Article(Base):
    __tablename__ = "articles"

    article_id = Column(String(50), primary_key=True) # e.g. ART_00000001
    source_id = Column(String(50), ForeignKey("sources.source_id"), nullable=False)
    url_id = Column(Integer, ForeignKey("urls.url_id"), nullable=False)
    url = Column(Text, nullable=False)
    canonical_url = Column(Text, nullable=True)
    headline = Column(Text, nullable=True)
    author = Column(String(100), nullable=True)
    publication_date = Column(DateTime, nullable=True)
    modified_date = Column(DateTime, nullable=True)
    category = Column(String(100), nullable=True)
    section = Column(String(100), nullable=True)
    language = Column(String(20), nullable=True)
    language_confidence = Column(Float, nullable=True)
    article_text = Column(Text, nullable=False)
    content_hash = Column(String(64), unique=True, nullable=False)
    is_duplicate = Column(Boolean, default=False, nullable=False)
    duplicate_group_id = Column(String(50), nullable=True)
    retrieved_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    source = relationship("Source", back_populates="articles")
    url_rel = relationship("URL", back_populates="article")
    sentences = relationship("Sentence", back_populates="article", cascade="all, delete-orphan")

class Sentence(Base):
    __tablename__ = "sentences"

    sentence_id = Column(String(50), primary_key=True) # e.g. SENT_00000001
    article_id = Column(String(50), ForeignKey("articles.article_id"), nullable=False)
    source_id = Column(String(50), ForeignKey("sources.source_id"), nullable=False)
    sentence_index = Column(Integer, nullable=False)
    paragraph_index = Column(Integer, nullable=False)
    sentence_text = Column(Text, nullable=False)
    normalized_text = Column(Text, nullable=False)
    language = Column(String(20), nullable=True)
    language_confidence = Column(Float, nullable=True)
    token_count = Column(Integer, nullable=False)
    quality_score = Column(Float, nullable=False)
    is_quote = Column(Boolean, default=False, nullable=False)
    is_headline = Column(Boolean, default=False, nullable=False)
    content_hash = Column(String(64), unique=True, nullable=False)
    is_duplicate = Column(Boolean, default=False, nullable=False)
    duplicate_group_id = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    article = relationship("Article", back_populates="sentences")

class CrawlRun(Base):
    __tablename__ = "crawl_runs"

    crawl_id = Column(String(50), primary_key=True)
    source_id = Column(String(50), ForeignKey("sources.source_id"), nullable=True)
    mode = Column(String(20), nullable=False) # discover, crawl, monitor
    start_time = Column(DateTime, default=datetime.utcnow, nullable=False)
    end_time = Column(DateTime, nullable=True)
    date_range_from = Column(String(20), nullable=True)
    date_range_to = Column(String(20), nullable=True)
    urls_discovered = Column(Integer, default=0, nullable=False)
    urls_fetched = Column(Integer, default=0, nullable=False)
    articles_extracted = Column(Integer, default=0, nullable=False)
    sentences_accepted = Column(Integer, default=0, nullable=False)
    sentences_rejected = Column(Integer, default=0, nullable=False)
    crawler_version = Column(String(20), nullable=False)
    config_hash = Column(String(64), nullable=False)

    events = relationship("CrawlEvent", back_populates="crawl_run", cascade="all, delete-orphan")

class CrawlEvent(Base):
    __tablename__ = "crawl_events"

    event_id = Column(Integer, primary_key=True, autoincrement=True)
    crawl_id = Column(String(50), ForeignKey("crawl_runs.crawl_id"), nullable=False)
    source_id = Column(String(50), ForeignKey("sources.source_id"), nullable=True)
    url = Column(Text, nullable=True)
    event_type = Column(String(50), nullable=False) # error, rate_limit, success
    http_status = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)

    crawl_run = relationship("CrawlRun", back_populates="events")
