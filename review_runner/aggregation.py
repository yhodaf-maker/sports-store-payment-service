from __future__ import annotations

import re

from .models import (
    ChunkResult,
    Finding,
    InclusionStatus,
    ReviewChunk,
    ReviewResult,
    SkippedItem,
)


def aggregate_results(
    chunks: list[ReviewChunk],
    chunk_results: list[ChunkResult],
    skipped: list[SkippedItem],
    file_statuses: dict[str, InclusionStatus],
    redaction_count: int,
    provider_metrics: dict[str, object] | None = None,
) -> ReviewResult:
    findings: dict[tuple[object, ...], Finding] = {}
    processed: list[str] = []
    failed: dict[str, str] = {}
    failure_details: dict[str, dict[str, object]] = {}
    summaries: list[str] = []
    risks: list[str] = []
    for chunk_result in chunk_results:
        if chunk_result.error_category:
            failed[chunk_result.chunk_id] = chunk_result.error_category
            continue
        result = chunk_result.result
        if result is None or not result.valid:
            failed[chunk_result.chunk_id] = result.error_category if result else "empty_response"
            if result:
                failure_details[chunk_result.chunk_id] = {
                    "failure_category": result.error_category,
                    "reason": result.safe_reason,
                    "retry_attempted": result.retry_attempted,
                    "retry_count": result.retry_count,
                    "skipped": result.skipped,
                    "partial": result.partial,
                }
            continue
        processed.append(chunk_result.chunk_id)
        if result.summary and result.summary not in summaries:
            summaries.append(result.summary)
        risks.append(result.overall_risk)
        for finding in result.findings:
            findings.setdefault(_dedupe_key(finding), finding)

    ordered = sorted(
        findings.values(),
        key=lambda finding: (
            finding.file.casefold(), finding.line is None, finding.line or 0,
            finding.category.casefold(), finding.title.casefold(),
            _normalize(finding.explanation),
        ),
    )
    return ReviewResult(
        findings=ordered,
        skipped=sorted(skipped, key=lambda item: (item.path, item.reason, item.hunk_header or "")),
        processed_chunks=sorted(processed),
        failed_chunks=dict(sorted(failed.items())),
        generated_chunks=[chunk.chunk_id for chunk in chunks],
        file_statuses=dict(sorted(file_statuses.items())),
        redaction_count=redaction_count,
        estimated_tokens=sum(chunk.estimated_tokens for chunk in chunks),
        failure_details=dict(sorted(failure_details.items())),
        partial=bool(processed and failed),
        ai_review_skipped=bool(chunks and not processed),
        summary=" ".join(summaries),
        overall_risk=_highest_risk(risks),
        provider_metrics=provider_metrics or {},
    )


def _dedupe_key(finding: Finding) -> tuple[object, ...]:
    return (
        finding.file.casefold(),
        finding.line,
        _normalize(finding.hunk or ""),
        finding.category.casefold().strip(),
        _normalize(finding.title),
        _normalize(finding.explanation),
    )


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _highest_risk(risks: list[str]) -> str:
    order = {"INFO": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
    return max(risks, key=lambda risk: order.get(risk, 0), default="INFO")
