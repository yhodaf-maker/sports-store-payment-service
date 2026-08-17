from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
ORCHESTRATOR = (ROOT / ".github/workflows/ai-review-after-ci.yml").read_text(
    encoding="utf-8"
)
REUSABLE = (ROOT / ".github/workflows/reusable-ai-review.yml").read_text(
    encoding="utf-8"
)


def test_all_repository_pr_checks_are_in_authoritative_ci_workflow():
    for required in (
        "Branch name validation",
        "Python Quality and Tests",
        "Secret Scanning",
        "Dockerfile Policy",
        "Container Vulnerabilities",
    ):
        assert required in CI
    assert "pull_request:" in CI
    assert "OPENROUTER" not in CI
    assert "ai-review" not in CI.casefold()
    assert not (ROOT / ".github/workflows/branch-name.yml").exists()


def test_orchestrator_only_invokes_review_after_successful_pr_ci():
    assert 'workflows: ["PR Quality and Security Validation"]' in ORCHESTRATOR
    assert "types: [completed]" in ORCHESTRATOR
    assert "github.event.workflow_run.conclusion == 'success'" in ORCHESTRATOR
    assert "github.event.workflow_run.event == 'pull_request'" in ORCHESTRATOR
    assert "github.event.workflow_run.pull_requests[1].number == null" in ORCHESTRATOR
    assert "always()" not in ORCHESTRATOR
    assert "always()" not in REUSABLE
    assert "uses: ./.github/workflows/reusable-ai-review.yml" in ORCHESTRATOR


def test_only_reviewer_secret_is_passed():
    assert "openrouter_api_key: ${{ secrets.OPENROUTER_API_KEY }}" in ORCHESTRATOR
    assert "secrets: inherit" not in ORCHESTRATOR
    assert "secrets: inherit" not in REUSABLE
    for unrelated in ("AWS_", "DATABASE", "ECR_", "PACKAGE_TOKEN"):
        assert unrelated not in ORCHESTRATOR
        assert unrelated not in REUSABLE


def test_reusable_workflow_is_least_privilege_and_data_only():
    assert "actions: read" in REUSABLE
    assert "contents: read" in REUSABLE
    assert "pull-requests: write" in REUSABLE
    for forbidden in (
        "id-token: write",
        "contents: write",
        "packages: write",
        "security-events: write",
    ):
        assert forbidden not in REUSABLE
    assert "ref: ${{ inputs.base_commit_sha }}" in REUSABLE
    assert "persist-credentials: false" in REUSABLE
    assert "github.event.pull_request.head" not in REUSABLE
    assert "docker build" not in REUSABLE
    assert "pytest" not in REUSABLE


def test_reusable_workflow_has_pr_concurrency_and_advisory_outputs():
    assert (
        "group: ai-review-${{ inputs.repository }}-${{ inputs.pull_request_number }}"
        in REUSABLE
    )
    assert "cancel-in-progress: true" in REUSABLE
    for output in ("review_status", "comment_status", "reviewed_commit_sha"):
        assert output in REUSABLE
    assert "continue-on-error: true" in REUSABLE
    assert "AI_REVIEW_REQUIRED_JOBS_JSON" in REUSABLE


def test_failed_ci_path_cannot_consume_openrouter_quota():
    success_gate = ORCHESTRATOR.index(
        "github.event.workflow_run.conclusion == 'success'"
    )
    secret_pass = ORCHESTRATOR.index("secrets.OPENROUTER_API_KEY")
    assert success_gate < secret_pass
    assert "OPENROUTER_API_KEY" not in CI


def test_semgrep_sast_is_pinned_report_only_and_least_privilege():
    assert "sast:" in CI
    assert "name: Semgrep SAST (report only)" in CI
    assert (
        "semgrep/semgrep@sha256:"
        "65dcd4408adda7c183a6b4550cb1e9b19f7f627a6fbb7e0559bd466bedc44d7b"
        in CI
    )
    assert "SEMGREP_VERSION: 1.172.0" in CI
    assert "semgrep scan --config auto ." in CI
    assert "persist-credentials: false" in CI

    sast = CI.split("\n  sast:", maxsplit=1)[1]
    assert "contents: read" in sast
    assert "--error" not in sast
    assert "continue-on-error" not in sast
    assert "secrets." not in sast
    for forbidden in (
        "id-token: write",
        "contents: write",
        "pull-requests: write",
        "security-events: write",
    ):
        assert forbidden not in sast

    assert '"Semgrep SAST (report only)"' in ORCHESTRATOR


def test_dependency_sca_is_pinned_report_only_and_least_privilege():
    assert "sca:" in CI
    assert "name: Dependency SCA (report only)" in CI
    assert "PIP_AUDIT_VERSION: 2.10.1" in CI
    assert "python:3.11.15-slim-bookworm@sha256:" in CI
    assert "python -m pip_audit --requirement" in CI
    assert "persist-credentials: false" in CI
    assert '"Dependency SCA (report only)"' in ORCHESTRATOR
    sca = CI.split("\n  sca:", maxsplit=1)[1]
    assert "contents: read" in sca
    assert "Vulnerabilities are report-only" in sca
    assert "invalid pip-audit report" in sca
    assert "continue-on-error" not in sca
    assert "|| true" not in sca
    assert "secrets." not in sca
    for forbidden in ("id-token: write", "contents: write", "pull-requests: write", "security-events: write"):
        assert forbidden not in sca
