from __future__ import annotations

from typing import Protocol

from .config import RunnerConfig


class TokenEstimator(Protocol):
    def estimate(self, text: str) -> int: ...


class ConservativeTokenEstimator:
    """A deliberately high UTF-8 byte estimate for unknown tokenizers."""

    def estimate(self, text: str) -> int:
        return max(1, len(text.encode("utf-8"))) if text else 0


def available_diff_budget(config: RunnerConfig) -> int:
    return config.available_diff_tokens
