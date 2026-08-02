from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class OpenRouterConfig:
    api_base_url: str = "https://openrouter.ai/api/v1"
    api_key: str = ""
    model: str = "nvidia/nemotron-3-super-120b-a12b:free"
    model_context_tokens: int = 128_000
    max_output_tokens: int = 4_000
    connect_timeout_seconds: float = 10.0
    request_timeout_seconds: float = 60.0
    max_retries: int = 2
    retry_initial_delay_seconds: float = 1.0
    retry_max_delay_seconds: float = 8.0
    max_requests_per_run: int = 25
    max_requests_per_chunk: int = 3
    max_input_tokens_per_run: int = 100_000
    max_output_tokens_per_run: int = 20_000
    max_execution_seconds: float = 300.0
    max_response_bytes: int = 256_000
    require_structured_outputs: bool = True
    require_zero_data_retention: bool = False
    deny_data_collection: bool = False
    allowed_providers: tuple[str, ...] = ()
    approved_fallback_models: tuple[str, ...] = ()
    app_url: str | None = None
    app_title: str | None = None

    @classmethod
    def load(cls) -> OpenRouterConfig:
        defaults = cls()
        values: dict[str, object] = {}
        for name, default in defaults.__dict__.items():
            env_name = f"OPENROUTER_{name.upper()}"
            if env_name not in os.environ:
                continue
            raw = os.environ[env_name]
            try:
                values[name] = _parse(raw, default)
            except ValueError as exc:
                raise ValueError(f"invalid OpenRouter configuration field: {name}") from exc
        config = cls(**values)
        config.validate()
        return config

    def validate(self) -> None:
        if not self.api_key.strip():
            raise ValueError("invalid OpenRouter configuration field: api_key")
        parsed = urlparse(self.api_base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("invalid OpenRouter configuration field: api_base_url")
        if not self.model or self.model == "openrouter/free" or not self.model.endswith(":free"):
            raise ValueError("invalid OpenRouter configuration field: model")
        if any(model == "openrouter/free" for model in self.approved_fallback_models):
            raise ValueError("invalid OpenRouter configuration field: approved_fallback_models")
        positive_ints = (
            "model_context_tokens", "max_output_tokens", "max_requests_per_run",
            "max_requests_per_chunk", "max_input_tokens_per_run", "max_output_tokens_per_run",
            "max_response_bytes",
        )
        for name in positive_ints:
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"invalid OpenRouter configuration field: {name}")
        if not isinstance(self.max_retries, int) or isinstance(self.max_retries, bool) or self.max_retries < 0:
            raise ValueError("invalid OpenRouter configuration field: max_retries")
        for name in (
            "connect_timeout_seconds", "request_timeout_seconds", "max_execution_seconds",
            "retry_initial_delay_seconds", "retry_max_delay_seconds",
        ):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                raise ValueError(f"invalid OpenRouter configuration field: {name}")
        if self.request_timeout_seconds <= 0 or self.connect_timeout_seconds <= 0 or self.max_execution_seconds <= 0:
            raise ValueError("invalid OpenRouter configuration field: timeout")
        if self.retry_max_delay_seconds < self.retry_initial_delay_seconds:
            raise ValueError("invalid OpenRouter configuration field: retry_max_delay_seconds")
        if self.max_requests_per_chunk > self.max_requests_per_run:
            raise ValueError("invalid OpenRouter configuration field: max_requests_per_chunk")
        if self.max_output_tokens >= self.model_context_tokens:
            raise ValueError("invalid OpenRouter configuration field: max_output_tokens")


def _parse(raw: str, default: object) -> object:
    if isinstance(default, bool):
        lowered = raw.strip().lower()
        if lowered not in {"true", "false"}:
            raise ValueError
        return lowered == "true"
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    if isinstance(default, tuple):
        return tuple(item.strip() for item in raw.split(",") if item.strip())
    return raw or None if default is None else raw
