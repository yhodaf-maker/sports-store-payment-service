from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

DEFAULT_EXCLUDES = (
    "**/*.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Pipfile.lock",
    "**/vendor/**",
    "**/node_modules/**",
    "**/dist/**",
    "**/build/**",
    "**/target/**",
    "**/.next/**",
    "**/generated/**",
    "**/gen/**",
    "**/openapi/**",
    "**/*_generated.*",
    "**/*.generated.*",
    "**/*_client.generated.*",
    "**/*generated*client*.*",
    "**/*openapi*client*.*",
    "**/*.min.*",
    "**/*.map",
    "**/*.png",
    "**/*.jpg",
    "**/*.jpeg",
    "**/*.gif",
    "**/*.webp",
    "**/*.ico",
    "**/*.woff*",
    "**/*.ttf",
    "**/*.otf",
    "**/*.pdf",
    "**/*.zip",
    "**/*.tar",
    "**/*.gz",
    "**/*.7z",
)

DEFAULT_CODE_TYPES = (
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs", ".rb",
    ".php", ".cs", ".cpp", ".cc", ".c", ".h", ".hpp", ".swift", ".kt",
    ".kts", ".scala", ".sh", ".bash", ".zsh", ".sql", ".html", ".css",
    ".scss", ".vue", ".svelte", ".json", ".yaml", ".yml", ".toml", ".xml",
    ".graphql", ".proto", ".tf", ".hcl", ".md", ".dockerfile",
)

DEFAULT_REDACTIONS = (
    ("private_key", r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    ("aws_access_key", r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    ("github_token", r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    ("authorization", r"(?i)(authorization\s*[:=]\s*(?:bearer|basic)\s+)[^\s\"']+"),
    ("credential", r"(?i)((?:api[_-]?key|secret|password|passwd|token)\s*[:=]\s*[\"']?)[^\s\"']{8,}"),
)


@dataclass(frozen=True)
class RunnerConfig:
    included_file_types: tuple[str, ...] = DEFAULT_CODE_TYPES
    excluded_patterns: tuple[str, ...] = DEFAULT_EXCLUDES
    sensitive_patterns: tuple[str, ...] = (".env", ".env.*", "**/.env", "**/.env.*")
    max_file_bytes: int = 50 * 1024
    max_file_lines: int = 1500
    max_files: int = 100
    max_total_pr_tokens: int = 100_000
    max_chunk_input_tokens: int = 24_000
    max_chunks: int = 20
    model_context_tokens: int = 32_000
    reserved_instruction_tokens: int = 1500
    reserved_schema_tokens: int = 750
    reserved_metadata_tokens: int = 500
    reserved_output_tokens: int = 4000
    safety_margin_tokens: int = 1000
    oversized_file_behavior: str = "truncate"
    redaction_rules: tuple[tuple[str, str], ...] = DEFAULT_REDACTIONS
    logging_level: str = "INFO"
    max_execution_seconds: float = 300.0

    @classmethod
    def load(cls, path: str | Path | None = None) -> RunnerConfig:
        values: dict[str, Any] = {}
        if path:
            with Path(path).open(encoding="utf-8") as config_file:
                loaded = json.load(config_file)
            if not isinstance(loaded, dict):
                raise ValueError("configuration file must contain a JSON object")
            values.update(loaded)

        return cls.from_mapping(values)

    @classmethod
    def from_mapping(cls, supplied: dict[str, Any] | None = None) -> RunnerConfig:
        values = dict(supplied or {})
        known = {item.name: item for item in fields(cls)}
        unknown = set(values) - set(known)
        if unknown:
            raise ValueError(f"unknown configuration fields: {', '.join(sorted(unknown))}")

        defaults = cls()
        for name in known:
            env_name = f"REVIEW_RUNNER_{name.upper()}"
            if env_name in os.environ:
                values[name] = _parse_environment_value(os.environ[env_name], getattr(defaults, name))

        for name in ("included_file_types", "excluded_patterns", "sensitive_patterns"):
            if name in values:
                values[name] = tuple(values[name])
        if "redaction_rules" in values:
            raw_rules = values["redaction_rules"]
            if isinstance(raw_rules, dict):
                raw_rules = raw_rules.items()
            values["redaction_rules"] = tuple(tuple(rule) for rule in raw_rules)

        config = replace(defaults, **values)
        config.validate()
        return config

    def validate(self) -> None:
        numeric = (
            "max_file_bytes", "max_file_lines", "max_files", "max_total_pr_tokens",
            "max_chunk_input_tokens", "max_chunks", "model_context_tokens",
            "reserved_instruction_tokens", "reserved_schema_tokens", "reserved_metadata_tokens",
            "reserved_output_tokens", "safety_margin_tokens",
        )
        for name in numeric:
            value = getattr(self, name)
            minimum = 0 if name.startswith("reserved_") or name == "safety_margin_tokens" else 1
            if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
                raise ValueError(f"{name} has an invalid value")
        if self.oversized_file_behavior not in {"skip", "truncate"}:
            raise ValueError("oversized_file_behavior must be 'skip' or 'truncate'")
        if self.logging_level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("logging_level is invalid")
        if (
            not isinstance(self.max_execution_seconds, (int, float))
            or isinstance(self.max_execution_seconds, bool)
            or self.max_execution_seconds <= 0
        ):
            raise ValueError("max_execution_seconds has an invalid value")
        for name, pattern in self.redaction_rules:
            if not name or not pattern:
                raise ValueError("redaction rules require a name and pattern")
            re.compile(pattern)
        if self.available_diff_tokens <= 0:
            raise ValueError("model token reservations leave no room for diff input")

    @property
    def available_diff_tokens(self) -> int:
        reserved = (
            self.reserved_instruction_tokens + self.reserved_schema_tokens
            + self.reserved_metadata_tokens + self.reserved_output_tokens
            + self.safety_margin_tokens
        )
        return min(self.max_chunk_input_tokens, self.model_context_tokens - reserved)


def _parse_environment_value(raw: str, default: Any) -> Any:
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError(f"invalid integer environment value: {raw!r}") from exc
    if isinstance(default, float):
        try:
            return float(raw)
        except ValueError as exc:
            raise ValueError("invalid floating-point environment value") from exc
    if isinstance(default, tuple):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = [item.strip() for item in raw.split(",") if item.strip()]
        return parsed
    return raw
