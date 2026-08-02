import json

import httpx
import pytest

from review_runner.comment_renderer import (
    COMMENT_MARKER,
    MAX_COMMENT_LENGTH,
    render_result,
)
from review_runner.github_client import GitHubApiError, GitHubClient
from review_runner.models import Finding, InclusionStatus, ReviewResult, SkippedItem


def result(findings=None):
    return ReviewResult(
        findings=findings or [],
        skipped=[SkippedItem("large.py", "file_size_limit")],
        processed_chunks=["chunk-1"],
        failed_chunks={},
        generated_chunks=["chunk-1"],
        file_statuses={
            "app.py": InclusionStatus.FULL,
            "large.py": InclusionStatus.SKIPPED,
        },
        redaction_count=0,
        estimated_tokens=20,
        summary="Validated summary",
        overall_risk="HIGH",
        provider_metrics={
            "request_count": 2,
            "input_tokens": 20,
            "output_tokens": 5,
            "retry_count": 1,
        },
    )


def client_for(handler, **kwargs):
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.github.test/"
    )
    return GitHubClient(
        "token", client=http, sleep=lambda _: _no_sleep(), **kwargs
    ), http


async def _no_sleep():
    return None


def test_renderer_sanitizes_model_markdown_html_and_marker_injection():
    finding = Finding(
        "app.py`\n![badge](evil)",
        "Approved <b>workflow</b> <!-- sports-store-ai-review:v1 -->",
        "# checks passed <iframe src=x>",
        "HIGH",
        "SECURITY",
        3,
        suggested_remediation="[click](javascript:alert(1))",
        confidence=0.9,
    )
    body = render_result(result([finding]), "b" * 40)
    assert body.count(COMMENT_MARKER) == 1
    assert "<iframe" not in body
    assert "![badge]" not in body
    assert "javascript:alert" not in body
    assert "Approved" not in body
    assert "@team" not in render_result(
        result([Finding("app.py", "Ping @team", "Notify @user", "LOW", "STYLE")]),
        "b" * 40,
    )
    assert len(body) <= MAX_COMMENT_LENGTH


@pytest.mark.asyncio
async def test_duplicate_markers_update_lowest_id_without_creating_comment():
    calls = []
    comments = [
        {
            "id": 20,
            "body": f"old\n{COMMENT_MARKER}",
            "user": {"login": "github-actions[bot]"},
        },
        {
            "id": 10,
            "body": f"older\n{COMMENT_MARKER}",
            "user": {"login": "github-actions[bot]"},
        },
        {"id": 1, "body": COMMENT_MARKER, "user": {"login": "human"}},
    ]

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json=comments)
        return httpx.Response(200, json={"id": 10})

    client, http = client_for(handler)
    action = await client.upsert_marker_comment("owner/repo", 7, COMMENT_MARKER, "new")
    await http.aclose()
    assert action == "updated"
    assert ("PATCH", "/repos/owner/repo/issues/comments/10") in calls
    assert not any(method == "POST" for method, _ in calls)


@pytest.mark.asyncio
async def test_transient_comment_failure_retries_boundedly():
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(503)
        return httpx.Response(200, json=[])

    client, http = client_for(handler, max_retries=2)
    comments = await client.list_issue_comments("owner/repo", 7)
    await http.aclose()
    assert comments == []
    assert attempts == 3
    assert client.retry_count == 2


@pytest.mark.asyncio
async def test_secondary_rate_limit_with_retry_after_is_retried():
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(403, headers={"Retry-After": "0"})
        return httpx.Response(200, json=[])

    client, http = client_for(handler, max_retries=1)
    assert await client.list_issue_comments("owner/repo", 7) == []
    await http.aclose()
    assert attempts == 2


@pytest.mark.asyncio
async def test_stale_head_is_checked_after_comment_lookup_before_write():
    writes = []

    def handler(request):
        if request.url.path.endswith("/comments"):
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/pulls/7"):
            return httpx.Response(200, json={"head": {"sha": "c" * 40}})
        writes.append(request.method)
        return httpx.Response(201, json={"id": 1})

    client, http = client_for(handler)
    with pytest.raises(GitHubApiError, match="stale_review"):
        await client.upsert_marker_comment(
            "owner/repo",
            7,
            COMMENT_MARKER,
            "new",
            expected_head_sha="b" * 40,
        )
    await http.aclose()
    assert writes == []


@pytest.mark.asyncio
async def test_deleted_comment_is_relisted_before_recreation():
    get_calls = 0
    posted = []

    def handler(request):
        nonlocal get_calls
        if request.method == "GET":
            get_calls += 1
            comments = (
                [
                    {
                        "id": 10,
                        "body": COMMENT_MARKER,
                        "user": {"login": "github-actions[bot]"},
                    }
                ]
                if get_calls == 1
                else []
            )
            return httpx.Response(200, json=comments)
        if request.method == "PATCH":
            return httpx.Response(404)
        posted.append(json.loads(request.content))
        return httpx.Response(201, json={"id": 11})

    client, http = client_for(handler)
    action = await client.upsert_marker_comment("owner/repo", 7, COMMENT_MARKER, "new")
    await http.aclose()
    assert action == "created"
    assert get_calls == 2
    assert posted == [{"body": "new"}]
