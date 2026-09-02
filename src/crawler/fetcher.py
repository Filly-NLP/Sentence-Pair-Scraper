import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Optional

import httpx

from src.crawler.backoff import BackoffHandler
from src.crawler.cache import HTTPCache


@dataclass
class FetchResult:
    status: int
    headers: Dict[str, str] = field(default_factory=dict)
    body: str = ""
    html: str = ""
    cached: bool = False
    attempts: int = 1
    retry_after: Optional[float] = None
    error: Optional[str] = None
    error_category: Optional[str] = None
    final_url: Optional[str] = None

    def __post_init__(self):
        if not self.html and self.body:
            self.html = self.body
        elif not self.body and self.html:
            self.body = self.html

    def get(self, key: str, default: Any = None) -> Any:
        if key == "html":
            return self.html or self.body
        if hasattr(self, key):
            val = getattr(self, key)
            return val if val is not None else default
        return default

    def __getitem__(self, key: str) -> Any:
        if key == "html":
            return self.html or self.body
        if hasattr(self, key):
            return getattr(self, key)
        raise KeyError(key)

    def __contains__(self, key: str) -> bool:
        return hasattr(self, key) or key == "html"


class HTTPFetcher:
    def __init__(
        self,
        user_agent: str,
        cache: Optional[HTTPCache] = None,
        timeout: float = 30.0,
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        write_timeout: float = 10.0,
        pool_timeout: float = 10.0,
        max_connections: int = 50,
        max_keepalive_connections: int = 20,
        max_retries: int = 3,
        backoff_handler: Optional[BackoffHandler] = None,
    ):
        self.user_agent = user_agent
        self.cache = cache
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.write_timeout = write_timeout
        self.pool_timeout = pool_timeout
        self.max_connections = max_connections
        self.max_keepalive_connections = max_keepalive_connections
        self.max_retries = max(1, max_retries)
        self.backoff_handler = backoff_handler or BackoffHandler()
        self.headers = {"User-Agent": user_agent}
        self.client: Optional[httpx.AsyncClient] = None

    def _create_client(self) -> httpx.AsyncClient:
        limits = httpx.Limits(
            max_connections=self.max_connections,
            max_keepalive_connections=self.max_keepalive_connections,
        )
        timeout = httpx.Timeout(
            self.timeout,
            connect=self.connect_timeout,
            read=self.read_timeout,
            write=self.write_timeout,
            pool=self.pool_timeout,
        )
        return httpx.AsyncClient(limits=limits, timeout=timeout, follow_redirects=True)

    async def start(self) -> None:
        if self.client is None:
            self.client = self._create_client()

    async def close(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    @staticmethod
    def _retry_after(response: httpx.Response) -> Optional[float]:
        value = response.headers.get("Retry-After")
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                return max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None

    @staticmethod
    def categorize_status(status: int) -> str:
        if status == 200 or status == 304:
            return "SUCCESS"
        if status == 429:
            return "RATE_LIMITED"
        if status == 403:
            return "FORBIDDEN"
        if status in (202, 408, 425, 500, 502, 503, 504):
            return "HTTP_TRANSIENT"
        return "HTTP_ERROR"

    async def _fetch_with_client(self, client: httpx.AsyncClient, url: str) -> FetchResult:
        cached = self.cache.get(url) if self.cache else None
        headers = self.headers.copy()
        if cached:
            meta = cached.get("meta", {})
            if meta.get("etag"):
                headers["If-None-Match"] = meta["etag"]
            if meta.get("last_modified"):
                headers["If-Modified-Since"] = meta["last_modified"]

        last_error = None
        last_category = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = await client.get(url, headers=headers)
                resp_headers = dict(response.headers)
                retry_after = self._retry_after(response)
                category = self.categorize_status(response.status_code)

                if response.status_code == 304 and cached:
                    return FetchResult(
                        status=200,
                        headers=resp_headers,
                        body=cached["html"],
                        html=cached["html"],
                        final_url=str(response.url),
                        cached=True,
                        attempts=attempt,
                        error_category="SUCCESS",
                    )
                if response.status_code == 200:
                    if self.cache:
                        self.cache.set(url, response.text, resp_headers)
                    return FetchResult(
                        status=200,
                        headers=resp_headers,
                        body=response.text,
                        html=response.text,
                        final_url=str(response.url),
                        cached=False,
                        attempts=attempt,
                        error_category="SUCCESS",
                    )
                # Transient HTTP statuses that warrant per-request backoff
                if response.status_code in (408, 425, 429, 500, 502, 503, 504):
                    if attempt < self.max_retries:
                        if retry_after is not None:
                            await asyncio.sleep(retry_after)
                        else:
                            await self.backoff_handler.sleep(attempt)
                        continue

                return FetchResult(
                    status=response.status_code,
                    headers=resp_headers,
                    body="",
                    html="",
                    final_url=str(response.url),
                    cached=False,
                    attempts=attempt,
                    retry_after=retry_after,
                    error=f"HTTP_{response.status_code}",
                    error_category=category,
                )
            except httpx.TimeoutException as exc:
                last_error = f"timeout: {exc}"
                last_category = "TIMEOUT"
                if attempt < self.max_retries:
                    await self.backoff_handler.sleep(attempt)
            except httpx.NetworkError as exc:
                last_error = f"network_error: {exc}"
                last_category = "NETWORK_ERROR"
                if attempt < self.max_retries:
                    await self.backoff_handler.sleep(attempt)
            except Exception as exc:
                last_error = str(exc)
                last_category = "NETWORK_ERROR"
                if attempt < self.max_retries:
                    await self.backoff_handler.sleep(attempt)

        return FetchResult(
            status=500,
            headers={},
            body="",
            html="",
            final_url=url,
            cached=False,
            attempts=self.max_retries,
            error=last_error or "fetch_exhausted",
            error_category=last_category or "NETWORK_ERROR",
        )

    async def fetch(self, url: str) -> FetchResult:
        """Fetch one URL, reusing the pipeline client when one is started."""
        if self.client is not None:
            return await self._fetch_with_client(self.client, url)
        async with self._create_client() as client:
            return await self._fetch_with_client(client, url)
