import time
import httpx
from urllib.robotparser import RobotFileParser
from urllib.parse import urlparse
from typing import Dict, Optional

class RobotsManager:
    def __init__(self, user_agent: str, cache_ttl_seconds: int = 86400):
        self.user_agent = user_agent
        self.cache_ttl = cache_ttl_seconds
        self.parsers: Dict[str, RobotFileParser] = {}
        self.cache_times: Dict[str, float] = {}

    def _get_robots_url(self, url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}/robots.txt"

    def refresh(self, domain_url: str) -> None:
        robots_url = self._get_robots_url(domain_url)
        parsed = urlparse(domain_url)
        domain = parsed.netloc

        parser = RobotFileParser()
        try:
            # Respect user agent and request robots.txt safely
            headers = {"User-Agent": self.user_agent}
            response = httpx.get(robots_url, headers=headers, timeout=10.0, follow_redirects=True)
            if response.status_code == 404:
                # If robots.txt doesn't exist, allow all
                parser.parse([])
            elif response.status_code == 403:
                # Disallow all if explicitly forbidden from viewing robots.txt
                parser.parse(["User-agent: *", "Disallow: /"])
            else:
                content = response.text
                parser.parse(content.splitlines())
        except Exception:
            # Fallback to permissive on network/resolution error to avoid total block,
            # but rate limiter will keep requests slow.
            parser.parse([])

        self.parsers[domain] = parser
        self.cache_times[domain] = time.time()

    def _get_parser(self, domain: str) -> RobotFileParser:
        now = time.time()
        if domain not in self.parsers or (now - self.cache_times.get(domain, 0)) > self.cache_ttl:
            # Trigger refresh
            self.refresh(f"https://{domain}")
        return self.parsers[domain]

    def is_allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        domain = parsed.netloc
        parser = self._get_parser(domain)
        return parser.can_fetch(self.user_agent, url)

    def get_crawl_delay(self, domain: str) -> Optional[float]:
        parser = self._get_parser(domain)
        try:
            delay = parser.crawl_delay(self.user_agent)
            if delay is not None:
                return float(delay)
        except Exception:
            pass
        return None
