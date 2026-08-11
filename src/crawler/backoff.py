import asyncio
import random

class BackoffHandler:
    def __init__(self, initial_seconds: float = 5.0, multiplier: float = 2.0, max_seconds: float = 300.0, jitter: bool = True):
        self.initial_seconds = initial_seconds
        self.multiplier = multiplier
        self.max_seconds = max_seconds
        self.jitter = jitter

    def get_delay(self, attempt: int) -> float:
        if attempt <= 0:
            return 0.0
        
        delay = self.initial_seconds * (self.multiplier ** (attempt - 1))
        delay = min(delay, self.max_seconds)
        
        if self.jitter:
            # Apply standard full jitter
            delay = random.uniform(self.initial_seconds, delay)
            
        return delay

    async def sleep(self, attempt: int) -> None:
        delay = self.get_delay(attempt)
        if delay > 0:
            await asyncio.sleep(delay)
