import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from src.storage.models import Base
from src.sources.registry import SourceConfig, RSSFeedConfig, SitemapConfig

@pytest.fixture
def db_session():
    """Create a fresh in-memory SQLite database for each test session."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()

@pytest.fixture
def sample_source():
    """Return a sample Bandera source configuration."""
    return SourceConfig(
        id="bandera",
        name="Bandera",
        domain="bandera.inquirer.net",
        enabled=True,
        language="filipino",
        crawl_delay_seconds=10,
        max_concurrent=1,
        rss=[RSSFeedConfig(url="https://bandera.inquirer.net/feed", category="general")],
        sitemap=[SitemapConfig(url="https://bandera.inquirer.net/sitemap.xml")],
        extraction=None
    )
