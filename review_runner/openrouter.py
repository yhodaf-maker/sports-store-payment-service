from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Self
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from .logging_utils import get_logger
from .models import (
    Finding,
    ProviderErrorCategory,
    ProviderResult,
    ReviewChunk,
    ReviewContext,
)
from .openrouter_config import OpenRouterConfig
from .prompt import build_messages
from .review_schema import REVIEW_JSON_SCHEMA, StructuredReview

Sleep = Callable[[float], Awaitable[None]]


class OpenRouterProvider:
    def __init__(
        self,
        config: OpenRouterConfig,
        client: httpx.AsyncClient | None = None,
        logger: logging.Logger | None = None,
        sleep: Sleep = asyncio.sleep,
    ):
        config.validate()
        self.config = config
        self.logger = logger or get_logger("INFO")
        self._sleep = sleep
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=config.api_base_url.rstrip("/") + "/",
            headers=self._headers(),
            timeout=httpx.Timeout(
                config.request_timeout_seconds,
                connect=config.connect_timeout_seconds,
            ),
        )
        self._context: ReviewContext | None = None
        self._started = 0.0
        self._requests = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._retries = 0
        self._prepared = False
        self._retry_after_seconds = 0.0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def metrics(self) -> dict[str, int | str]:
        return {
            "model": self.config.model,
            "request_count": self._requests,
            "input_tokens": self._input_tokens,
            "output_tokens": self._output_tokens,
            "retry_count": self._retries,
            "duration_ms": int((time.monotonic() - self._started) * 1000) if self._started else 0,
        }

    async def prepare(self, context: ReviewContext) -> ProviderResult | None:
        self._context = context
        self._started = time.monotonic()
        if context.model_context_tokens > self.config.model_context_tokens:
            return self._failure(
                ProviderErrorCategory.UNSUPPORTED_CAPABILITY,
                "Configured model context window is smaller than the runner context window.",
                skipped=True,
            )
        if context.reserved_output_tokens > self.config.max_output_tokens:
            return self._failure(
                ProviderErrorCategory.UNSUPPORTED_CAPABILITY,
                "Runner output reservation exceeds the provider output limit.",
                skipped=True,
            )
        quota = self._quota_failure(0)
        if quota:
            return quota
        try:
            self._requests += 1
            model_path = quote(self.config.model, safe="/")
            response = await self._client.get(f"models/{model_path}/endpoints")
        except httpx.TimeoutException:
            return self._failure(
                ProviderErrorCategory.NETWORK_TIMEOUT, "Model capability check timed out.", skipped=True
            )
        except httpx.TransportError:
            return self._failure(
                ProviderErrorCategory.PROVIDER_UNAVAILABLE,
                "Model capability check could not reach OpenRouter.",
                skipped=True,
            )
        if response.status_code in {401, 403}:
            return self._failure(
                ProviderErrorCategory.AUTHENTICATION_ERROR,
                "OpenRouter authentication was rejected.",
                skipped=True,
            )
        if response.status_code == 404:
            return self._failure(
                ProviderErrorCategory.MODEL_UNAVAILABLE,
                "The configured OpenRouter model is unavailable.",
                skipped=True,
            )
        if not response.is_success:
            return self._failure(
                ProviderErrorCategory.PROVIDER_UNAVAILABLE,
                "OpenRouter model capability validation is unavailable.",
                skipped=True,
            )
        try:
            payload = response.json()
            model = payload["data"]
            endpoints = model["endpoints"]
        except (ValueError, KeyError, TypeError):
            return self._failure(
                ProviderErrorCategory.MODEL_UNAVAILABLE,
                "OpenRouter returned invalid model capability metadata.",
                skipped=True,
            )
        capable = [endpoint for endpoint in endpoints if self._eligible_endpoint(endpoint, context)]
        if not capable:
            return self._failure(
                ProviderErrorCategory.UNSUPPORTED_CAPABILITY,
                "No eligible route supports the required context and structured output.",
                skipped=True,
            )
        eligible = [endpoint for endpoint in capable if self._privacy_eligible(endpoint)]
        if not eligible:
            return self._failure(
                ProviderErrorCategory.PRIVACY_REQUIREMENT_UNAVAILABLE,
                "No provider route satisfies the required privacy policy.",
                skipped=True,
            )
        self._prepared = True
        self.logger.info(
            "provider ready model=%s status=available eligible_routes=%d",
            self.config.model,
            len(eligible),
        )
        return None

    async def review(self, chunk: ReviewChunk) -> ProviderResult:
        started = time.monotonic()
        if not self._prepared or self._context is None:
            return self._failure(
                ProviderErrorCategory.CONFIGURATION_ERROR,
                "Provider was not prepared for this review.",
                skipped=True,
            )
        quota = self._quota_failure(chunk.estimated_tokens)
        if quota:
            return quota
        if chunk.estimated_tokens + self.config.max_output_tokens > self.config.model_context_tokens:
            return self._failure(
                ProviderErrorCategory.PAYLOAD_TOO_LARGE,
                "Chunk exceeds the validated model token limit.",
                skipped=True,
            )

        retry_count = 0
        last: ProviderResult | None = None
        while retry_count <= self.config.max_retries:
            quota = self._quota_failure(chunk.estimated_tokens if retry_count == 0 else 0, retry_count)
            if quota:
                quota.retry_attempted = retry_count > 0
                quota.retry_count = retry_count
                return quota
            self._requests += 1
            if retry_count == 0:
                self._input_tokens += chunk.estimated_tokens
            try:
                response = await self._client.post("chat/completions", json=self._request_body(chunk))
                self._retry_after_seconds = _retry_after(response)
                result, retryable = self._handle_response(response, chunk)
            except httpx.TimeoutException:
                result = self._failure(
                    ProviderErrorCategory.NETWORK_TIMEOUT, "OpenRouter request timed out."
                )
                retryable = True
            except httpx.TransportError:
                result = self._failure(
                    ProviderErrorCategory.PROVIDER_UNAVAILABLE,
                    "OpenRouter request was interrupted.",
                )
                retryable = True
            except Exception:  # noqa: BLE001
                result = self._failure(
                    ProviderErrorCategory.UNEXPECTED_PROVIDER_ERROR,
                    "OpenRouter request failed unexpectedly.",
                )
                retryable = False
            result.retry_count = retry_count
            result.retry_attempted = retry_count > 0
            result.duration_ms = int((time.monotonic() - started) * 1000)
            last = result
            if result.valid or not retryable or retry_count >= self.config.max_retries:
                self._log_result(chunk, result)
                return result
            delay = min(
                max(
                    self.config.retry_initial_delay_seconds * (2**retry_count),
                    self._retry_after_seconds,
                ),
                self.config.retry_max_delay_seconds,
            )
            retry_count += 1
            if delay:
                await self._sleep(delay)
        return last or self._failure(
            ProviderErrorCategory.UNEXPECTED_PROVIDER_ERROR, "OpenRouter request failed."
        )

    def _eligible_endpoint(self, endpoint: object, context: ReviewContext) -> bool:
        if not isinstance(endpoint, dict) or endpoint.get("status", 0) not in {0, None}:
            return False
        parameters = set(endpoint.get("supported_parameters") or [])
        required = {"response_format", "structured_outputs"}
        if self.config.require_structured_outputs and not required.issubset(parameters):
            return False
        context_length = endpoint.get("context_length")
        if not isinstance(context_length, int) or context_length < context.model_context_tokens:
            return False
        maximum = endpoint.get("max_completion_tokens")
        if isinstance(maximum, int) and maximum < self.config.max_output_tokens:
            return False
        pricing = endpoint.get("pricing")
        if not isinstance(pricing, dict):
            return False
        try:
            if float(pricing.get("prompt", 1)) != 0 or float(pricing.get("completion", 1)) != 0:
                return False
        except (TypeError, ValueError):
            return False
        if self.config.allowed_providers:
            provider = str(endpoint.get("provider_name", "")).casefold()
            tag = str(endpoint.get("tag", "")).casefold()
            if not any(item.casefold() in {provider, tag} for item in self.config.allowed_providers):
                return False
        return True

    def _privacy_eligible(self, endpoint: object) -> bool:
        if not isinstance(endpoint, dict):
            return False
        if self.config.require_zero_data_retention and endpoint.get("supports_zdr") is False:
            return False
        policy = endpoint.get("data_policy")
        return not (
            self.config.deny_data_collection
            and isinstance(policy, dict)
            and (policy.get("training") is True or policy.get("data_collection") == "allow")
        )

    def _request_body(self, chunk: ReviewChunk) -> dict[str, object]:
        assert self._context is not None
        models = [self.config.model, *self.config.approved_fallback_models]
        body: dict[str, object] = {
            "model": self.config.model,
            "messages": build_messages(chunk, self._context),
            "max_tokens": self.config.max_output_tokens,
            "temperature": 0,
            "stream": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "pull_request_review", "strict": True, "schema": REVIEW_JSON_SCHEMA},
            },
            "provider": {
                "require_parameters": True,
                "data_collection": "deny" if self.config.deny_data_collection else "allow",
                "zdr": self.config.require_zero_data_retention,
                "allow_fallbacks": True,
            },
        }
        if len(models) > 1:
            body.pop("model")
            body["models"] = models
        if self.config.allowed_providers:
            body["provider"]["only"] = list(self.config.allowed_providers)  # type: ignore[index]
        return body

    def _handle_response(
        self, response: httpx.Response, chunk: ReviewChunk
    ) -> tuple[ProviderResult, bool]:
        if response.status_code in {401, 403}:
            return self._failure(
                ProviderErrorCategory.AUTHENTICATION_ERROR, "OpenRouter authentication was rejected."
            ), False
        if response.status_code == 429:
            return self._failure(
                ProviderErrorCategory.RATE_LIMITED, "OpenRouter rate limit was reached."
            ), True
        if response.status_code in {408, 502, 503, 504} or response.status_code >= 500:
            return self._failure(
                ProviderErrorCategory.PROVIDER_UNAVAILABLE, "OpenRouter is temporarily unavailable."
            ), True
        if response.status_code == 404:
            return self._failure(
                ProviderErrorCategory.MODEL_UNAVAILABLE, "The configured model is unavailable."
            ), False
        if response.status_code in {400, 402, 409, 422}:
            category = self._classify_client_error(response)
            return self._failure(category, _safe_reason(category)), False
        if not response.is_success:
            return self._failure(
                ProviderErrorCategory.UNEXPECTED_PROVIDER_ERROR,
                "OpenRouter rejected the request.",
            ), False
        if len(response.content) > self.config.max_response_bytes:
            return self._failure(
                ProviderErrorCategory.INVALID_STRUCTURED_RESPONSE,
                "Model response exceeded the configured size limit.",
            ), False
        try:
            envelope = response.json()
            content = envelope["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError
            usage = envelope.get("usage") or {}
            output_tokens = usage.get("completion_tokens", 0)
            if not isinstance(output_tokens, int) or isinstance(output_tokens, bool) or output_tokens < 0:
                raise TypeError
            if self._output_tokens + output_tokens > self.config.max_output_tokens_per_run:
                return self._failure(
                    ProviderErrorCategory.QUOTA_EXHAUSTED,
                    "Review output-token quota was exhausted.",
                    skipped=True,
                ), False
            self._output_tokens += output_tokens
            json.loads(content)
            structured = StructuredReview.model_validate_json(content, strict=True)
        except (ValueError, KeyError, IndexError, TypeError, ValidationError):
            return self._failure(
                ProviderErrorCategory.INVALID_STRUCTURED_RESPONSE,
                "Model response did not match the required review schema.",
            ), True
        findings: list[Finding] = []
        for item in structured.findings:
            if item.file_path not in chunk.files:
                return self._failure(
                    ProviderErrorCategory.INVALID_STRUCTURED_RESPONSE,
                    "Model response referenced a file outside the submitted chunk.",
                ), True
            line = item.line_number
            if line is not None and line not in chunk.changed_lines.get(item.file_path, []):
                line = None
            findings.append(Finding(
                file=item.file_path,
                title=item.title,
                explanation=item.explanation,
                severity=item.severity.value,
                category=item.category.value,
                line=line,
                suggested_remediation=item.suggested_remediation,
                confidence=item.confidence,
                chunk_id=chunk.chunk_id,
            ))
        return ProviderResult(
            findings=findings,
            summary=structured.summary,
            overall_risk=structured.overall_risk.value,
            input_tokens=chunk.estimated_tokens,
            output_tokens=output_tokens,
            model=self.config.model,
        ), False

    def _classify_client_error(self, response: httpx.Response) -> ProviderErrorCategory:
        try:
            text = json.dumps(response.json()).casefold()
        except ValueError:
            text = ""
        if "privacy" in text or "zdr" in text or "data policy" in text:
            return ProviderErrorCategory.PRIVACY_REQUIREMENT_UNAVAILABLE
        if "response_format" in text or "structured" in text or "parameter" in text:
            return ProviderErrorCategory.UNSUPPORTED_CAPABILITY
        if "context" in text or "too large" in text or "token" in text:
            return ProviderErrorCategory.PAYLOAD_TOO_LARGE
        return ProviderErrorCategory.UNEXPECTED_PROVIDER_ERROR

    def _quota_failure(self, input_tokens: int, chunk_attempts: int = 0) -> ProviderResult | None:
        elapsed = time.monotonic() - self._started if self._started else 0
        execution_limit = self.config.max_execution_seconds
        if self._context is not None:
            execution_limit = min(execution_limit, self._context.max_execution_seconds)
        if (
            self._requests >= self.config.max_requests_per_run
            or chunk_attempts >= self.config.max_requests_per_chunk
            or self._input_tokens + input_tokens > self.config.max_input_tokens_per_run
            or elapsed >= execution_limit
        ):
            return self._failure(
                ProviderErrorCategory.QUOTA_EXHAUSTED,
                "Configured AI review quota or execution limit was exhausted.",
                skipped=True,
            )
        return None

    def _failure(
        self,
        category: ProviderErrorCategory,
        reason: str,
        *,
        skipped: bool = False,
    ) -> ProviderResult:
        return ProviderResult(
            valid=False,
            error_category=category.value,
            safe_reason=reason,
            skipped=skipped,
            model=self.config.model,
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"}
        if self.config.app_url:
            headers["HTTP-Referer"] = self.config.app_url
        if self.config.app_title:
            headers["X-OpenRouter-Title"] = self.config.app_title
        return headers

    def _log_result(self, chunk: ReviewChunk, result: ProviderResult) -> None:
        self._retries += result.retry_count
        self.logger.info(
            "provider result model=%s chunk_id=%s estimated_input_tokens=%d output_tokens=%d "
            "duration_ms=%d retries=%d validation=%s findings=%d failure_category=%s",
            self.config.model,
            chunk.chunk_id,
            chunk.estimated_tokens,
            result.output_tokens,
            result.duration_ms,
            result.retry_count,
            "valid" if result.valid else "invalid",
            len(result.findings),
            result.error_category or "none",
        )


def _safe_reason(category: ProviderErrorCategory) -> str:
    reasons = {
        ProviderErrorCategory.PRIVACY_REQUIREMENT_UNAVAILABLE: "No provider route satisfies the required privacy policy.",
        ProviderErrorCategory.UNSUPPORTED_CAPABILITY: "The configured route does not support required response parameters.",
        ProviderErrorCategory.PAYLOAD_TOO_LARGE: "The provider rejected the validated payload size.",
    }
    return reasons.get(category, "OpenRouter rejected the request.")


def _retry_after(response: httpx.Response) -> float:
    raw = response.headers.get("Retry-After", "")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0
