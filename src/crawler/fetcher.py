import httpx
from typing import Dict, Any, Optional
from src.crawler.cache import HTTPCache
from src.crawler.backoff import BackoffHandler

class HTTPFetcher:
    def __init__(self, user_agent: str, cache: Optional[HTTPCache] = None, timeout: float = 30.0, max_retries: int = 3):
        self.user_agent = user_agent
        self.cache = cache
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_handler = BackoffHandler()
        self.headers = {"User-Agent": user_agent}

    async def fetch(self, url: str) -> Dict[str, Any]:
        """Fetch URL content respecting cache, conditional headers, and backoff retries."""
        # 1. Check local cache
        cached = self.cache.get(url) if self.cache else None
        
        headers = self.headers.copy()
        if cached:
            # Inject conditional tags
            meta = cached["meta"]
            if meta.get("etag"):
                headers["If-None-Match"] = meta["etag"]
            if meta.get("last_modified"):
                headers["If-Modified-Since"] = meta["last_modified"]

        # 2. Async execute request with retry backoff handling
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            for attempt in range(1, self.max_retries + 1):
                try:
                    response = await client.get(url, headers=headers)
                    
                    if response.status_code == 304 and cached:
                        # Unchanged: return cache
                        return {"status": 200, "html": cached["html"], "cached": True}
                        
                    if response.status_code == 200:
                        # Write cache
                        if self.cache:
                            self.cache.set(url, response.text, dict(response.headers))
                        return {"status": 200, "html": response.text, "cached": False}
                        
                    # Handle rate limit responses (429, 503, 502, 504)
                    if response.status_code in (429, 502, 503, 504):
                        if attempt == self.max_retries:
                            return {"status": response.status_code, "html": "", "cached": False}
                        await self.backoff_handler.sleep(attempt)
                        continue
                        
                    # Non-retryable errors (e.g. 403, 404)
                    return {"status": response.status_code, "html": "", "cached": False}
                except Exception as e:
                    if attempt == self.max_retries:
                        return {"status": 500, "html": "", "cached": False, "error": str(e)}
                    await self.backoff_handler.sleep(attempt)
                    
        return {"status": 500, "html": "", "cached": False}
