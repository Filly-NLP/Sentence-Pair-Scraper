from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, Boolean, Float, DateTime, Text, ForeignKey, UniqueConstraint, Index
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

Base = declarative_base()


class URLStatus:
    DISCOVERED = "DISCOVERED"
    PROCESSING = "PROCESSING"
    RETRY_WAIT = "RETRY_WAIT"
    DOWNLOADED = "DOWNLOADED"
    ACCEPTED = "ACCEPTED"
    NO_SENTENCES = "NO_SENTENCES"
    REJECTED = "REJECTED"
    BLOCKED = "BLOCKED"
    TERMINAL_FAILED = "TERMINAL_FAILED"

    # Legacy status for backwards compatibility
    FAILED = "FAILED"

    ACTIVE_STATES = {DISCOVERED, PROCESSING, RETRY_WAIT, DOWNLOADED}
    TERMINAL_STATES = {ACCEPTED, NO_SENTENCES, REJECTED, BLOCKED, TERMINAL_FAILED, FAILED}
    ALL = ACTIVE_STATES | TERMINAL_STATES


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
    cooldown_until = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    urls = relationship("URL", back_populates="source", cascade="all, delete-orphan")
    articles = relationship("Article", back_populates="source", cascade="all, delete-orphan")
    discovery_observations = relationship("DiscoveryObservation", back_populates="source")

class URL(Base):
    __tablename__ = "urls"
    __table_args__ = (
        Index("ix_urls_due_work", "status", "next_retry_at", "source_id", "url_id"),
        Index("ix_urls_frontier_order", "status", "discovery_depth", "frontier_priority", "url"),
    )

    url_id = Column(Integer, primary_key=True, autoincrement=True)
    url = Column(Text, unique=True, nullable=False)
    canonical_url = Column(Text, nullable=True)
    source_id = Column(String(50), ForeignKey("sources.source_id"), nullable=False)
    status = Column(String(20), default=URLStatus.DISCOVERED, nullable=False)
    discovery_method = Column(String(20), default="RSS", nullable=False) # RSS, SITEMAP, ARCHIVE
    discovery_depth = Column(Integer, default=0, nullable=False)
    frontier_priority = Column(Integer, default=0, nullable=False)
    discovered_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    fetched_at = Column(DateTime, nullable=True)
    # Publication time supplied by a trusted RSS entry; HTML metadata remains
    # authoritative when present, but this hint allows date fallback.
    publication_date_hint = Column(DateTime, nullable=True)
    sitemap_lastmod_hint = Column(DateTime, nullable=True)
    date_hint_source = Column(String(50), nullable=True)
    date_hint_confidence = Column(Float, nullable=True)
    content_hash = Column(String(64), nullable=True)
    error_reason = Column(Text, nullable=True)
    retry_count = Column(Integer, default=0, nullable=False)
    next_retry_at = Column(DateTime, nullable=True)
    last_http_status = Column(Integer, nullable=True)
    failure_class = Column(String(50), nullable=True)
    last_attempt_at = Column(DateTime, nullable=True)
    processing_started_at = Column(DateTime, nullable=True)
    sentence_count = Column(Integer, default=0, nullable=False)
    extraction_diagnostics = Column(Text, nullable=True)

    source = relationship("Source", back_populates="urls")
    article = relationship("Article", back_populates="url_rel", uselist=False, cascade="all, delete-orphan")
    discovery_edges_from = relationship(
        "URLDiscoveryEdge",
        foreign_keys="URLDiscoveryEdge.from_url_id",
        back_populates="from_url",
    )
    discovery_edges_to = relationship(
        "URLDiscoveryEdge",
        foreign_keys="URLDiscoveryEdge.to_url_id",
        back_populates="to_url",
    )

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
    date_source = Column(String(20), nullable=True)
    sentence_count = Column(Integer, default=0, nullable=False)
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
    discovery_observations = relationship("DiscoveryObservation", back_populates="crawl_run")
    discovery_edges = relationship("URLDiscoveryEdge", back_populates="crawl_run")

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


class URLDiscoveryEdge(Base):
    """Durable link provenance between two normalized article candidates."""

    __tablename__ = "url_discovery_edges"
    __table_args__ = (
        UniqueConstraint(
            "from_url_id", "to_url_id", "discovery_method",
            name="uq_url_discovery_edges_provenance",
        ),
        Index("ix_url_discovery_edges_from_depth", "from_url_id", "depth"),
        Index("ix_url_discovery_edges_to_url", "to_url_id"),
        Index("ix_url_discovery_edges_crawl_method", "crawl_id", "discovery_method"),
    )

    edge_id = Column(Integer, primary_key=True, autoincrement=True)
    from_url_id = Column(Integer, ForeignKey("urls.url_id"), nullable=False)
    to_url_id = Column(Integer, ForeignKey("urls.url_id"), nullable=False)
    crawl_id = Column(String(50), ForeignKey("crawl_runs.crawl_id"), nullable=True)
    discovery_method = Column(String(20), default="LINK", nullable=False)
    depth = Column(Integer, default=0, nullable=False)
    link_text = Column(String(500), nullable=True)
    rel = Column(String(200), nullable=True)
    discovered_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    from_url = relationship(
        "URL", foreign_keys=[from_url_id], back_populates="discovery_edges_from"
    )
    to_url = relationship(
        "URL", foreign_keys=[to_url_id], back_populates="discovery_edges_to"
    )
    crawl_run = relationship("CrawlRun", back_populates="discovery_edges")


class DiscoveryObservation(Base):
    """Bounded aggregate telemetry for one discovery run/dimension."""

    __tablename__ = "discovery_observations"
    __table_args__ = (
        Index(
            "ix_discovery_observations_crawl_source_outcome",
            "crawl_id", "source_id", "outcome",
        ),
        Index(
            "ix_discovery_observations_source_reason_observed_at",
            "source_id", "reason", "observed_at",
        ),
    )

    observation_id = Column(Integer, primary_key=True, autoincrement=True)
    crawl_id = Column(String(50), ForeignKey("crawl_runs.crawl_id"), nullable=True)
    source_id = Column(String(50), ForeignKey("sources.source_id"), nullable=True)
    discovery_method = Column(String(30), nullable=False)
    root_url = Column(Text, nullable=True)
    outcome = Column(String(30), nullable=False)
    reason = Column(String(80), nullable=True)
    http_status = Column(Integer, nullable=True)
    observation_count = Column(Integer, default=0, nullable=False)
    metadata_json = Column(Text, nullable=True)
    # ``detail_json`` is an additive spelling retained for consumers using the
    # terminology from the adaptation handoff.
    detail_json = Column(Text, nullable=True)
    observed_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    crawl_run = relationship("CrawlRun", back_populates="discovery_observations")
    source = relationship("Source", back_populates="discovery_observations")
    samples = relationship(
        "DiscoveryObservationSample",
        back_populates="observation",
        cascade="all, delete-orphan",
    )


class DiscoveryObservationSample(Base):
    """Capped URL samples attached to a discovery aggregate."""

    __tablename__ = "discovery_observation_samples"
    __table_args__ = (
        Index("ix_discovery_observation_samples_observation_id", "observation_id"),
    )

    sample_id = Column(Integer, primary_key=True, autoincrement=True)
    observation_id = Column(
        Integer,
        ForeignKey("discovery_observations.observation_id"),
        nullable=False,
    )
    candidate_url = Column(Text, nullable=True)
    normalized_url = Column(Text, nullable=True)
    observed_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    observation = relationship("DiscoveryObservation", back_populates="samples")
