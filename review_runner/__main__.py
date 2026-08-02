from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from .config import RunnerConfig
from .input import read_diff
from .mock_provider import MockReviewProvider
from .runner import ReviewRunner


def main() -> int:
    parser = argparse.ArgumentParser(description="Review a unified pull request diff offline")
    parser.add_argument("--diff", type=Path, help="patch file; stdin is used when omitted")
    parser.add_argument("--config", type=Path, help="JSON runner configuration")
    parser.add_argument(
        "--mock-scenario", default="findings", choices=sorted(MockReviewProvider.SCENARIOS)
    )
    args = parser.parse_args()
    try:
        config = RunnerConfig.load(args.config)
        diff_text = read_diff(args.diff)
        logging.basicConfig(
            level=config.logging_level.upper(),
            format="%(levelname)s %(name)s %(message)s",
            stream=sys.stderr,
        )
        result = asyncio.run(ReviewRunner(MockReviewProvider(args.mock_scenario), config).run(diff_text))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"review-runner configuration/input error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 1 if result.failed_chunks else 0


if __name__ == "__main__":
    raise SystemExit(main())
