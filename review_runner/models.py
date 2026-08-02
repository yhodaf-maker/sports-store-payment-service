from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class ChangeType(str, Enum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"
    BINARY = "binary"


class InclusionStatus(str, Enum):
    FULL = "fully_reviewed"
    PARTIAL = "partially_included"
    SKIPPED = "skipped"


class Severity(str, Enum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class FindingCategory(str, Enum):
    STYLE = "STYLE"
    PERFORMANCE = "PERFORMANCE"
    SECURITY = "SECURITY"
    BUG = "BUG"
    RELIABILITY = "RELIABILITY"
    MAINTAINABILITY = "MAINTAINABILITY"


class ProviderErrorCategory(str, Enum):
    CONFIGURATION_ERROR = "configuration_error"
    AUTHENTICATION_ERROR = "authentication_error"
    MODEL_UNAVAILABLE = "model_unavailable"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    PRIVACY_REQUIREMENT_UNAVAILABLE = "privacy_requirement_unavailable"
    RATE_LIMITED = "rate_limited"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    NETWORK_TIMEOUT = "network_timeout"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    INVALID_STRUCTURED_RESPONSE = "invalid_structured_response"
    QUOTA_EXHAUSTED = "quota_exhausted"
    UNEXPECTED_PROVIDER_ERROR = "unexpected_provider_error"


@dataclass
class DiffLine:
    kind: str
    text: str
    old_line: int | None = None
    new_line: int | None = None


@dataclass
class DiffHunk:
    header: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[DiffLine] = field(default_factory=list)

    def render(self) -> str:
        return "\n".join([self.header, *(line.text for line in self.lines)])


@dataclass
class DiffFile:
    path: str
    previous_path: str | None
    change_type: ChangeType
    headers: list[str]
    hunks: list[DiffHunk]
    binary: bool = False
    added_lines: int = 0
    removed_lines: int = 0
    status: InclusionStatus = InclusionStatus.FULL

    def render_headers(self) -> str:
        return "\n".join(self.headers)

    def render(self) -> str:
        sections = [self.render_headers(), *(hunk.render() for hunk in self.hunks)]
        return "\n".join(section for section in sections if section)


@dataclass(frozen=True)
class SkippedItem:
    path: str
    reason: str
    detail: str = ""
    hunk_header: str | None = None


@dataclass
class ReviewChunk:
    chunk_id: str
    content: str
    files: list[str]
    estimated_tokens: int
    hunk_headers: list[str] = field(default_factory=list)
    changed_lines: dict[str, list[int]] = field(default_factory=dict)


@dataclass(frozen=True)
class Finding:
    file: str
    title: str
    explanation: str
    severity: str
    category: str
    line: int | None = None
    hunk: str | None = None
    suggested_remediation: str = ""
    confidence: float = 1.0
    chunk_id: str | None = None


@dataclass
class ProviderResult:
    findings: list[Finding] = field(default_factory=list)
    valid: bool = True
    error_category: str | None = None
    summary: str = ""
    overall_risk: str = "INFO"
    safe_reason: str | None = None
    retry_attempted: bool = False
    retry_count: int = 0
    skipped: bool = False
    partial: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    model: str | None = None


@dataclass(frozen=True)
class ReviewContext:
    commit_sha: str | None
    model_context_tokens: int
    max_chunk_input_tokens: int
    reserved_output_tokens: int
    max_execution_seconds: float


@dataclass
class ChunkResult:
    chunk_id: str
    result: ProviderResult | None = None
    error_category: str | None = None


@dataclass
class ReviewResult:
    findings: list[Finding]
    skipped: list[SkippedItem]
    processed_chunks: list[str]
    failed_chunks: dict[str, str]
    generated_chunks: list[str]
    file_statuses: dict[str, InclusionStatus]
    redaction_count: int
    estimated_tokens: int
    failure_details: dict[str, dict[str, Any]] = field(default_factory=dict)
    partial: bool = False
    ai_review_skipped: bool = False
    summary: str = ""
    overall_risk: str = "INFO"
    provider_metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
