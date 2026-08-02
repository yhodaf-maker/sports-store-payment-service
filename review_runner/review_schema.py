from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import FindingCategory, Severity


class StructuredFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    file_path: str = Field(min_length=1, max_length=1024)
    line_number: int | None
    severity: Severity
    category: FindingCategory
    title: str = Field(min_length=1, max_length=200)
    explanation: str = Field(min_length=1, max_length=4000)
    suggested_remediation: str = Field(min_length=1, max_length=4000)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("title", "explanation", "suggested_remediation")
    @classmethod
    def reject_embedded_content(cls, value: str) -> str:
        if "\x00" in value or re.search(r"(?is)<\s*(script|iframe|object|embed)\b", value):
            raise ValueError("embedded executable content is not allowed")
        return value


class StructuredReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    summary: str = Field(min_length=1, max_length=2000)
    overall_risk: Severity
    findings: list[StructuredFinding] = Field(max_length=100)


REVIEW_JSON_SCHEMA = StructuredReview.model_json_schema()
