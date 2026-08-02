from __future__ import annotations

from dataclasses import replace
from pathlib import PurePosixPath

from .config import RunnerConfig
from .models import DiffFile, InclusionStatus, SkippedItem


def filter_files(
    files: list[DiffFile], config: RunnerConfig
) -> tuple[list[DiffFile], list[SkippedItem], dict[str, InclusionStatus]]:
    included: list[DiffFile] = []
    skipped: list[SkippedItem] = []
    statuses: dict[str, InclusionStatus] = {}
    for file in files:
        reason = _filter_reason(file, config)
        if reason:
            statuses[file.path] = InclusionStatus.SKIPPED
            skipped.append(SkippedItem(file.path, reason))
            continue
        if len(included) >= config.max_files:
            statuses[file.path] = InclusionStatus.SKIPPED
            skipped.append(SkippedItem(file.path, "maximum_file_count"))
            continue
        limited, omission = _apply_file_limits(file, config)
        if limited is None:
            statuses[file.path] = InclusionStatus.SKIPPED
            skipped.append(omission)
            continue
        included.append(limited)
        statuses[file.path] = limited.status
        if omission:
            skipped.append(omission)
    return included, skipped, statuses


def _filter_reason(file: DiffFile, config: RunnerConfig) -> str | None:
    paths = [file.path]
    if file.previous_path:
        paths.append(file.previous_path)
    if file.binary:
        return "binary_file"
    if any(_matches(path, config.sensitive_patterns) for path in paths):
        return "sensitive_path"
    if any(_matches(path, config.excluded_patterns) for path in paths):
        return "excluded_pattern"
    suffixes = PurePosixPath(file.path.lower()).suffixes
    effective_suffix = "".join(suffixes[-2:]) if suffixes[-1:] == [".map"] else (suffixes[-1] if suffixes else "")
    basename = PurePosixPath(file.path).name.lower()
    if (
        config.included_file_types
        and effective_suffix not in config.included_file_types
        and basename not in {"dockerfile", "makefile", "jenkinsfile"}
    ):
        return "unsupported_file_type"
    return None


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    pure_path = PurePosixPath(path.lstrip("./"))
    for pattern in patterns:
        normalized = pattern.lstrip("./")
        if pure_path.match(normalized) or (
            normalized.startswith("**/") and pure_path.match(normalized[3:])
        ):
            return True
        directory = normalized.removesuffix("/**").rstrip("/")
        if normalized.endswith("/**") and (str(pure_path) == directory or str(pure_path).startswith(directory + "/")):
            return True
    return False


def _apply_file_limits(
    file: DiffFile, config: RunnerConfig
) -> tuple[DiffFile | None, SkippedItem | None]:
    rendered = file.render()
    if len(rendered.encode("utf-8")) <= config.max_file_bytes and len(rendered.splitlines()) <= config.max_file_lines:
        return file, None
    if config.oversized_file_behavior == "skip":
        return None, SkippedItem(file.path, "file_size_limit", "file diff exceeds configured limit")

    kept = []
    for hunk in file.hunks:
        candidate = replace(file, hunks=[*kept, hunk], status=InclusionStatus.PARTIAL)
        text = candidate.render()
        if len(text.encode("utf-8")) > config.max_file_bytes or len(text.splitlines()) > config.max_file_lines:
            break
        kept.append(hunk)
    if not kept:
        return None, SkippedItem(file.path, "file_size_limit", "no complete hunk fits configured limit")
    limited = replace(file, hunks=kept, status=InclusionStatus.PARTIAL)
    return limited, SkippedItem(
        file.path,
        "file_partially_included",
        f"omitted {len(file.hunks) - len(kept)} hunk(s) at file limit",
    )
