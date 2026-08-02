from __future__ import annotations

import re
from dataclasses import replace

from .config import RunnerConfig
from .models import DiffFile, DiffHunk, DiffLine


def redact_files(files: list[DiffFile], config: RunnerConfig) -> tuple[list[DiffFile], int, dict[str, int]]:
    compiled = [(name, re.compile(pattern)) for name, pattern in config.redaction_rules]
    counts: dict[str, int] = {}
    sequence: dict[str, int] = {}

    def redact(text: str) -> str:
        placeholders: dict[str, str] = {}
        for name, pattern in compiled:
            def replacement(match: re.Match[str], rule_name: str = name) -> str:
                if "\x00review_redaction_" in match.group(0):
                    return match.group(0)
                sequence[rule_name] = sequence.get(rule_name, 0) + 1
                counts[rule_name] = counts.get(rule_name, 0) + 1
                marker = f"[REDACTED:{rule_name.upper()}:{sequence[rule_name]}]"
                placeholder = f"\x00review_redaction_{len(placeholders)}\x00"
                placeholders[placeholder] = marker
                if match.lastindex:
                    return (match.group(1) or "") + placeholder
                return placeholder
            text = pattern.sub(replacement, text)
        for placeholder, marker in placeholders.items():
            text = text.replace(placeholder, marker)
        return text

    redacted: list[DiffFile] = []
    for file in files:
        headers = [redact(header) for header in file.headers]
        hunks: list[DiffHunk] = []
        private_key_marker: str | None = None
        for hunk in file.hunks:
            lines: list[DiffLine] = []
            for line in hunk.lines:
                if private_key_marker:
                    prefix = line.text[:1] if line.text[:1] in {"+", "-", " "} else ""
                    text = prefix + private_key_marker
                    if "-----END " in line.text and "PRIVATE KEY-----" in line.text:
                        private_key_marker = None
                else:
                    text = redact(line.text)
                    if (
                        "-----BEGIN " in line.text
                        and "PRIVATE KEY-----" in line.text
                        and "private_key" in sequence
                    ):
                        private_key_marker = f"[REDACTED:PRIVATE_KEY:{sequence['private_key']}]"
                        if "-----END " in line.text:
                            private_key_marker = None
                lines.append(replace(line, text=text))
            hunks.append(replace(hunk, header=redact(hunk.header), lines=lines))
        redacted.append(replace(file, headers=headers, hunks=hunks))
    return redacted, sum(counts.values()), counts
