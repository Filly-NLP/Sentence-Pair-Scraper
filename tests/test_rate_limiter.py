import asyncio
import time
import pytest

from src.crawler.rate_limiter import RateLimiter


@pytest.mark.asyncio
async def test_request_slot_holds_permit_during_full_request():
    limiter = RateLimiter(default_delay=0.0, default_concurrency=1)
    domain = "example.com"
    concurrency_counter = 0
    max_observed_concurrency = 0

    async def worker():
        nonlocal concurrency_counter, max_observed_concurrency
        async with limiter.request_slot(domain):
            concurrency_counter += 1
            max_observed_concurrency = max(max_observed_concurrency, concurrency_counter)
            await asyncio.sleep(0.05)
            concurrency_counter -= 1

    await asyncio.gather(worker(), worker(), worker())
    assert max_observed_concurrency == 1


@pytest.mark.asyncio
async def test_monotonic_delay_spacing_between_request_starts():
    delay = 0.05
    limiter = RateLimiter(default_delay=delay, default_concurrency=1)
    domain = "example.com"
    start_times = []

    async def worker():
        async with limiter.request_slot(domain, effective_delay=delay):
            start_times.append(time.monotonic())
            await asyncio.sleep(0.01)

    await asyncio.gather(worker(), worker(), worker())

    assert len(start_times) == 3
    # Verify spacing between consecutive request starts
    diff_1 = start_times[1] - start_times[0]
    diff_2 = start_times[2] - start_times[1]

    assert diff_1 >= delay * 0.85
    assert diff_2 >= delay * 0.85


@pytest.mark.asyncio
async def test_cross_domain_overlap_and_global_concurrency_bound():
    limiter = RateLimiter(default_delay=0.0, default_concurrency=1, max_global_concurrency=2)
    active_domains = set()
    max_global_observed = 0
    concurrency_counter = 0

    async def worker(dom: str):
        nonlocal max_global_observed, concurrency_counter
        async with limiter.request_slot(dom):
            concurrency_counter += 1
            active_domains.add(dom)
            max_global_observed = max(max_global_observed, concurrency_counter)
            await asyncio.sleep(0.05)
            concurrency_counter -= 1

    await asyncio.gather(
        worker("domain-a.com"),
        worker("domain-b.com"),
        worker("domain-c.com"),
    )

    assert max_global_observed == 2
    assert len(active_domains) == 3


@pytest.mark.asyncio
async def test_stable_domain_state_on_limit_updates():
    limiter = RateLimiter(default_delay=1.0, default_concurrency=1)
    domain = "example.com"
    state_1 = limiter._get_or_create_domain_state(domain)
    initial_sem = state_1.semaphore

    limiter.set_domain_limit(domain, delay=2.0)
    state_2 = limiter._get_or_create_domain_state(domain)

    assert state_2.semaphore is initial_sem
    assert state_2.delay == 2.0
