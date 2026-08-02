from __future__ import annotations

import asyncio
from typing import ClassVar

from .models import Finding, ProviderResult, ReviewChunk


class MockReviewProvider:
    SCENARIOS: ClassVar[set[str]] = {
        "findings", "no_findings", "multiple", "duplicates", "empty",
        "error", "invalid", "delayed",
    }

    def __init__(self, scenario: str = "findings", delay_seconds: float = 0.01):
        if scenario not in self.SCENARIOS:
            raise ValueError(f"unknown mock scenario: {scenario}")
        self.scenario = scenario
        self.delay_seconds = delay_seconds

    async def review(self, chunk: ReviewChunk) -> ProviderResult:
        if self.scenario == "error":
            raise RuntimeError("mock provider error")
        if self.scenario == "invalid":
            return ProviderResult(valid=False, error_category="invalid_result")
        if self.scenario == "empty":
            return ProviderResult(valid=False, error_category="empty_response")
        if self.scenario == "no_findings":
            return ProviderResult()
        if self.scenario == "delayed":
            await asyncio.sleep(self.delay_seconds)
            return ProviderResult()

        path = chunk.files[0] if chunk.files else "unknown"
        primary = Finding(
            file=path,
            line=1,
            severity="medium",
            category="correctness",
            title="Mock finding",
            explanation="Deterministic mock explanation.",
        )
        if self.scenario == "duplicates":
            duplicate = Finding(
                file=path,
                line=1,
                severity="medium",
                category="Correctness",
                title=" mock finding ",
                explanation="Deterministic   mock explanation.",
            )
            return ProviderResult([primary, duplicate])
        if self.scenario == "multiple":
            return ProviderResult([
                primary,
                Finding(
                    file=path,
                    line=2,
                    severity="high",
                    category="security",
                    title=f"Chunk issue in {chunk.chunk_id}",
                    explanation="A second deterministic finding.",
                ),
            ])
        return ProviderResult([primary])
