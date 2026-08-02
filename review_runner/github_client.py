from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .logging_utils import get_logger

Sleep = Callable[[float], Awaitable[None]]


class GitHubApiError(RuntimeError):
    def __init__(self, category: str, status_code: int | None = None):
        super().__init__(category)
        self.category = category
        self.status_code = status_code


class GitHubClient:
    def __init__(
        self,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        logger: logging.Logger | None = None,
        sleep: Sleep = asyncio.sleep,
        max_retries: int = 2,
    ):
        if not token:
            raise ValueError("GitHub token is required")
        self.logger = logger or get_logger("INFO")
        self._sleep = sleep
        self._max_retries = max_retries
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url="https://api.github.com/",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=httpx.Timeout(30, connect=10),
        )
        self.retry_count = 0

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_pull_request(self, repository: str, number: int) -> dict[str, Any]:
        response = await self._request("GET", f"repos/{repository}/pulls/{number}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise GitHubApiError("invalid_pull_request_metadata")
        return payload

    async def get_pull_request_diff(self, repository: str, number: int) -> str:
        response = await self._request(
            "GET",
            f"repos/{repository}/pulls/{number}",
            headers={"Accept": "application/vnd.github.v3.diff"},
        )
        return response.text

    async def get_workflow_run(self, repository: str, run_id: int) -> dict[str, Any]:
        response = await self._request("GET", f"repos/{repository}/actions/runs/{run_id}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise GitHubApiError("invalid_workflow_run_metadata")
        return payload

    async def list_workflow_run_jobs(
        self, repository: str, run_id: int
    ) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        page = 1
        while True:
            response = await self._request(
                "GET",
                f"repos/{repository}/actions/runs/{run_id}/jobs?filter=latest&per_page=100&page={page}",
            )
            payload = response.json()
            page_jobs = payload.get("jobs") if isinstance(payload, dict) else None
            if not isinstance(page_jobs, list):
                raise GitHubApiError("invalid_workflow_job_metadata")
            jobs.extend(item for item in page_jobs if isinstance(item, dict))
            if len(page_jobs) < 100:
                return jobs
            page += 1

    async def get_repository_file(self, repository: str, path: str, ref: str) -> str:
        response = await self._request(
            "GET",
            f"repos/{repository}/contents/{path}?ref={ref}",
            headers={"Accept": "application/vnd.github.raw+json"},
        )
        return response.text

    async def list_issue_comments(
        self, repository: str, number: int
    ) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []
        page = 1
        while True:
            response = await self._request(
                "GET",
                f"repos/{repository}/issues/{number}/comments?per_page=100&page={page}",
            )
            payload = response.json()
            if not isinstance(payload, list):
                raise GitHubApiError("invalid_comment_metadata")
            comments.extend(item for item in payload if isinstance(item, dict))
            if len(payload) < 100:
                return comments
            page += 1

    async def create_issue_comment(
        self, repository: str, number: int, body: str
    ) -> int:
        response = await self._request(
            "POST", f"repos/{repository}/issues/{number}/comments", json={"body": body}
        )
        return _comment_id(response)

    async def update_issue_comment(
        self, repository: str, comment_id: int, body: str
    ) -> None:
        await self._request(
            "PATCH",
            f"repos/{repository}/issues/comments/{comment_id}",
            json={"body": body},
        )

    async def upsert_marker_comment(
        self,
        repository: str,
        number: int,
        marker: str,
        body: str,
        *,
        expected_head_sha: str | None = None,
    ) -> str:
        matches = _matching_comments(
            await self.list_issue_comments(repository, number), marker
        )
        if len(matches) > 1:
            self.logger.warning(
                "duplicate review markers detected count=%d canonical_comment_id=%d",
                len(matches),
                matches[0]["id"],
            )
        if matches:
            try:
                await self._require_head(repository, number, expected_head_sha)
                await self.update_issue_comment(repository, matches[0]["id"], body)
                return "updated"
            except GitHubApiError as exc:
                if exc.status_code != 404:
                    raise
                # The comment may have been deleted after lookup. Re-list before creating.
                matches = _matching_comments(
                    await self.list_issue_comments(repository, number), marker
                )
                if matches:
                    await self._require_head(repository, number, expected_head_sha)
                    await self.update_issue_comment(repository, matches[0]["id"], body)
                    return "updated"
        await self._require_head(repository, number, expected_head_sha)
        await self.create_issue_comment(repository, number, body)
        return "created"

    async def _require_head(
        self, repository: str, number: int, expected_head_sha: str | None
    ) -> None:
        if expected_head_sha is None:
            return
        pull_request = await self.get_pull_request(repository, number)
        head = pull_request.get("head")
        if not isinstance(head, dict) or head.get("sha") != expected_head_sha:
            raise GitHubApiError("stale_review")

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        attempt = 0
        while True:
            response: httpx.Response | None = None
            try:
                response = await self._client.request(method, url, **kwargs)
            except httpx.TimeoutException:
                category = "github_timeout"
            except httpx.TransportError:
                category = "github_unavailable"
            else:
                if response.is_success:
                    return response
                category = _error_category(response)
                if not _retryable(response):
                    raise GitHubApiError(category, response.status_code)
            if attempt >= self._max_retries:
                raise GitHubApiError(
                    category, response.status_code if response is not None else None
                )
            delay = _retry_delay(response, attempt)
            attempt += 1
            self.retry_count += 1
            self.logger.warning(
                "GitHub API retry category=%s attempt=%d", category, attempt
            )
            await self._sleep(delay)


def _matching_comments(
    comments: list[dict[str, Any]], marker: str
) -> list[dict[str, Any]]:
    matches = []
    for comment in comments:
        body = comment.get("body")
        user = comment.get("user")
        login = user.get("login", "") if isinstance(user, dict) else ""
        if (
            isinstance(body, str)
            and marker in body.splitlines()
            and isinstance(comment.get("id"), int)
            and login == "github-actions[bot]"
        ):
            matches.append(comment)
    return sorted(matches, key=lambda item: item["id"])


def _comment_id(response: httpx.Response) -> int:
    try:
        comment_id = response.json()["id"]
    except (ValueError, KeyError, TypeError) as exc:
        raise GitHubApiError("invalid_comment_metadata") from exc
    if not isinstance(comment_id, int):
        raise GitHubApiError("invalid_comment_metadata")
    return comment_id


def _retryable(response: httpx.Response) -> bool:
    return response.status_code in {408, 429, 500, 502, 503, 504} or (
        response.status_code == 403
        and (
            response.headers.get("X-RateLimit-Remaining") == "0"
            or "Retry-After" in response.headers
        )
    )


def _error_category(response: httpx.Response) -> str:
    if response.status_code in {401, 403} and not _retryable(response):
        return "github_permission_denied"
    if response.status_code == 404:
        return "github_resource_not_found"
    if response.status_code == 410:
        return "pull_request_closed"
    if response.status_code == 429 or _retryable(response):
        return (
            "github_rate_limited"
            if response.status_code in {403, 429}
            else "github_unavailable"
        )
    return "github_api_error"


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    if response is not None:
        raw = response.headers.get("Retry-After")
        if raw:
            try:
                return min(max(float(raw), 0), 30)
            except ValueError:
                pass
        reset = response.headers.get("X-RateLimit-Reset")
        if reset:
            try:
                return min(max(float(reset) - time.time(), 0), 30)
            except ValueError:
                pass
    return min(2**attempt, 8)
