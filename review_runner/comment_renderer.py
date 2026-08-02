from __future__ import annotations

import html
import re
from collections import Counter

from .models import Finding, InclusionStatus, ReviewResult

COMMENT_MARKER = "<!-- sports-store-ai-review:v1 -->"
MAX_COMMENT_LENGTH = 60_000
MAX_FINDINGS = 20
_SEVERITY = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}


def render_in_progress(commit_sha: str) -> str:
    return _limit_comment(
        "\n".join(
            [
                "## Advisory AI Review: In Progress",
                "",
                f"Reviewed commit: `{_safe_sha(commit_sha)}`",
                "",
                "The sanitized Pull Request diff is being reviewed. This advisory review does not replace deterministic security and quality checks.",
                "",
                COMMENT_MARKER,
            ]
        )
    )


def render_result(result: ReviewResult, commit_sha: str) -> str:
    state = display_state(result)
    reviewed = sum(
        status is not InclusionStatus.SKIPPED
        for status in result.file_statuses.values()
    )
    skipped_files = sum(
        status is InclusionStatus.SKIPPED for status in result.file_statuses.values()
    )
    partial_files = sum(
        status is InclusionStatus.PARTIAL for status in result.file_statuses.values()
    )
    summary = result.summary or _default_summary(result, state)
    lines = [
        f"## Advisory AI Review: {state}",
        "",
        f"Reviewed commit: `{_safe_sha(commit_sha)}`",
        f"Overall risk: **{_sanitize(result.overall_risk, 20)}**",
        f"Review summary: {_sanitize(summary, 2_000)}",
        "",
        "### Coverage",
        f"Reviewed files: **{reviewed}**",
        f"Skipped files: **{skipped_files}**",
        f"Partially reviewed files: **{partial_files}**",
        f"Processed chunks: **{len(result.processed_chunks)} / {len(result.generated_chunks)}**",
    ]
    reasons = Counter(item.reason for item in result.skipped)
    if reasons:
        rendered = ", ".join(
            f"{_sanitize(reason, 80)} ({count})"
            for reason, count in reasons.most_common(10)
        )
        if len(reasons) > 10:
            rendered += f", and {len(reasons) - 10} additional reason types"
        lines.append(f"Skipped content: {rendered}")
    failures = Counter(result.failed_chunks.values())
    if failures:
        lines.append(
            "Provider limitations: "
            + ", ".join(
                f"{_sanitize(category, 80)} ({count})"
                for category, count in failures.most_common(8)
            )
        )
    lines.extend(["", "### Validated Findings"])
    findings = sorted(result.findings, key=_finding_key)
    if not findings:
        lines.append("No actionable findings were identified in the reviewed content.")
    for finding in findings[:MAX_FINDINGS]:
        lines.extend(_render_finding(finding))
    omitted = len(findings) - MAX_FINDINGS
    if omitted > 0:
        lines.extend(
            [
                "",
                f"**{omitted} additional validated findings were omitted from this size-limited comment.**",
            ]
        )
    metrics = result.provider_metrics
    if metrics:
        lines.extend(
            [
                "",
                "### Operational Summary",
                f"Provider requests: **{_safe_int(metrics.get('request_count'))}**",
                f"Approximate input tokens: **{_safe_int(metrics.get('input_tokens'))}**",
                f"Reported output tokens: **{_safe_int(metrics.get('output_tokens'))}**",
                f"Provider retries: **{_safe_int(metrics.get('retry_count'))}**",
            ]
        )
    lines.extend(
        [
            "",
            "> This review is advisory. Deterministic CI, security scans, and human review remain authoritative.",
            "",
            COMMENT_MARKER,
        ]
    )
    return _limit_comment("\n".join(lines))


def render_safe_failure(
    commit_sha: str, category: str, *, skipped: bool = False
) -> str:
    state = "Skipped" if skipped else "Failed Safely"
    return _limit_comment(
        "\n".join(
            [
                f"## Advisory AI Review: {state}",
                "",
                f"Reviewed commit: `{_safe_sha(commit_sha)}`",
                f"Reason: {_sanitize(category, 200)}",
                "",
                "No approval is implied. Deterministic CI, security scans, and human review remain authoritative.",
                "",
                COMMENT_MARKER,
            ]
        )
    )


def display_state(result: ReviewResult) -> str:
    if result.partial or (result.processed_chunks and result.failed_chunks):
        return "Partial"
    if result.ai_review_skipped or (
        result.generated_chunks and not result.processed_chunks
    ):
        return "Skipped"
    if not result.generated_chunks:
        return "Skipped"
    return "Completed"


def normalized_status(result: ReviewResult) -> str:
    if result.partial or (result.processed_chunks and result.failed_chunks):
        return "partial"
    if result.ai_review_skipped:
        return "provider_failure"
    if not result.generated_chunks:
        return "skipped"
    if not result.findings:
        return "completed_no_findings"
    return "completed"


def _render_finding(finding: Finding) -> list[str]:
    location = _sanitize(finding.file, 1_024)
    if finding.line is not None:
        location += f":{finding.line}"
    code_location = location.replace("`", "\\`")
    confidence = min(max(float(finding.confidence), 0), 1)
    return [
        "",
        f"#### {_sanitize(finding.severity, 20)} / {_sanitize(finding.category, 40)}: {_sanitize(finding.title, 200)}",
        f"Location: `{code_location}`",
        f"Explanation: {_sanitize(finding.explanation, 1_500)}",
        f"Suggested remediation: {_sanitize(finding.suggested_remediation, 1_500)}",
        f"Confidence: **{confidence:.0%}**",
    ]


def _finding_key(finding: Finding) -> tuple[object, ...]:
    return (
        _SEVERITY.get(finding.severity, 5),
        finding.file.casefold(),
        finding.line or 0,
    )


def _default_summary(result: ReviewResult, state: str) -> str:
    if state == "Partial":
        return "Only part of the eligible content was reviewed; skipped content is summarized below."
    if state == "Skipped":
        return "The AI review did not complete and must not be interpreted as approval."
    if result.findings:
        return f"The review identified {len(result.findings)} validated finding(s)."
    return "No actionable findings were identified in the reviewed content."


def _sanitize(value: object, maximum: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = text.replace(COMMENT_MARKER, "[marker removed]")
    text = re.sub(
        r"(?i)\b(?:javascript|data|vbscript):", "unsafe-scheme-removed:", text
    )
    text = re.sub(
        r"(?i)\b(approved?|approval|mergeable|checks? passed)\b",
        "advisory assessment",
        text,
    )
    text = html.escape(text, quote=False)
    text = re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", text)
    text = text.replace("@", "@&#8203;")
    if len(text) > maximum:
        text = text[: max(0, maximum - 3)].rstrip() + "..."
    return text or "Not available"


def _safe_sha(value: str) -> str:
    return value if re.fullmatch(r"[0-9a-f]{40}", value) else "invalid"


def _safe_int(value: object) -> int:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else 0
    )


def _limit_comment(body: str) -> str:
    if len(body) <= MAX_COMMENT_LENGTH:
        return body
    suffix = (
        "\n\nDetailed output was truncated to fit the persistent comment limit.\n\n"
        + COMMENT_MARKER
    )
    return body[: MAX_COMMENT_LENGTH - len(suffix)].rstrip() + suffix
