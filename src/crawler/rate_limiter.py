import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import time
from typing import Dict, Optional
from urllib.parse import urlparse


@dataclass
class DomainState:
    domain: str
    concurrency: int = 1
    delay: float = 5.0
    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))
    last_start_time: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class RateLimiter:
    def __init__(
        self,
        default_delay: float = 5.0,
        default_concurrency: int = 1,
        max_global_concurrency: int = 50,
    ):
        self.default_delay = default_delay
        self.default_concurrency = default_concurrency
        self.max_global_concurrency = max_global_concurrency
        self.global_semaphore = asyncio.Semaphore(max_global_concurrency)
        self._domains: Dict[str, DomainState] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _normalize_domain(domain: str) -> str:
        clean = (domain or "").lower().strip()
        if "://" in clean:
            parsed = urlparse(clean)
            clean = parsed.hostname or clean
        return clean.split(":", 1)[0].removeprefix("www.")

    def _get_or_create_domain_state(self, domain: str) -> DomainState:
        norm = self._normalize_domain(domain)
        if norm not in self._domains:
            self._domains[norm] = DomainState(
                domain=norm,
                concurrency=self.default_concurrency,
                delay=self.default_delay,
                semaphore=asyncio.Semaphore(self.default_concurrency),
            )
        return self._domains[norm]

    def set_domain_limit(
        self,
        domain: str,
        delay: Optional[float] = None,
        concurrency: Optional[int] = None,
    ) -> None:
        """Update domain delay and concurrency without replacing live semaphores."""
        norm = self._normalize_domain(domain)
        if norm not in self._domains:
            conc = concurrency if concurrency is not None else self.default_concurrency
            self._domains[norm] = DomainState(
                domain=norm,
                concurrency=conc,
                delay=delay if delay is not None else self.default_delay,
                semaphore=asyncio.Semaphore(conc),
            )
        else:
            state = self._domains[norm]
            if delay is not None:
                state.delay = delay
            if concurrency is not None and concurrency != state.concurrency:
                state.concurrency = concurrency

    def get_domain_delay(self, domain: str) -> float:
        norm = self._normalize_domain(domain)
        if norm in self._domains:
            return self._domains[norm].delay
        return self.default_delay

    def get_domain_concurrency(self, domain: str) -> int:
        norm = self._normalize_domain(domain)
        if norm in self._domains:
            return self._domains[norm].concurrency
        return self.default_concurrency

    @asynccontextmanager
    async def request_slot(self, domain: str, effective_delay: Optional[float] = None):
        """Acquire a request slot holding both domain and global permits for full request duration."""
        state = self._get_or_create_domain_state(domain)
        delay = effective_delay if effective_delay is not None else state.delay

        async with self.global_semaphore:
            async with state.semaphore:
                async with state._lock:
                    now = time.monotonic()
                    elapsed = now - state.last_start_time
                    if elapsed < delay:
                        await asyncio.sleep(delay - elapsed)
                    state.last_start_time = time.monotonic()
                yield

    async def wait_if_needed(self, url: str) -> None:
        """Legacy compatibility method; acquires and releases slot immediately."""
        parsed = urlparse(url)
        domain = parsed.hostname or parsed.netloc or url
        async with self.request_slot(domain):
            pass
