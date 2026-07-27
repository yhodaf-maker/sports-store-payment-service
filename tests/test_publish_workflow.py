import os
import re

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
