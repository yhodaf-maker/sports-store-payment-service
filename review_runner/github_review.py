from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .comment_renderer import (
    COMMENT_MARKER,
    normalized_status,
    render_in_progress,
    render_result,
    render_safe_failure,
)
from .config import RunnerConfig
from .github_client import GitHubApiError, GitHubClient
from .provider import ReviewProvider
from .provider_factory import create_provider
from .runner import ReviewRunner

MAX_DIFF_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class ReviewInputs:
    repository: str
    pull_request_number: int
    base_sha: str
    head_sha: str
    reviewed_sha: str
    workflow_run_id: int
    required_ci_jobs: tuple[str, ...] = ()
    config_json: str = ""

    @classmethod
    def from_environment(cls) -> ReviewInputs:
        try:
            pull_request_number = int(os.environ["AI_REVIEW_PR_NUMBER"])
            workflow_run_id = int(os.environ["AI_REVIEW_WORKFLOW_RUN_ID"])
        except (KeyError, ValueError) as exc:
            raise ValueError("invalid required numeric review input") from exc
        try:
            required_jobs = json.loads(
                os.environ.get("AI_REVIEW_REQUIRED_JOBS_JSON", "")
            )
        except json.JSONDecodeError as exc:
            raise ValueError("required CI jobs must be valid JSON") from exc
        if not isinstance(required_jobs, list) or not all(
            isinstance(item, str) for item in required_jobs
        ):
            raise TypeError("required CI jobs must be a JSON string list")
        inputs = cls(
            repository=os.environ.get("AI_REVIEW_REPOSITORY", ""),
            pull_request_number=pull_request_number,
            base_sha=os.environ.get("AI_REVIEW_BASE_SHA", ""),
            head_sha=os.environ.get("AI_REVIEW_HEAD_SHA", ""),
            reviewed_sha=os.environ.get("AI_REVIEW_COMMIT_SHA", ""),
            workflow_run_id=workflow_run_id,
            required_ci_jobs=tuple(required_jobs),
            config_json=os.environ.get("AI_REVIEW_CONFIG_JSON", ""),
        )
        inputs.validate()
        return inputs

    def validate(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository):
            raise ValueError("invalid repository review input")
        if self.pull_request_number <= 0 or self.workflow_run_id <= 0:
            raise ValueError("review identifiers must be positive")
        for value in (self.base_sha, self.head_sha, self.reviewed_sha):
            if not re.fullmatch(r"[0-9a-f]{40}", value):
                raise ValueError(
                    "commit review inputs must be full lowercase SHA values"
                )
        if self.reviewed_sha != self.head_sha:
            raise ValueError("reviewed commit must match the trusted head commit")
        if (
            not self.required_ci_jobs
            or len(self.required_ci_jobs) > 50
            or len(set(self.required_ci_jobs)) != len(self.required_ci_jobs)
            or any(not name.strip() or len(name) > 200 for name in self.required_ci_jobs)
        ):
            raise ValueError("required CI job configuration is invalid")

    def runner_config(self) -> RunnerConfig:
        if not self.config_json.strip():
            return RunnerConfig.load()
        try:
            payload = json.loads(self.config_json)
        except json.JSONDecodeError as exc:
            raise ValueError("reviewer configuration must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise TypeError("reviewer configuration must be a JSON object")
        return RunnerConfig.from_mapping(payload)


@dataclass(frozen=True)
class ExecutionOutputs:
    review_status: str
    comment_status: str
    reviewed_commit_sha: str


async def execute_review(
    inputs: ReviewInputs,
    github: GitHubClient,
    provider: ReviewProvider,
    config: RunnerConfig,
    logger: logging.Logger,
) -> ExecutionOutputs:
    metadata = await github.get_pull_request(
        inputs.repository, inputs.pull_request_number
    )
    fork = _validate_metadata(inputs, metadata)
    try:
        await _validate_workflow_authorization(inputs, metadata, github)
    except (GitHubApiError, ValueError):
        if not fork:
            raise
        action = await _safe_comment(
            github,
            inputs,
            render_safe_failure(
                inputs.reviewed_sha,
                "Fork review could not verify the trusted deterministic CI definition.",
                skipped=True,
            ),
            logger,
        )
        return ExecutionOutputs("fork_review_skipped", action, inputs.reviewed_sha)
    logger.info(
        "review metadata validated repository=%s pr=%d commit=%s workflow_run_id=%d fork=%s",
        inputs.repository,
        inputs.pull_request_number,
        inputs.reviewed_sha,
        inputs.workflow_run_id,
        fork,
    )
    try:
        diff = await github.get_pull_request_diff(
            inputs.repository, inputs.pull_request_number
        )
    except GitHubApiError:
        if fork:
            action = await _safe_comment(
                github,
                inputs,
                render_safe_failure(
                    inputs.reviewed_sha,
                    "Fork diff could not be retrieved safely through the GitHub API.",
                    skipped=True,
                ),
                logger,
            )
            return ExecutionOutputs("fork_review_skipped", action, inputs.reviewed_sha)
        raise
    if len(diff.encode("utf-8")) > MAX_DIFF_BYTES:
        action = await _safe_comment(
            github,
            inputs,
            render_safe_failure(
                inputs.reviewed_sha,
                "Pull Request diff exceeds the safe retrieval limit.",
                skipped=True,
            ),
            logger,
        )
        return ExecutionOutputs("skipped", action, inputs.reviewed_sha)

    progress_action = await _safe_comment(
        github, inputs, render_in_progress(inputs.reviewed_sha), logger
    )
    logger.info(
        "review started commit=%s comment_action=%s",
        inputs.reviewed_sha,
        progress_action,
    )
    result = await ReviewRunner(provider, config, logger=logger).run(
        diff, inputs.reviewed_sha
    )

    current = await github.get_pull_request(
        inputs.repository, inputs.pull_request_number
    )
    if current.get("state") != "open":
        logger.info("review publication skipped because Pull Request is no longer open")
        return ExecutionOutputs("skipped", progress_action, inputs.reviewed_sha)
    current_head = _nested(current, "head", "sha")
    if current_head != inputs.reviewed_sha:
        logger.info("stale review discarded commit=%s", inputs.reviewed_sha)
        return ExecutionOutputs("stale", progress_action, inputs.reviewed_sha)

    status = normalized_status(result)
    comment_action = await _safe_comment(
        github,
        inputs,
        render_result(result, inputs.reviewed_sha),
        logger,
        expected_head_sha=inputs.reviewed_sha,
    )
    if comment_action == "stale":
        logger.info(
            "stale review discarded before comment write commit=%s",
            inputs.reviewed_sha,
        )
        return ExecutionOutputs("stale", "not_attempted", inputs.reviewed_sha)
    if comment_action == "failed":
        return ExecutionOutputs(status, "comment_failure", inputs.reviewed_sha)
    logger.info(
        "review published status=%s files=%d chunks=%d skipped=%d comment_action=%s",
        status,
        len(result.file_statuses),
        len(result.processed_chunks),
        len(result.skipped),
        comment_action,
    )
    return ExecutionOutputs(status, comment_action, inputs.reviewed_sha)


async def _safe_comment(
    github: GitHubClient,
    inputs: ReviewInputs,
    body: str,
    logger: logging.Logger,
    *,
    expected_head_sha: str | None = None,
) -> str:
    try:
        return await github.upsert_marker_comment(
            inputs.repository,
            inputs.pull_request_number,
            COMMENT_MARKER,
            body,
            expected_head_sha=expected_head_sha,
        )
    except GitHubApiError as exc:
        if exc.category == "stale_review":
            return "stale"
        logger.warning("comment operation failed category=%s", exc.category)
        return "failed"


async def _validate_workflow_authorization(
    inputs: ReviewInputs, metadata: dict[str, Any], github: GitHubClient
) -> None:
    run = await github.get_workflow_run(inputs.repository, inputs.workflow_run_id)
    associated = run.get("pull_requests")
    if (
        run.get("id") != inputs.workflow_run_id
        or run.get("event") != "pull_request"
        or run.get("conclusion") != "success"
        or run.get("path") != ".github/workflows/ci.yml"
        or not isinstance(associated, list)
        or len(associated) != 1
        or not isinstance(associated[0], dict)
        or associated[0].get("number") != inputs.pull_request_number
    ):
        raise ValueError("workflow run is not authorized for this Pull Request review")

    jobs = await github.list_workflow_run_jobs(inputs.repository, inputs.workflow_run_id)
    for required_name in inputs.required_ci_jobs:
        matching = [job for job in jobs if job.get("name") == required_name]
        if len(matching) != 1 or matching[0].get("conclusion") != "success":
            raise ValueError(
                "a required deterministic CI job did not complete successfully"
            )

    head_repository = _nested(metadata, "head", "repo", "full_name")
    base_definition, head_definition = await asyncio.gather(
        github.get_repository_file(
            inputs.repository, ".github/workflows/ci.yml", inputs.base_sha
        ),
        github.get_repository_file(
            head_repository, ".github/workflows/ci.yml", inputs.head_sha
        ),
    )
    if base_definition != head_definition:
        raise ValueError("Pull Request changes the trusted deterministic CI definition")


def _validate_metadata(inputs: ReviewInputs, metadata: dict[str, Any]) -> bool:
    if metadata.get("number") != inputs.pull_request_number:
        raise ValueError("Pull Request number does not match trusted metadata")
    if metadata.get("state") != "open":
        raise ValueError("Pull Request is not open")
    base_repo = _nested(metadata, "base", "repo", "full_name")
    head_repo = _nested(metadata, "head", "repo", "full_name")
    if base_repo != inputs.repository:
        raise ValueError("Pull Request repository does not match trusted metadata")
    if _nested(metadata, "base", "sha") != inputs.base_sha:
        raise ValueError("Pull Request base commit changed after deterministic CI")
    if _nested(metadata, "head", "sha") != inputs.head_sha:
        raise ValueError("Pull Request head commit changed after deterministic CI")
    if not isinstance(head_repo, str):
        raise TypeError("Pull Request head repository metadata is unavailable")
    return head_repo != inputs.repository


def _nested(value: dict[str, Any], *keys: str) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _write_outputs(outputs: ExecutionOutputs) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    lines = [
        f"review_status={outputs.review_status}",
        f"comment_status={outputs.comment_status}",
        f"reviewed_commit_sha={outputs.reviewed_commit_sha}",
    ]
    if path:
        with Path(path).open("a", encoding="utf-8") as output_file:
            output_file.write("\n".join(lines) + "\n")
    else:
        print("\n".join(lines))


async def _async_main(logger: logging.Logger) -> ExecutionOutputs:
    inputs = ReviewInputs.from_environment()
    config = inputs.runner_config()
    github = GitHubClient(os.environ.get("GITHUB_TOKEN", ""), logger=logger)
    provider: ReviewProvider | None = None
    try:
        provider = create_provider("openrouter")
        return await execute_review(inputs, github, provider, config, logger)
    except GitHubApiError as exc:
        logger.warning("AI review failed safely category=%s", exc.category)
        return ExecutionOutputs("failed_safely", "comment_failure", inputs.reviewed_sha)
    except (ValueError, OSError) as exc:
        logger.warning("AI review failed safely category=%s", type(exc).__name__)
        return ExecutionOutputs("failed_safely", "not_attempted", inputs.reviewed_sha)
    except Exception as exc:  # noqa: BLE001
        logger.error("AI review failed safely category=%s", type(exc).__name__)
        try:
            action = await _safe_comment(
                github,
                inputs,
                render_safe_failure(inputs.reviewed_sha, "Unexpected reviewer error."),
                logger,
            )
        except Exception:  # noqa: BLE001
            action = "failed"
        return ExecutionOutputs("failed_safely", action, inputs.reviewed_sha)
    finally:
        if provider is not None:
            close = getattr(provider, "aclose", None)
            if close:
                await close()
        await github.aclose()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    logger = logging.getLogger("review-runner.github")
    try:
        outputs = asyncio.run(_async_main(logger))
    except Exception as exc:  # noqa: BLE001 - inputs may fail before a trusted PR is identified.
        logger.error(
            "AI review input validation failed safely category=%s", type(exc).__name__
        )
        outputs = ExecutionOutputs("failed_safely", "not_attempted", "")
    _write_outputs(outputs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
