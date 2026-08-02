from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

from .models import ChangeType, DiffFile, DiffHunk, DiffLine, SkippedItem

HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class ParseResult:
    files: list[DiffFile] = field(default_factory=list)
    skipped: list[SkippedItem] = field(default_factory=list)


def parse_diff(diff_text: str) -> ParseResult:
    result = ParseResult()
    if not diff_text.strip():
        return result
    unsupported_sections = sum(
        line.startswith("diff --") and not line.startswith("diff --git ")
        for line in diff_text.splitlines()
    )
    result.skipped.extend(
        SkippedItem("<patch>", "unsupported_diff", "unsupported diff section")
        for _ in range(unsupported_sections)
    )
    sections = _split_sections(diff_text)
    if not sections:
        result.skipped.append(SkippedItem("<patch>", "unsupported_diff", "no diff --git sections"))
        return result
    for section in sections:
        try:
            result.files.append(_parse_file(section))
        except (ValueError, IndexError) as exc:
            result.skipped.append(SkippedItem(_best_effort_path(section), "parse_error", str(exc)))
    return result


def _split_sections(text: str) -> list[list[str]]:
    sections: list[list[str]] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("diff --git "):
            if current:
                sections.append(current)
            current = [line]
        elif current is not None:
            current.append(line)
    if current:
        sections.append(current)
    return sections


def _parse_file(lines: list[str]) -> DiffFile:
    try:
        parts = shlex.split(lines[0])
    except ValueError as exc:
        raise ValueError("malformed diff header") from exc
    if len(parts) != 4 or not parts[2].startswith("a/") or not parts[3].startswith("b/"):
        raise ValueError("malformed diff --git paths")
    old_path = parts[2][2:]
    path = parts[3][2:]
    headers: list[str] = []
    hunks: list[DiffHunk] = []
    rename_from = None
    rename_to = None
    added_file = deleted_file = binary = False
    index = 0
    while index < len(lines):
        line = lines[index]
        match = HUNK_HEADER.match(line)
        if match:
            hunk, index = _parse_hunk(lines, index, match)
            hunks.append(hunk)
            continue
        headers.append(line)
        if line.startswith("rename from "):
            rename_from = line.removeprefix("rename from ")
        elif line.startswith("rename to "):
            rename_to = line.removeprefix("rename to ")
        elif line.startswith("new file mode ") or line == "--- /dev/null":
            added_file = True
        elif line.startswith("deleted file mode ") or line == "+++ /dev/null":
            deleted_file = True
        elif line.startswith(("Binary files ", "GIT binary patch")):
            binary = True
        index += 1
    if rename_to:
        path = rename_to
    if binary:
        change_type = ChangeType.BINARY
    elif rename_from or rename_to:
        change_type = ChangeType.RENAMED
    elif added_file:
        change_type = ChangeType.ADDED
    elif deleted_file:
        change_type = ChangeType.DELETED
    else:
        change_type = ChangeType.MODIFIED
    return DiffFile(
        path=path,
        previous_path=rename_from or (old_path if change_type == ChangeType.RENAMED else None),
        change_type=change_type,
        headers=headers,
        hunks=hunks,
        binary=binary,
        added_lines=sum(line.kind == "added" for hunk in hunks for line in hunk.lines),
        removed_lines=sum(line.kind == "removed" for hunk in hunks for line in hunk.lines),
    )


def _parse_hunk(lines: list[str], index: int, match: re.Match[str]) -> tuple[DiffHunk, int]:
    old_start, old_count, new_start, new_count = (
        int(match.group(1)), int(match.group(2) or 1),
        int(match.group(3)), int(match.group(4) or 1),
    )
    hunk = DiffHunk(lines[index], old_start, old_count, new_start, new_count)
    old_line, new_line = old_start, new_start
    index += 1
    while index < len(lines) and not lines[index].startswith("diff --git "):
        line = lines[index]
        if HUNK_HEADER.match(line):
            break
        if line.startswith("+"):
            hunk.lines.append(DiffLine("added", line, None, new_line))
            new_line += 1
        elif line.startswith("-"):
            hunk.lines.append(DiffLine("removed", line, old_line, None))
            old_line += 1
        elif line.startswith(" "):
            hunk.lines.append(DiffLine("context", line, old_line, new_line))
            old_line += 1
            new_line += 1
        elif line == r"\ No newline at end of file":
            hunk.lines.append(DiffLine("marker", line))
        else:
            raise ValueError(f"unsupported line in hunk {hunk.header!r}")
        index += 1
    return hunk, index


def _best_effort_path(lines: list[str]) -> str:
    try:
        parts = shlex.split(lines[0])
        return parts[-1].removeprefix("b/")
    except (ValueError, IndexError):
        return "<unknown>"
