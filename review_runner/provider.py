from __future__ import annotations

from typing import Protocol

from .models import ProviderResult, ReviewChunk, ReviewContext


class ReviewProvider(Protocol):
    async def prepare(self, context: ReviewContext) -> ProviderResult | None: ...

    async def review(self, chunk: ReviewChunk) -> ProviderResult: ...
