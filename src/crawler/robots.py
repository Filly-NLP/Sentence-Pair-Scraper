import asyncio
import inspect
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

_ORIGINAL_HTTPX_GET = httpx.get


class RobotsManager:
    def __init__(
        self,
        user_agent: str,
        cache_ttl_seconds: int = 86400,
        timeout: float = 10.0,
    ):
        self.user_agent = user_agent
        self.cache_ttl = cache_ttl_seconds
        self.timeout = timeout
        self.parsers: Dict[str, RobotFileParser] = {}
        self.cache_times: Dict[str, float] = {}
        self.sitemaps: Dict[str, List[str]] = {}
        self._lock = asyncio.Lock()

    def _get_robots_url(self, url: str) -> str:
        parsed = urlparse(url)
        scheme = parsed.scheme or "https"
        netloc = parsed.netloc or parsed.path
        return f"{scheme}://{netloc}/robots.txt"

    def _normalize_domain(self, domain: str) -> str:
        clean = (domain or "").lower().strip()
        if "://" in clean:
            parsed = urlparse(clean)
            clean = parsed.netloc or parsed.hostname or clean
        return clean.split(":", 1)[0]

    def _parse_robots_response(self, domain: str, response: Any) -> None:
        parser = RobotFileParser()
        sitemaps_found: List[str] = []
        try:
            status_code = getattr(response, "status_code", 200)
            if status_code == 404:
                parser.parse([])
            elif status_code == 403:
                parser.parse(["User-agent: *", "Disallow: /"])
            else:
                text = getattr(response, "text", "")
                lines = text.splitlines()
                parser.parse(lines)
                for line in lines:
                    clean = line.strip()
                    if clean.lower().startswith("sitemap:"):
                        sitemap_url = clean[8:].strip()
                        if sitemap_url and sitemap_url not in sitemaps_found:
                            sitemaps_found.append(sitemap_url)
        except Exception:
            parser.parse([])

        self.parsers[domain] = parser
        self.cache_times[domain] = time.time()
        self.sitemaps[domain] = sitemaps_found

    async def refresh_async(self, domain_url: str, client: Optional[httpx.AsyncClient] = None) -> None:
        robots_url = self._get_robots_url(domain_url)
        domain = self._normalize_domain(domain_url)
        headers = {"User-Agent": self.user_agent}

        try:
            if httpx.get is not _ORIGINAL_HTTPX_GET:
                res = httpx.get(robots_url, headers=headers, timeout=self.timeout)
                if inspect.isawaitable(res):
                    res = await res
                self._parse_robots_response(domain, res)
                return

            if client is not None:
                response = await client.get(robots_url, headers=headers, timeout=self.timeout)
            else:
                async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as local_client:
                    response = await local_client.get(robots_url, headers=headers)
            self._parse_robots_response(domain, response)
        except Exception:
            parser = RobotFileParser()
            parser.parse([])
            self.parsers[domain] = parser
            self.cache_times[domain] = time.time()
            self.sitemaps[domain] = []

    def refresh(self, domain_url: str) -> None:
        robots_url = self._get_robots_url(domain_url)
        domain = self._normalize_domain(domain_url)
        headers = {"User-Agent": self.user_agent}
        try:
            response = httpx.get(robots_url, headers=headers, timeout=self.timeout)
            self._parse_robots_response(domain, response)
        except Exception:
            parser = RobotFileParser()
            parser.parse([])
            self.parsers[domain] = parser
            self.cache_times[domain] = time.time()
            self.sitemaps[domain] = []

    async def _ensure_parser_async(self, domain: str, client: Optional[httpx.AsyncClient] = None) -> RobotFileParser:
        norm_domain = self._normalize_domain(domain)
        now = time.time()
        if norm_domain not in self.parsers or (now - self.cache_times.get(norm_domain, 0)) > self.cache_ttl:
            async with self._lock:
                if norm_domain not in self.parsers or (now - self.cache_times.get(norm_domain, 0)) > self.cache_ttl:
                    await self.refresh_async(f"https://{norm_domain}", client=client)
        return self.parsers[norm_domain]

    def _get_parser_sync(self, domain: str) -> RobotFileParser:
        norm_domain = self._normalize_domain(domain)
        now = time.time()
        if norm_domain not in self.parsers or (now - self.cache_times.get(norm_domain, 0)) > self.cache_ttl:
            self.refresh(f"https://{norm_domain}")
        return self.parsers[norm_domain]

    async def is_allowed_async(self, url: str, client: Optional[httpx.AsyncClient] = None) -> bool:
        domain = self._normalize_domain(url)
        parser = await self._ensure_parser_async(domain, client=client)
        return parser.can_fetch(self.user_agent, url)

    def is_allowed(self, url: str) -> bool:
        domain = self._normalize_domain(url)
        parser = self._get_parser_sync(domain)
        return parser.can_fetch(self.user_agent, url)

    async def get_crawl_delay_async(self, domain: str, client: Optional[httpx.AsyncClient] = None) -> Optional[float]:
        norm_domain = self._normalize_domain(domain)
        parser = self.parsers.get(norm_domain)
        if parser is None:
            try:
                parser = await self._ensure_parser_async(norm_domain, client=client)
            except Exception:
                return None
        try:
            delay = parser.crawl_delay(self.user_agent)
            if delay is not None:
                return float(delay)
        except Exception:
            pass
        return None

    def get_crawl_delay(self, domain: str) -> Optional[float]:
        norm_domain = self._normalize_domain(domain)
        parser = self.parsers.get(norm_domain)
        if parser is None:
            return None
        try:
            delay = parser.crawl_delay(self.user_agent)
            if delay is not None:
                return float(delay)
        except Exception:
            pass
        return None

    async def get_sitemaps_async(self, domain: str, client: Optional[httpx.AsyncClient] = None) -> List[str]:
        norm_domain = self._normalize_domain(domain)
        await self._ensure_parser_async(norm_domain, client=client)
        return list(self.sitemaps.get(norm_domain, []))

    def get_sitemaps(self, domain: str) -> List[str]:
        norm_domain = self._normalize_domain(domain)
        self._get_parser_sync(norm_domain)
        return list(self.sitemaps.get(norm_domain, []))
