"""HTML metadata and article-body extraction with source-aware selectors."""

import json
from datetime import datetime
from typing import Any, Dict, Iterable

from bs4 import BeautifulSoup

from src.sources.registry import SourceConfig


class ArticleExtractor:
    _DEFAULT_BODY_SELECTORS = (
        ".entry-content", ".post-content", "article .content",
        "#sports_article_writeup", ".article__writeup",
        ".story_main .article-body", ".story_main .article_body",
    )
    _ARTICLE_TYPES = {"article", "newsarticle", "reportageNewsArticle".lower()}
    _EXCLUDED_SELECTORS = (
        "nav", "footer", "header", "aside", "sidebar", ".sidebar", ".ads",
        ".ad", ".advertisement", ".social-share", ".comments", ".related",
        ".newsletter", ".subscribe", ".caption", ".byline", ".author", ".dateline",
        ".timestamp", ".publication-date", ".tags", ".share", ".photo-credit",
        ".embed", ".video", "script", "style", "form",
    )

    @classmethod
    def _jsonld_nodes(cls, value: Any) -> Iterable[dict[str, Any]]:
        if isinstance(value, list):
            for item in value:
                yield from cls._jsonld_nodes(item)
        elif isinstance(value, dict):
            yield value
            graph = value.get("@graph")
            if graph:
                yield from cls._jsonld_nodes(graph)

    @classmethod
    def _article_nodes(cls, soup: BeautifulSoup) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                payload = json.loads(script.string or script.get_text() or "")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            for node in cls._jsonld_nodes(payload):
                types = node.get("@type", [])
                if isinstance(types, str):
                    types = [types]
                elif not isinstance(types, (list, tuple, set)):
                    types = [types] if types else []
                if any(str(t).lower() in cls._ARTICLE_TYPES or "article" in str(t).lower() for t in types):
                    nodes.append(node)
        return nodes

    @staticmethod
    def _author_name(value: Any) -> str:
        if isinstance(value, dict):
            return str(value.get("name") or "")
        if isinstance(value, list):
            names = [ArticleExtractor._author_name(item) for item in value]
            return ", ".join(name for name in names if name)
        return str(value or "")

    @staticmethod
    def _meta_content(soup: BeautifulSoup, **attrs: str) -> str:
        node = soup.find("meta", attrs=attrs)
        return (node.get("content") or "").strip() if node else ""

    @classmethod
    def _date_candidates(cls, soup: BeautifulSoup, source: SourceConfig) -> list[tuple[str, str]]:
        candidates: list[tuple[str, str]] = []
        # JSON-LD article nodes have the highest confidence.  Do not stop at a
        # WebPage node or the first malformed object.
        for node in cls._article_nodes(soup):
            value = node.get("datePublished") or node.get("dateCreated")
            if value:
                candidates.append((str(value).strip(), "jsonld"))

        for attrs in (
            {"property": "article:published_time"},
            {"name": "datePublished"},
            {"itemprop": "datePublished"},
            {"property": "og:article:published_time"},
        ):
            value = cls._meta_content(soup, **attrs)
            if value:
                candidates.append((value, "meta"))

        selectors: list[str] = []
        if source.extraction and source.extraction.date_selector:
            selectors.extend(x.strip() for x in source.extraction.date_selector.split(","))
        selectors.extend(("time[datetime]", "[itemprop='datePublished']", "[property='article:published_time']"))
        for selector in selectors:
            try:
                node = soup.select_one(selector)
            except Exception:
                node = None
            if node:
                value = (node.get("datetime") or node.get("content") or node.get_text(" ", strip=True)).strip()
                if value:
                    candidates.append((value, "selector"))
        return candidates

    @classmethod
    def _select_date(cls, soup: BeautifulSoup, source: SourceConfig) -> tuple[str, str]:
        candidates = cls._date_candidates(soup, source)
        if not candidates:
            return "", "missing"
        from src.extraction.date_filter import DateFilter
        probe = DateFilter(datetime(1970, 1, 1))
        for value, source_name in candidates:
            if probe.parse_date(value) is not None:
                return value, source_name
        return candidates[0]

    @classmethod
    def _select_modified_date(cls, soup: BeautifulSoup) -> tuple[str, str]:
        for node in cls._article_nodes(soup):
            value = node.get("dateModified") or node.get("dateUpdated")
            if value:
                return str(value).strip(), "jsonld_modified"

        for attrs in (
            {"property": "article:modified_time"},
            {"name": "dateModified"},
            {"itemprop": "dateModified"},
            {"property": "og:updated_time"},
        ):
            value = cls._meta_content(soup, **attrs)
            if value:
                return value, "meta_modified"

        return "", "missing"

    @classmethod
    def _body_node(cls, soup: BeautifulSoup, source: SourceConfig):
        selectors: list[str] = []
        if source.extraction and source.extraction.content_selector:
            selectors.extend(x.strip() for x in source.extraction.content_selector.split(","))
        selectors.extend(cls._DEFAULT_BODY_SELECTORS)
        best_node = None
        best_score = 0
        best_selector = None
        best_method = source.extraction.type if source.extraction else "generic"
        for selector in selectors:
            try:
                node = soup.select_one(selector)
            except Exception:
                node = None
            if node:
                paragraphs = [p.get_text(" ", strip=True) for p in node.find_all("p")]
                score = sum(len(p) for p in paragraphs if p)
                if score == 0:
                    score = len(node.get_text(" ", strip=True))
                if score > best_score:
                    best_node = node
                    best_score = score
                    best_selector = selector
        if best_node is not None:
            return best_node, best_method, best_selector
        fallback_node = soup.find("article") or soup.body
        return fallback_node, "generic-fallback", "article|body"

    @classmethod
    def extract(cls, html: str, source: SourceConfig) -> Dict[str, Any]:
        """Extract metadata and cleaned body text.

        The adapter type is retained in the result for diagnostics.  Source
        configuration controls selectors while generic safety exclusions apply
        to all publishers.
        """
        soup = BeautifulSoup(html or "", "lxml")
        nodes = cls._article_nodes(soup)
        headline = ""
        author = ""
        for node in nodes:
            headline = headline or str(node.get("headline") or "")
            author = author or cls._author_name(node.get("author"))
        if not headline:
            headline = cls._meta_content(soup, property="og:title") or (soup.title.get_text(strip=True) if soup.title else "")
        if not author:
            author = cls._meta_content(soup, name="author") or cls._meta_content(soup, itemprop="author")
        pub_date_raw, date_source = cls._select_date(soup, source)
        mod_date_raw, mod_date_source = cls._select_modified_date(soup)

        body_node, extraction_method, matched_selector = cls._body_node(soup, source)
        body_text = ""
        paragraph_count = 0
        if body_node:
            for bad_selector in cls._EXCLUDED_SELECTORS:
                for bad_tag in body_node.select(bad_selector):
                    bad_tag.decompose()
            paragraphs = [p.get_text(" ", strip=True) for p in body_node.find_all("p")]
            paragraphs = [p for p in paragraphs if p]
            if not paragraphs:
                blocks = body_node.find_all(["div", "section", "li"], recursive=False)
                paragraphs = [b.get_text(" ", strip=True) for b in blocks if b.get_text(" ", strip=True)]
            if not paragraphs:
                text = body_node.get_text(" ", strip=True)
                paragraphs = [text] if text else []
            paragraph_count = len(paragraphs)
            body_text = "\n".join(paragraphs)

        canonical_node = soup.find("link", rel=lambda value: value and "canonical" in value)
        canonical_url = canonical_node.get("href", "").strip() if canonical_node else ""
        return {
            "headline": headline.strip(),
            "author": author.strip(),
            "publication_date_raw": pub_date_raw,
            "publication_date_source": date_source,
            "modified_date_raw": mod_date_raw,
            "modified_date_source": mod_date_source,
            "canonical_url": canonical_url,
            "article_text": body_text,
            "body_chars": len(body_text),
            "paragraph_count": paragraph_count,
            "body_valid": bool(body_text.strip()),
            "selector_match": matched_selector,
            "extraction_method": extraction_method,
            "category": "",
        }
