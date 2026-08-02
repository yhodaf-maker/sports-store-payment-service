from __future__ import annotations

import os

import httpx

from .openrouter import OpenRouterProvider
from .openrouter_config import OpenRouterConfig
from .provider import ReviewProvider


def create_provider(
    name: str | None = None, *, client: httpx.AsyncClient | None = None
) -> ReviewProvider:
    selected = (name or os.getenv("REVIEW_PROVIDER", "openrouter")).strip().lower()
    if selected == "openrouter":
        return OpenRouterProvider(OpenRouterConfig.load(), client=client)
    raise ValueError("invalid review provider configuration field: provider")
