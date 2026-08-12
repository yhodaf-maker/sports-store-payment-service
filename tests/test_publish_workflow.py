import os
import re

import pytest

# Resolve path to publish.yml
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW_PATH = os.path.join(BASE_DIR, ".github", "workflows", "publish.yml")

def test_workflow_exists():
    assert os.path.exists(WORKFLOW_PATH), "publish.yml workflow file is missing"

def test_workflow_trigger_and_permissions():
    with open(WORKFLOW_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    # Trigger branch check (push to main)
    assert re.search(r'push:\s*\n\s*branches:\s*\n\s*-\s*main', content) is not None, "Workflow must trigger on pushes to main branch"

    # OIDC Permissions check (id-token: write, contents: read)
    assert re.search(r'id-token:\s*write', content) is not None, "Workflow must request id-token write permissions for OIDC"
    assert re.search(r'contents:\s*read', content) is not None, "Workflow must request contents read permissions"

def test_oidc_actions_used():
    with open(WORKFLOW_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    # AWS OIDC action check
    assert "aws-actions/configure-aws-credentials" in content, "Workflow must authenticate via aws-actions/configure-aws-credentials"
    assert "role-to-assume" in content, "Workflow must specify role-to-assume input"

    # ECR Login action check
    assert "aws-actions/amazon-ecr-login" in content, "Workflow must log in to ECR via aws-actions/amazon-ecr-login"

def test_no_latest_tag_used():
    with open(WORKFLOW_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    # Ensure ':latest' tag is not declared or referenced
    assert ":latest" not in content, "The usage of ':latest' image tag is strictly prohibited"

def test_no_deployment_commands_used():
    with open(WORKFLOW_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    # Ensure cluster isolation (no kubectl or helm)
    assert "kubectl" not in content, "Workflow must maintain isolation and not use kubectl"
    assert "helm" not in content, "Workflow must maintain isolation and not use helm"


def _assert_writeback_contract(content):
    image_file = "environments/production/images/payment-service.yaml"
    push_index = content.index("docker push")
    token_index = content.index("actions/create-github-app-token@v3")
    yq_index = content.index("mikefarah/yq@v4.53.2")
    commit_index = content.index("git commit -m")

    assert push_index < token_index < yq_index < commit_index
    assert "client-id: ${{ vars.GITOPS_CLIENT_ID }}" in content
    assert "private-key: ${{ secrets.GITOPS_APP_PRIVATE_KEY }}" in content
    assert "repositories: sports-store-deployments" in content
    assert "permission-contents: write" in content
    assert "repository: yhodaf-maker/sports-store-deployments" in content
    assert "ref: main" in content
    assert "token: ${{ steps.gitops-token.outputs.token }}" in content
    assert f".image.tag = strenv(IMAGE_TAG)' 'sports-store-deployments/{image_file}'" in content
    assert "IMAGE_TAG: ${{ steps.get_tag.outputs.tag }}" in content
    assert f"IMAGE_FILE: {image_file}" in content
    assert 'git diff --quiet -- "$IMAGE_FILE"' in content
    assert "sports-store-gitops-bot[bot]" in content
    assert 'if [ "$APP_SLUG" != "sports-store-gitops-bot" ]' in content
    assert "BOT_ID=$(gh api '/users/sports-store-gitops-bot%5Bbot%5D' --jq '.id')" in content
    assert "[skip ci]" in content
    assert "git push origin HEAD:main" in content
    assert "for attempt in 1 2 3" in content
    assert re.search(r"\b(sed|awk|kubectl|helm)\b", content) is None


def test_gitops_image_tag_writeback_contract():
    with open(WORKFLOW_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    _assert_writeback_contract(content)
    assert re.search(
        r"concurrency:\s*\n\s*group:\s*production-image-writeback\s*\n"
        r"\s*cancel-in-progress:\s*false",
        content,
    )


@pytest.mark.parametrize(
    "required_fragment",
    [
        "actions/create-github-app-token@v3",
        ".image.tag = strenv(IMAGE_TAG)",
        'git diff --quiet -- "$IMAGE_FILE"',
        "[skip ci]",
        "git push origin HEAD:main",
    ],
)
def test_writeback_contract_fails_if_critical_behavior_is_removed(required_fragment):
    with open(WORKFLOW_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    assert required_fragment in content
    with pytest.raises((AssertionError, ValueError)):
        _assert_writeback_contract(content.replace(required_fragment, "", 1))
