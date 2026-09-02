import tempfile
from pathlib import Path
import pytest
from src.crawler.config import CrawlerConfig
from src.crawler.pipeline import CrawlPipeline
from src.sources.registry import SourceConfig, SourceRegistry
from src.sentence.quality_filter import SentenceQualityFilter
from src.language.detector import FilipinoLanguageDetector


def test_default_profile_preserves_historical_defaults():
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = Path(tmpdir) / "crawler.yaml"
        cfg_path.write_text(
            """
crawler:
  version: "1.0.0"
sentence:
  min_tokens: 3
  max_tokens: 100
  include_quotes: false
  include_headlines: false
language:
  min_confidence: 0.0
""",
            encoding="utf-8",
        )
        cfg = CrawlerConfig(cfg_path)
        pipeline = CrawlPipeline(None, cfg, "TestBot/1.0")
        policy = pipeline.resolve_effective_policy()
        assert policy["profile_name"] == "default"
        assert policy["min_tokens"] == 3
        assert policy["max_tokens"] == 100
        assert policy["include_quotes"] is False
        assert policy["include_headlines"] is False
        assert policy["accepted_languages"] == ["FILIPINO"]
        assert policy["min_language_confidence"] == 0.0


def test_named_policy_profiles():
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = Path(tmpdir) / "crawler.yaml"
        cfg_path.write_text(
            """
crawler:
  version: "1.0.0"
policy:
  profile: "strict"
""",
            encoding="utf-8",
        )
        cfg = CrawlerConfig(cfg_path)
        pipeline = CrawlPipeline(None, cfg, "TestBot/1.0")
        
        # Strict
        strict_policy = pipeline.resolve_effective_policy()
        assert strict_policy["profile_name"] == "strict"
        assert strict_policy["min_tokens"] == 6
        assert strict_policy["max_tokens"] == 60
        assert strict_policy["include_quotes"] is False
        assert strict_policy["min_quality_score"] == 0.9

        # Balanced via source override
        src_balanced = SourceConfig(id="s1", name="S1", domain="s1.com", policy_profile="balanced")
        balanced_policy = pipeline.resolve_effective_policy(src_balanced)
        assert balanced_policy["profile_name"] == "balanced"
        assert balanced_policy["min_tokens"] == 4
        assert balanced_policy["include_quotes"] is True

        # Recall via source override
        src_recall = SourceConfig(id="s2", name="S2", domain="s2.com", policy_profile="recall")
        recall_policy = pipeline.resolve_effective_policy(src_recall)
        assert recall_policy["profile_name"] == "recall"
        assert recall_policy["include_headlines"] is True
        assert recall_policy["allow_mixed_language"] is True


def test_per_source_granular_overrides():
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = Path(tmpdir) / "crawler.yaml"
        cfg_path.write_text(
            """
crawler:
  version: "1.0.0"
policy:
  profile: "balanced"
""",
            encoding="utf-8",
        )
        cfg = CrawlerConfig(cfg_path)
        pipeline = CrawlPipeline(None, cfg, "TestBot/1.0")

        src = SourceConfig(
            id="custom_src",
            name="Custom",
            domain="custom.com",
            policy_profile="balanced",
            min_tokens=10,
            include_quotes=False,
            min_quality_score=0.95,
        )
        resolved = pipeline.resolve_effective_policy(src)
        assert resolved["min_tokens"] == 10  # Overridden from 4
        assert resolved["max_tokens"] == 80  # Inherited from balanced
        assert resolved["include_quotes"] is False  # Overridden from True
        assert resolved["min_quality_score"] == 0.95  # Overridden from 0.7