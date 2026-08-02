from __future__ import annotations

import logging
import time

from .aggregation import aggregate_results
from .chunking import create_chunks
from .config import RunnerConfig
from .diff_parser import parse_diff
from .filtering import filter_files
from .logging_utils import get_logger
from .models import ChunkResult, InclusionStatus, ReviewContext, ReviewResult
from .provider import ReviewProvider
from .redaction import redact_files
from .tokens import ConservativeTokenEstimator, TokenEstimator


class ReviewRunner:
    def __init__(
        self,
        provider: ReviewProvider,
        config: RunnerConfig | None = None,
        token_estimator: TokenEstimator | None = None,
        logger: logging.Logger | None = None,
    ):
        self.config = config or RunnerConfig()
        self.config.validate()
        self.provider = provider
        self.token_estimator = token_estimator or ConservativeTokenEstimator()
        self.logger = logger or get_logger(self.config.logging_level)

    async def run(self, diff_text: str, commit_sha: str | None = None) -> ReviewResult:
        started = time.monotonic()
        parsed = parse_diff(diff_text)
        self.logger.info("diff parsed files=%d parse_skips=%d", len(parsed.files), len(parsed.skipped))

        files, skipped, statuses = filter_files(parsed.files, self.config)
        skipped = [*parsed.skipped, *skipped]
        for item in parsed.skipped:
            statuses.setdefault(item.path, InclusionStatus.SKIPPED)
        self.logger.info(
            "files selected included=%d skipped=%d reasons=%s",
            len(files),
            len(skipped),
            _reason_counts(skipped),
        )

        redacted, redaction_count, redaction_categories = redact_files(files, self.config)
        self.logger.info(
            "content redacted count=%d categories=%s",
            redaction_count,
            dict(sorted(redaction_categories.items())),
        )

        chunking = create_chunks(redacted, self.config, self.token_estimator)
        skipped.extend(chunking.skipped)
        chunk_paths = {path for chunk in chunking.chunks for path in chunk.files}
        for item in chunking.skipped:
            if item.path in statuses:
                statuses[item.path] = (
                    InclusionStatus.PARTIAL if item.path in chunk_paths else InclusionStatus.SKIPPED
                )
        self.logger.info(
            "chunks generated count=%d ids=%s estimated_tokens=%d",
            len(chunking.chunks),
            [chunk.chunk_id for chunk in chunking.chunks],
            sum(chunk.estimated_tokens for chunk in chunking.chunks),
        )

        results: list[ChunkResult] = []
        preparation_blocked = False
        prepare = getattr(self.provider, "prepare", None)
        if prepare and chunking.chunks:
            context = ReviewContext(
                commit_sha=commit_sha,
                model_context_tokens=self.config.model_context_tokens,
                max_chunk_input_tokens=self.config.max_chunk_input_tokens,
                reserved_output_tokens=self.config.reserved_output_tokens,
                max_execution_seconds=self.config.max_execution_seconds,
            )
            try:
                preparation_failure = await prepare(context)
            except Exception as exc:  # noqa: BLE001
                category = type(exc).__name__
                self.logger.error("provider preparation failure category=%s", category)
                preparation_failure = None
                results.extend(ChunkResult(chunk.chunk_id, error_category=category) for chunk in chunking.chunks)
                preparation_blocked = True
            if preparation_failure is not None:
                results.extend(
                    ChunkResult(chunk.chunk_id, preparation_failure) for chunk in chunking.chunks
                )
                preparation_blocked = True

        for chunk in (() if preparation_blocked else chunking.chunks):
            try:
                provider_result = await self.provider.review(chunk)
                results.append(ChunkResult(chunk.chunk_id, provider_result))
            # Provider boundaries must not abort processing of unrelated chunks.
            except Exception as exc:  # noqa: BLE001
                category = type(exc).__name__
                self.logger.error("provider failure chunk_id=%s category=%s", chunk.chunk_id, category)
                results.append(ChunkResult(chunk.chunk_id, error_category=category))

        metrics = getattr(self.provider, "metrics", None)
        result = aggregate_results(
            chunking.chunks, results, skipped, statuses, redaction_count,
            metrics if isinstance(metrics, dict) else None,
        )
        self.logger.info(
            "review complete processed=%d failed=%d duration_ms=%d",
            len(result.processed_chunks),
            len(result.failed_chunks),
            int((time.monotonic() - started) * 1000),
        )
        return result


def _reason_counts(skipped: list[object]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in skipped:
        reason = getattr(item, "reason", "unknown")
        counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))
