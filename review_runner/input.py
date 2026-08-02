from __future__ import annotations

import sys
from pathlib import Path
from typing import TextIO


def read_diff(path: Path | None, stdin: TextIO | None = None) -> str:
    """Read only the caller-supplied patch; never inspect repository files."""
    if path is not None:
        return path.read_text(encoding="utf-8")
    return (stdin or sys.stdin).read()
