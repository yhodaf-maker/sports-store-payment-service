import logging

import pytest

from review_runner.config import RunnerConfig
from review_runner.github_client import GitHubApiError
from review_runner.github_review import ReviewInputs, execute_review, main
from review_runner.mock_provider import MockReviewProvider

SHA_BASE = "a" * 40
SHA_HEAD = "b" * 40
REQUIRED_JOBS = ("Quality", "Security")
PATCH = (
    "diff --git a/app.py b/app.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1 +1 @@\n"
    "-old\n"
    "+new"
)


def inputs():
    return ReviewInputs(
        "owner/repo", 7, SHA_BASE, SHA_HEAD, SHA_HEAD, 99, REQUIRED_JOBS
    )


def metadata(head=SHA_HEAD, head_repository="owner/repo"):
    return {
        "number": 7,
        "state": "open",
        "base": {"sha": SHA_BASE, "repo": {"full_name": "owner/repo"}},
        "head": {"sha": head, "repo": {"full_name": head_repository}},
    }


class FakeGitHub:
    def __init__(
        self,
        metadata_values=None,
        diff=PATCH,
        diff_error=None,
        comment_error=None,
        workflow_definition="trusted-ci",
        head_workflow_definition="trusted-ci",
        jobs=None,
    ):
        self.metadata_values = list(metadata_values or [metadata(), metadata()])
        self.diff = diff
        self.diff_error = diff_error
        self.comment_error = comment_error
        self.workflow_definition = workflow_definition
        self.head_workflow_definition = head_workflow_definition
        self.jobs = jobs or [
            {"name": name, "conclusion": "success"} for name in REQUIRED_JOBS
        ]
        self.comments = []

    async def get_pull_request(self, repository, number):
        return self.metadata_values.pop(0)

    async def get_pull_request_diff(self, repository, number):
        if self.diff_error:
            raise self.diff_error
        return self.diff

    async def get_workflow_run(self, repository, run_id):
        return {
            "id": 99,
            "event": "pull_request",
            "conclusion": "success",
            "path": ".github/workflows/ci.yml",
            "pull_requests": [{"number": 7}],
        }

    async def list_workflow_run_jobs(self, repository, run_id):
        return self.jobs

    async def get_repository_file(self, repository, path, ref):
        return (
            self.workflow_definition
            if repository == "owner/repo" and ref == SHA_BASE
            else self.head_workflow_definition
        )

    async def upsert_marker_comment(
        self, repository, number, marker, body, *, expected_head_sha=None
    ):
        if self.comment_error:
            raise self.comment_error
        self.comments.append(body)
        return "created" if len(self.comments) == 1 else "updated"


@pytest.mark.asyncio
async def test_passing_ci_metadata_runs_review_and_updates_one_marker_comment():
    github = FakeGitHub()
    output = await execute_review(
        inputs(),
        github,
        MockReviewProvider("findings"),
        RunnerConfig(),
        logging.getLogger("test"),
    )
    assert output.review_status == "completed"
    assert output.comment_status == "updated"
    assert len(github.comments) == 2
    assert "In Progress" in github.comments[0]
    assert "Completed" in github.comments[1]
    assert github.comments[1].count("<!-- sports-store-ai-review:v1 -->") == 1
    assert SHA_HEAD in github.comments[1]


@pytest.mark.asyncio
async def test_successful_review_with_no_findings_has_distinct_status():
    output = await execute_review(
        inputs(),
        FakeGitHub(),
        MockReviewProvider("no_findings"),
        RunnerConfig(),
        logging.getLogger("test"),
    )
    assert output.review_status == "completed_no_findings"


@pytest.mark.asyncio
async def test_stale_result_is_discarded_without_final_comment():
    github = FakeGitHub([metadata(), metadata(head="c" * 40)])
    output = await execute_review(
        inputs(),
        github,
        MockReviewProvider("findings"),
        RunnerConfig(),
        logging.getLogger("test"),
    )
    assert output.review_status == "stale"
    assert len(github.comments) == 1
    assert "In Progress" in github.comments[0]


@pytest.mark.asyncio
async def test_fork_diff_is_reviewed_as_api_data_only():
    github = FakeGitHub(
        [metadata(head_repository="fork/repo"), metadata(head_repository="fork/repo")]
    )
    output = await execute_review(
        inputs(),
        github,
        MockReviewProvider("no_findings"),
        RunnerConfig(),
        logging.getLogger("test"),
    )
    assert output.review_status == "completed_no_findings"


@pytest.mark.asyncio
async def test_fork_is_skipped_when_api_diff_cannot_be_retrieved():
    github = FakeGitHub(
        [metadata(head_repository="fork/repo")],
        diff_error=GitHubApiError("github_permission_denied", 403),
    )
    output = await execute_review(
        inputs(),
        github,
        MockReviewProvider("findings"),
        RunnerConfig(),
        logging.getLogger("test"),
    )
    assert output.review_status == "fork_review_skipped"
    assert "Fork diff could not be retrieved safely" in github.comments[0]


@pytest.mark.asyncio
async def test_changed_trusted_metadata_prevents_provider_and_comment_use():
    class CountingProvider(MockReviewProvider):
        def __init__(self):
            super().__init__("findings")
            self.calls = 0

        async def review(self, chunk):
            self.calls += 1
            return await super().review(chunk)

    provider = CountingProvider()
    github = FakeGitHub([metadata(head="c" * 40)])
    with pytest.raises(ValueError, match="head commit changed"):
        await execute_review(
            inputs(), github, provider, RunnerConfig(), logging.getLogger("test")
        )
    assert provider.calls == 0
    assert github.comments == []


@pytest.mark.asyncio
async def test_changed_ci_definition_prevents_provider_and_comment_use():
    class CountingProvider(MockReviewProvider):
        def __init__(self):
            super().__init__("findings")
            self.calls = 0

        async def review(self, chunk):
            self.calls += 1
            return await super().review(chunk)

    provider = CountingProvider()
    github = FakeGitHub(head_workflow_definition="pr-controlled-ci")
    with pytest.raises(ValueError, match="changes the trusted deterministic CI"):
        await execute_review(
            inputs(), github, provider, RunnerConfig(), logging.getLogger("test")
        )
    assert provider.calls == 0
    assert github.comments == []


@pytest.mark.asyncio
async def test_skipped_required_job_prevents_provider_use():
    jobs = [
        {"name": "Quality", "conclusion": "success"},
        {"name": "Security", "conclusion": "skipped"},
    ]
    with pytest.raises(ValueError, match="required deterministic CI job"):
        await execute_review(
            inputs(),
            FakeGitHub(jobs=jobs),
            MockReviewProvider("findings"),
            RunnerConfig(),
            logging.getLogger("test"),
        )


@pytest.mark.asyncio
async def test_provider_failure_is_advisory_and_published_as_skipped():
    github = FakeGitHub()
    output = await execute_review(
        inputs(),
        github,
        MockReviewProvider("error"),
        RunnerConfig(),
        logging.getLogger("test"),
    )
    assert output.review_status == "provider_failure"
    assert "Skipped" in github.comments[-1]


@pytest.mark.asyncio
async def test_comment_api_failure_does_not_abort_review():
    output = await execute_review(
        inputs(),
        FakeGitHub(comment_error=GitHubApiError("github_permission_denied", 403)),
        MockReviewProvider("no_findings"),
        RunnerConfig(),
        logging.getLogger("test"),
    )
    assert output.review_status == "completed_no_findings"
    assert output.comment_status == "comment_failure"


@pytest.mark.asyncio
async def test_closed_pull_request_is_not_updated_with_final_result():
    closed = metadata()
    closed["state"] = "closed"
    github = FakeGitHub([metadata(), closed])
    output = await execute_review(
        inputs(),
        github,
        MockReviewProvider("findings"),
        RunnerConfig(),
        logging.getLogger("test"),
    )
    assert output.review_status == "skipped"
    assert len(github.comments) == 1


def test_inputs_reject_branch_controlled_or_mismatched_metadata():
    with pytest.raises(ValueError, match="repository"):
        ReviewInputs("$(malicious)", 7, SHA_BASE, SHA_HEAD, SHA_HEAD, 99).validate()
    with pytest.raises(ValueError, match="reviewed commit"):
        ReviewInputs("owner/repo", 7, SHA_BASE, SHA_HEAD, "c" * 40, 99).validate()


def test_invalid_inputs_fail_open_at_process_boundary(monkeypatch, tmp_path):
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.delenv("AI_REVIEW_PR_NUMBER", raising=False)
    assert main() == 0
    assert "review_status=failed_safely" in output.read_text(encoding="utf-8")
