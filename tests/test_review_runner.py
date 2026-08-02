import logging
from dataclasses import replace

import pytest

from review_runner.aggregation import aggregate_results
from review_runner.chunking import create_chunks
from review_runner.config import RunnerConfig
from review_runner.diff_parser import parse_diff
from review_runner.filtering import filter_files
from review_runner.mock_provider import MockReviewProvider
from review_runner.models import (
    ChangeType,
    ChunkResult,
    Finding,
    InclusionStatus,
    ProviderResult,
    ReviewChunk,
)
from review_runner.redaction import redact_files
from review_runner.runner import ReviewRunner
from review_runner.tokens import available_diff_budget


class CharacterEstimator:
    def estimate(self, text):
        return len(text)


def config(**overrides):
    values = {
        "max_file_bytes": 100_000,
        "max_file_lines": 10_000,
        "max_files": 100,
        "max_total_pr_tokens": 100_000,
        "max_chunk_input_tokens": 10_000,
        "max_chunks": 20,
        "model_context_tokens": 10_000,
        "reserved_instruction_tokens": 0,
        "reserved_schema_tokens": 0,
        "reserved_metadata_tokens": 0,
        "reserved_output_tokens": 0,
        "safety_margin_tokens": 1,
    }
    values.update(overrides)
    return RunnerConfig(**values)


def modified(path="app.py", hunks=None):
    if hunks is None:
        hunks = ["@@ -1 +1 @@\n-old\n+new"]
    return "\n".join([
        f"diff --git a/{path} b/{path}",
        "index 1111111..2222222 100644",
        f"--- a/{path}",
        f"+++ b/{path}",
        *hunks,
    ])


def added(path="new.py"):
    return "\n".join([
        f"diff --git a/{path} b/{path}",
        "new file mode 100644",
        "--- /dev/null",
        f"+++ b/{path}",
        "@@ -0,0 +1 @@",
        "+new",
    ])


def deleted(path="old.py"):
    return "\n".join([
        f"diff --git a/{path} b/{path}",
        "deleted file mode 100644",
        f"--- a/{path}",
        "+++ /dev/null",
        "@@ -1 +0,0 @@",
        "-old",
    ])


def renamed(old="before.py", new="after.py"):
    return "\n".join([
        f"diff --git a/{old} b/{new}",
        "similarity index 100%",
        f"rename from {old}",
        f"rename to {new}",
    ])


def test_empty_diff_produces_no_work():
    parsed = parse_diff(" \n")
    assert parsed.files == []
    assert parsed.skipped == []


def test_missing_final_newline_marker_is_preserved():
    patch = modified(hunks=[
        "@@ -1 +1 @@\n-old\n+new\n\\ No newline at end of file"
    ])
    file = parse_diff(patch).files[0]
    assert file.hunks[0].lines[-1].kind == "marker"
    assert file.hunks[0].lines[-1].text == r"\ No newline at end of file"


def test_small_modified_file_and_multiple_files_are_parsed():
    parsed = parse_diff(modified("a.py") + "\n" + modified("b.js"))
    assert [(item.path, item.change_type) for item in parsed.files] == [
        ("a.py", ChangeType.MODIFIED),
        ("b.js", ChangeType.MODIFIED),
    ]
    assert (parsed.files[0].added_lines, parsed.files[0].removed_lines) == (1, 1)


def test_added_deleted_and_renamed_files_are_classified():
    files = parse_diff(added() + "\n" + deleted() + "\n" + renamed()).files
    assert [(item.path, item.previous_path, item.change_type) for item in files] == [
        ("new.py", None, ChangeType.ADDED),
        ("old.py", None, ChangeType.DELETED),
        ("after.py", "before.py", ChangeType.RENAMED),
    ]


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        (modified("poetry.lock"), "excluded_pattern"),
        (modified("src/generated/client.py"), "excluded_pattern"),
        (modified("image.png"), "excluded_pattern"),
        (modified(".env"), "sensitive_path"),
        (
            "diff --git a/blob.py b/blob.py\nBinary files a/blob.py and b/blob.py differ",
            "binary_file",
        ),
    ],
)
def test_lock_generated_binary_and_sensitive_files_are_excluded(patch, reason):
    included, skipped, statuses = filter_files(parse_diff(patch).files, config())
    assert included == []
    assert [(item.path, item.reason) for item in skipped] == [(parse_diff(patch).files[0].path, reason)]
    assert next(iter(statuses.values())) is InclusionStatus.SKIPPED


def test_secrets_are_redacted_without_mutating_parsed_diff():
    secret = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
    original = parse_diff(modified(hunks=[f"@@ -1 +1 @@\n-old\n+token={secret}"])).files
    redacted, count, categories = redact_files(original, config())
    assert secret in original[0].render()
    assert secret not in redacted[0].render()
    assert "+token=[REDACTED:AWS_ACCESS_KEY:1]" in redacted[0].render()
    assert count == 1
    assert categories == {"aws_access_key": 1}


def test_private_key_body_is_not_left_in_provider_content():
    begin = "-" * 5 + "BEGIN PRIVATE KEY" + "-" * 5
    end = "-" * 5 + "END PRIVATE KEY" + "-" * 5
    patch = modified(hunks=[
        f"@@ -0,0 +1,3 @@\n+{begin}\n+SENSITIVEKEYBODY\n+{end}"
    ])
    redacted, count, categories = redact_files(parse_diff(patch).files, config())
    content = redacted[0].render()
    assert "SENSITIVEKEYBODY" not in content
    assert "PRIVATE KEY-----" not in content
    assert count == 1
    assert categories == {"private_key": 1}


def test_oversized_file_can_be_skipped():
    files = parse_diff(modified()).files
    included, skipped, statuses = filter_files(
        files, config(max_file_bytes=1, oversized_file_behavior="skip")
    )
    assert included == []
    assert skipped[0].reason == "file_size_limit"
    assert statuses["app.py"] is InclusionStatus.SKIPPED


def test_oversized_file_truncates_only_at_hunk_boundary():
    patch = modified(hunks=[
        "@@ -1 +1 @@\n-a\n+b",
        "@@ -10 +10 @@\n-c\n+d",
    ])
    file = parse_diff(patch).files[0]
    one_hunk_size = len(replace(file, hunks=file.hunks[:1]).render().encode())
    included, skipped, statuses = filter_files(
        [file], config(max_file_bytes=one_hunk_size, oversized_file_behavior="truncate")
    )
    assert [hunk.header for hunk in included[0].hunks] == ["@@ -1 +1 @@"]
    assert skipped[0].reason == "file_partially_included"
    assert statuses["app.py"] is InclusionStatus.PARTIAL


def test_maximum_file_count_is_reported():
    files = parse_diff(modified("a.py") + "\n" + modified("b.py")).files
    included, skipped, statuses = filter_files(files, config(max_files=1))
    assert [item.path for item in included] == ["a.py"]
    assert [(item.path, item.reason) for item in skipped] == [("b.py", "maximum_file_count")]
    assert statuses["b.py"] is InclusionStatus.SKIPPED


def test_chunking_combines_whole_files_when_they_fit():
    files = parse_diff(modified("a.py") + "\n" + modified("b.py")).files
    result = create_chunks(files, config(), CharacterEstimator())
    assert len(result.chunks) == 1
    assert result.chunks[0].files == ["a.py", "b.py"]
    assert result.chunks[0].content == "\n".join(item.render() for item in files)


def test_chunking_preserves_file_boundaries_when_combination_does_not_fit():
    files = parse_diff(modified("a.py") + "\n" + modified("b.py")).files
    budget = max(len(item.render()) for item in files)
    result = create_chunks(files, config(max_chunk_input_tokens=budget), CharacterEstimator())
    assert [chunk.files for chunk in result.chunks] == [["a.py"], ["b.py"]]


def test_chunking_splits_large_file_at_hunk_boundaries():
    file = parse_diff(modified(hunks=[
        "@@ -1 +1 @@\n-a\n+b",
        "@@ -10 +10 @@\n-c\n+d",
    ])).files[0]
    hunk_sizes = [
        len(f"{file.render_headers()}\n{hunk.render()}")
        for hunk in file.hunks
    ]
    result = create_chunks(
        [file], config(max_chunk_input_tokens=max(hunk_sizes)), CharacterEstimator()
    )
    assert len(result.chunks) == 2
    assert [chunk.hunk_headers for chunk in result.chunks] == [
        ["@@ -1 +1 @@"], ["@@ -10 +10 @@"]
    ]
    assert all("@@" in chunk.content for chunk in result.chunks)


def test_max_chunk_count_omits_remaining_fragments():
    files = parse_diff(modified("a.py") + "\n" + modified("b.py")).files
    budget = max(len(item.render()) for item in files)
    result = create_chunks(
        files, config(max_chunk_input_tokens=budget, max_chunks=1), CharacterEstimator()
    )
    assert len(result.chunks) == 1
    assert [(item.path, item.reason) for item in result.skipped] == [
        ("b.py", "chunk_budget_limit")
    ]


def test_total_token_budget_omits_fragment_that_would_exceed_it():
    files = parse_diff(modified("a.py") + "\n" + modified("b.py")).files
    budget = max(len(item.render()) for item in files)
    result = create_chunks(files, config(
        max_chunk_input_tokens=budget,
        max_total_pr_tokens=len(files[0].render()),
    ), CharacterEstimator())
    assert [chunk.files for chunk in result.chunks] == [["a.py"]]
    assert [(item.path, item.reason) for item in result.skipped] == [
        ("b.py", "chunk_budget_limit")
    ]


def test_token_budget_subtracts_every_reservation_and_honors_chunk_cap():
    limited_by_context = config(
        model_context_tokens=1000,
        max_chunk_input_tokens=900,
        reserved_instruction_tokens=100,
        reserved_schema_tokens=50,
        reserved_metadata_tokens=25,
        reserved_output_tokens=200,
        safety_margin_tokens=75,
    )
    assert available_diff_budget(limited_by_context) == 550
    assert available_diff_budget(replace(limited_by_context, max_chunk_input_tokens=500)) == 500


def test_invalid_configuration_fails_validation():
    with pytest.raises(ValueError, match="leave no room"):
        replace(config(), model_context_tokens=10, reserved_output_tokens=10).validate()


def test_aggregation_orders_findings_and_removes_normalized_duplicates():
    chunk = ReviewChunk("chunk-1", "diff", ["b.py"], 4)
    primary = Finding("b.py", "Issue", "bad spacing", "high", "Correctness", 2)
    duplicate = Finding("B.PY", " issue ", "bad   spacing", "low", " correctness ", 2)
    first = Finding("a.py", "First", "first", "low", "style", 1)
    result = aggregate_results(
        [chunk],
        [ChunkResult("chunk-1", ProviderResult([primary, duplicate, first]))],
        [], {"b.py": InclusionStatus.FULL}, 2,
    )
    assert result.findings == [first, primary]
    assert result.processed_chunks == ["chunk-1"]
    assert result.generated_chunks == ["chunk-1"]
    assert result.estimated_tokens == 4
    assert result.redaction_count == 2


@pytest.mark.asyncio
async def test_mock_provider_error_is_isolated_as_failed_chunk():
    runner = ReviewRunner(MockReviewProvider("error"), config(), CharacterEstimator())
    result = await runner.run(modified())
    assert result.processed_chunks == []
    assert list(result.failed_chunks.values()) == ["RuntimeError"]


@pytest.mark.asyncio
async def test_mock_empty_response_is_reported_as_failed_chunk():
    runner = ReviewRunner(MockReviewProvider("empty"), config(), CharacterEstimator())
    result = await runner.run(modified())
    assert list(result.failed_chunks.values()) == ["empty_response"]


def test_partial_malformed_diff_keeps_valid_file_and_reports_bad_file():
    malformed = modified("bad.py", ["@@ -1 +1 @@\n?unsupported"])
    parsed = parse_diff(modified("good.py") + "\n" + malformed)
    assert [item.path for item in parsed.files] == ["good.py"]
    assert [(item.path, item.reason) for item in parsed.skipped] == [
        ("bad.py", "parse_error")
    ]


@pytest.mark.asyncio
async def test_logs_never_contain_complete_diff_or_secret(caplog):
    secret = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
    patch = modified(hunks=[f"@@ -1 +1 @@\n-old-value\n+token={secret}"])
    logger = logging.getLogger("review-runner-test")
    with caplog.at_level(logging.INFO, logger=logger.name):
        await ReviewRunner(MockReviewProvider("no_findings"), config(), CharacterEstimator(), logger).run(patch)
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert patch not in messages
    assert secret not in messages
    assert "old-value" not in messages
    assert "content redacted count=1" in messages


@pytest.mark.asyncio
async def test_partial_provider_failure_continues_remaining_chunks():
    class PartialProvider:
        def __init__(self):
            self.calls = 0

        async def review(self, chunk):
            self.calls += 1
            if self.calls == 1:
                return ProviderResult(valid=False, error_category="provider_unavailable")
            return ProviderResult()

    patch = modified("a.py") + "\n" + modified("b.py")
    files = parse_diff(patch).files
    budget = max(len(item.render()) for item in files)
    provider = PartialProvider()
    result = await ReviewRunner(
        provider,
        config(max_chunk_input_tokens=budget),
        CharacterEstimator(),
    ).run(patch, commit_sha="a" * 40)
    assert provider.calls == 2
    assert len(result.processed_chunks) == 1
    assert len(result.failed_chunks) == 1
    assert result.partial
    assert not result.ai_review_skipped
