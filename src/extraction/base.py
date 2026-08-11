import re
import json
from bs4 import BeautifulSoup
from typing import Dict, Any, Optional
from datetime import datetime
from src.sources.registry import SourceConfig

class ArticleExtractor:
    @staticmethod
    def extract(html: str, source: SourceConfig) -> Dict[str, Any]:
        """Extract article body text and metadata from raw HTML using standard extraction adapters."""
        soup = BeautifulSoup(html, "lxml")
        
        headline = ""
        author = ""
        pub_date_str = ""
        category = ""
        
        # 1. Parse JSON-LD metadata for highest quality metadata extraction
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                if isinstance(data, list):
                    data = data[0] if data else {}
                
                # Check graph type or direct article type
                graph = data.get("@graph")
                if graph and isinstance(graph, list):
                    for item in graph:
                        if "Article" in item.get("@type", "") or "NewsArticle" in item.get("@type", ""):
                            data = item
                            break
                            
                if "Article" in data.get("@type", "") or "NewsArticle" in data.get("@type", "") or "WebPage" in data.get("@type", ""):
                    headline = data.get("headline") or headline
                    pub_date_str = data.get("datePublished") or pub_date_str
                    author_data = data.get("author")
                    if isinstance(author_data, dict):
                        author = author_data.get("name") or author
                    elif isinstance(author_data, list) and author_data:
                        author = author_data[0].get("name") if isinstance(author_data[0], dict) else ""
                    break
            except Exception:
                pass

        # 2. Extract OpenGraph and standard headers if JSON-LD fallback needed
        if not headline:
            og_title = soup.find("meta", property="og:title")
            headline = og_title["content"] if og_title and og_title.get("content") else (soup.title.string if soup.title else "")

        if not pub_date_str:
            meta_pub = soup.find("meta", property="article:published_time")
            pub_date_str = meta_pub["content"] if meta_pub and meta_pub.get("content") else ""

        # 3. Clean and Extract Body Content (Generic or publisher-specific selectors)
        content_sel = ".entry-content, .post-content, article .content, #sports_article_writeup, .article__writeup, .story_main .article-body, .story_main .article_body"
        if source.extraction and source.extraction.content_selector:
            content_sel = source.extraction.content_selector

        body_node = None
        for sel in content_sel.split(","):
            node = soup.select_one(sel.strip())
            if node:
                body_node = node
                break

        if not body_node:
            # Fallback to generic article element
            body_node = soup.find("article") or soup.body

        # Strip headers, footers, navigation, sidebars, advertisements
        if body_node:
            for bad_tag in body_node.select("nav, footer, header, sidebar, .ads, .advertisement, script, style, .social-share, .comments"):
                bad_tag.decompose()
            
            # Extract paragraphs text content
            paras = [p.get_text().strip() for p in body_node.find_all("p") if p.get_text().strip()]
            body_text = "\n".join(paras)
        else:
            body_text = ""

        return {
            "headline": headline.strip() if headline else "",
            "author": author.strip() if author else "",
            "publication_date_raw": pub_date_str.strip() if pub_date_str else "",
            "article_text": body_text,
            "category": category
        }
