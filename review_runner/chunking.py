from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .config import RunnerConfig
from .models import DiffFile, DiffHunk, ReviewChunk, SkippedItem
from .tokens import TokenEstimator


@dataclass
class ChunkingResult:
    chunks: list[ReviewChunk]
    skipped: list[SkippedItem]


@dataclass
class _Fragment:
    path: str
    text: str
    hunk_header: str | None
    changed_lines: list[int]


def create_chunks(
    files: list[DiffFile], config: RunnerConfig, estimator: TokenEstimator
) -> ChunkingResult:
    budget = config.available_diff_tokens
    fragments: list[_Fragment] = []
    skipped: list[SkippedItem] = []
    complete = "\n".join(file.render() for file in files)
    if complete and estimator.estimate(complete) <= budget:
        fragments = [
            _Fragment(file.path, file.render(), None, _changed_lines(file.hunks)) for file in files
        ]
    else:
        for file in files:
            file_text = file.render()
            if estimator.estimate(file_text) <= budget:
                fragments.append(_Fragment(file.path, file_text, None, _changed_lines(file.hunks)))
                continue
            fragments.extend(_split_file(file, budget, estimator, skipped))

    chunks: list[ReviewChunk] = []
    current: list[_Fragment] = []
    total_tokens = 0
    for index, fragment in enumerate(fragments):
        candidate = "\n".join(item.text for item in [*current, fragment])
        if current and estimator.estimate(candidate) > budget:
            total_tokens = _append_chunk(chunks, current, estimator, total_tokens)
            current = []
            candidate = fragment.text
        fragment_tokens = estimator.estimate(fragment.text)
        candidate_tokens = estimator.estimate(candidate)
        chunk_slots_used = len(chunks) + (1 if current else 0)
        if chunk_slots_used >= config.max_chunks or total_tokens + candidate_tokens > config.max_total_pr_tokens:
            if current and len(chunks) < config.max_chunks:
                total_tokens = _append_chunk(chunks, current, estimator, total_tokens)
            for omitted in fragments[index:]:
                skipped.append(SkippedItem(
                    omitted.path,
                    "chunk_budget_limit",
                    hunk_header=omitted.hunk_header,
                ))
            current = []
            break
        if fragment_tokens > budget:
            skipped.append(SkippedItem(fragment.path, "chunk_input_limit", hunk_header=fragment.hunk_header))
            continue
        current.append(fragment)
    if current and len(chunks) < config.max_chunks:
        _append_chunk(chunks, current, estimator, total_tokens)
    return ChunkingResult(chunks, skipped)


def _split_file(
    file: DiffFile, budget: int, estimator: TokenEstimator, skipped: list[SkippedItem]
) -> list[_Fragment]:
    fragments: list[_Fragment] = []
    headers = file.render_headers()
    for hunk in file.hunks:
        text = "\n".join(part for part in (headers, hunk.render()) if part)
        if estimator.estimate(text) <= budget:
            fragments.append(_Fragment(file.path, text, hunk.header, _changed_lines([hunk])))
            continue
        fragments.extend(_split_hunk(file.path, headers, hunk, budget, estimator, skipped))
    if not file.hunks:
        skipped.append(SkippedItem(file.path, "chunk_input_limit", "file headers exceed chunk budget"))
    return fragments


def _split_hunk(
    path: str,
    headers: str,
    hunk: DiffHunk,
    budget: int,
    estimator: TokenEstimator,
    skipped: list[SkippedItem],
) -> list[_Fragment]:
    prefix = "\n".join(part for part in (headers, hunk.header) if part)
    if estimator.estimate(prefix) >= budget:
        skipped.append(SkippedItem(path, "chunk_input_limit", "diff metadata exceeds chunk budget", hunk.header))
        return []
    parts: list[_Fragment] = []
    current: list[object] = []
    for line in hunk.lines:
        candidate = "\n".join([prefix, *(item.text for item in current), line.text])
        if current and estimator.estimate(candidate) > budget:
            parts.append(_Fragment(
                path,
                "\n".join([prefix, *(item.text for item in current)]),
                hunk.header,
                [item.new_line for item in current if item.kind == "add" and item.new_line is not None],
            ))
            current = []
            candidate = f"{prefix}\n{line.text}"
        if estimator.estimate(candidate) > budget:
            skipped.append(SkippedItem(path, "chunk_input_limit", "individual diff line exceeds chunk budget", hunk.header))
            continue
        current.append(line)
    if current:
        parts.append(_Fragment(
            path,
            "\n".join([prefix, *(item.text for item in current)]),
            hunk.header,
            [item.new_line for item in current if item.kind == "add" and item.new_line is not None],
        ))
    return parts


def _append_chunk(
    chunks: list[ReviewChunk], fragments: list[_Fragment], estimator: TokenEstimator, total: int
) -> int:
    content = "\n".join(fragment.text for fragment in fragments)
    tokens = estimator.estimate(content)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:10]
    chunk_id = f"chunk-{len(chunks) + 1:04d}-{digest}"
    files = sorted({fragment.path for fragment in fragments})
    chunks.append(ReviewChunk(
        chunk_id=chunk_id,
        content=content,
        files=files,
        estimated_tokens=tokens,
        hunk_headers=[fragment.hunk_header for fragment in fragments if fragment.hunk_header],
        changed_lines={
            path: sorted({line for fragment in fragments if fragment.path == path for line in fragment.changed_lines})
            for path in files
        },
    ))
    return total + tokens


def _changed_lines(hunks: list[DiffHunk]) -> list[int]:
    return sorted({
        line.new_line for hunk in hunks for line in hunk.lines
        if line.kind == "add" and line.new_line is not None
    })
