import asyncio
import time
from typing import Dict
from urllib.parse import urlparse

class RateLimiter:
    def __init__(self, default_delay: float = 5.0, default_concurrency: int = 1):
        self.default_delay = default_delay
        self.default_concurrency = default_concurrency
        
        # Track last request timestamp per domain
        self.last_request_times: Dict[str, float] = {}
        # Semaphores to control concurrent requests per domain
        self.semaphores: Dict[str, asyncio.Semaphore] = {}
        # Overridden delays per domain (e.g. from sources config or robots.txt)
        self.domain_delays: Dict[str, float] = {}

    def set_domain_limit(self, domain: str, delay: float, concurrency: int) -> None:
        self.domain_delays[domain] = delay
        self.semaphores[domain] = asyncio.Semaphore(concurrency)

    def _get_semaphore(self, domain: str) -> asyncio.Semaphore:
        if domain not in self.semaphores:
            self.semaphores[domain] = asyncio.Semaphore(self.default_concurrency)
        return self.semaphores[domain]

    def _get_delay(self, domain: str) -> float:
        return self.domain_delays.get(domain, self.default_delay)

    async def wait_if_needed(self, url: str) -> None:
        parsed = urlparse(url)
        domain = parsed.netloc
        if not domain:
            return

        sem = self._get_semaphore(domain)
        async with sem:
            now = time.time()
            last_time = self.last_request_times.get(domain, 0.0)
            delay = self._get_delay(domain)
            elapsed = now - last_time
            
            if elapsed < delay:
                sleep_time = delay - elapsed
                await asyncio.sleep(sleep_time)
            
            self.last_request_times[domain] = time.time()
