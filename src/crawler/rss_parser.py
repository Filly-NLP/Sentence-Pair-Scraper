import feedparser
from typing import List, Dict, Any
from datetime import datetime
import time

class RSSParser:
    @staticmethod
    def parse_feed(feed_content: str) -> List[Dict[str, Any]]:
        """Parse RSS/Atom string content and return structured list of articles."""
        feed = feedparser.parse(feed_content)
        articles = []
        
        for entry in feed.entries:
            # Safely extract publish date
            pub_date = None
            for date_key in ("published_parsed", "updated_parsed", "created_parsed"):
                date_struct = entry.get(date_key)
                if date_struct:
                    try:
                        pub_date = datetime.fromtimestamp(time.mktime(date_struct))
                        break
                    except Exception:
                        pass
            
            articles.append({
                "title": entry.get("title", ""),
                "url": entry.get("link", ""),
                "published_date": pub_date,
                "category": entry.get("category", "")
            })
            
        return articles
