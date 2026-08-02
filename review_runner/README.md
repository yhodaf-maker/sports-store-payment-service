# PR Diff Review Runner

`review_runner` prepares a supplied Pull Request unified diff for an AI review provider. It never inspects the repository or publishes GitHub comments. The default CLI remains offline and uses the mock provider; `OpenRouterProvider` is available to later workflow integration.

## Local Use

Read a patch from a file:

```bash
python -m review_runner --diff changes.patch --mock-scenario findings
```

Read a patch from standard input:

```bash
git diff main...HEAD | python -m review_runner --mock-scenario no_findings
```

Operational logs go to stderr. The structured `ReviewResult` is emitted as JSON on stdout. Exit status is `0` when all generated chunks are processed, `1` when a provider chunk fails, and `2` for invalid configuration or input file errors.

The accepted input is a Git unified patch containing `diff --git` file sections and `@@` unified hunks. Empty patches are valid. Added, modified, deleted, renamed, binary, quoted-path, and missing-final-newline metadata are retained where Git includes them.

## Configuration

Pass a JSON object with `--config path.json`. Every option can also be overridden with a `REVIEW_RUNNER_<UPPERCASE_NAME>` environment variable. Tuple/list values use a JSON array or a comma-separated value.

| Option | Default | Purpose |
| --- | ---: | --- |
| `included_file_types` | Common source/config/documentation suffixes | Allowed file suffixes |
| `excluded_patterns` | See below | Excluded `PurePath.match` patterns |
| `sensitive_patterns` | `.env` patterns | Paths never included in provider input |
| `max_file_bytes` | `51200` | Maximum rendered diff bytes per file |
| `max_file_lines` | `1500` | Maximum rendered diff lines per file |
| `max_files` | `100` | Maximum included files |
| `max_total_pr_tokens` | `100000` | Total generated chunk budget |
| `max_chunk_input_tokens` | `24000` | Configured per-chunk ceiling |
| `max_chunks` | `20` | Maximum generated chunks |
| `model_context_tokens` | `32000` | Provider model context window |
| `reserved_instruction_tokens` | `1500` | Instruction reservation |
| `reserved_schema_tokens` | `750` | Result schema reservation |
| `reserved_metadata_tokens` | `500` | Provider metadata reservation |
| `reserved_output_tokens` | `4000` | Expected output reservation |
| `safety_margin_tokens` | `1000` | Additional safety reservation |
| `oversized_file_behavior` | `truncate` | `truncate` at hunk boundaries or `skip` |
| `redaction_rules` | Common credential patterns | Named regular expressions |
| `logging_level` | `INFO` | Python logging level |
| `max_execution_seconds` | `300` | Overall runner/provider execution budget |

Available diff tokens are the smaller of `max_chunk_input_tokens` and the model context window after every reservation. The fallback estimator charges one token per UTF-8 byte. This intentionally overestimates typical model tokenization; a future provider can inject an exact implementation of `TokenEstimator`.

Custom redaction expressions may use no capture groups to replace the complete match, or one first capture group containing a safe prefix to preserve. The remainder is replaced with a stable marker such as `[REDACTED:CREDENTIAL:1]`.

## Filtering And Coverage

Default exclusions include dependency lockfiles, `vendor`, `node_modules`, generated directories and filenames, build outputs, minified files, source maps, images, fonts, PDFs, and archives. Binary markers are excluded independently. Repository overrides can replace these lists.

Filtering and sensitive-path checks happen before redaction, estimation, and chunk construction. Path rules are checked against both old and new names for renames. Every excluded item appears in `skipped` with a path and reason; omitted hunks also include their hunk header. `file_statuses` records `fully_reviewed`, `partially_included`, or `skipped`.

Chunking attempts the complete PR, then file boundaries, then hunk boundaries, and finally complete diff-line boundaries. File headers and hunk headers are repeated where needed. Stable chunk IDs include their sequence and a content digest. Chunk and PR limits stop further construction and create explicit `chunk_budget_limit` records.

## Mock Provider

`MockReviewProvider` implements the same asynchronous `ReviewProvider` protocol intended for OpenRouter. Scenarios are deterministic:

- `findings`: one finding per chunk
- `no_findings`: valid response without findings
- `multiple`: multiple findings per chunk
- `duplicates`: normalized duplicate findings
- `empty`: invalid empty response
- `error`: provider exception
- `invalid`: invalid structured result
- `delayed`: deterministic local delay

A provider implements `ReviewProvider.review(ReviewChunk) -> ProviderResult` and may implement `prepare(ReviewContext)` for run-scoped capability validation. Provider input must use `ReviewChunk.content`, which is constructed only after filtering and redaction. It must not receive the original patch.

## OpenRouter Provider

The MVP model is explicitly configured as `nvidia/nemotron-3-super-120b-a12b:free`. The adapter rejects `openrouter/free`, requires an explicit `:free` primary model, and never chooses a paid model implicitly. Change models through `OPENROUTER_MODEL`; approved model fallbacks may be listed explicitly in `OPENROUTER_APPROVED_FALLBACK_MODELS`. Changing providers requires only another `ReviewProvider` adapter and selection in `provider_factory.py`; diff processing and aggregation do not change.

Required configuration:

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | none | Required API credential; never logged |
| `OPENROUTER_API_BASE_URL` | `https://openrouter.ai/api/v1` | HTTPS API root |
| `OPENROUTER_MODEL` | `nvidia/nemotron-3-super-120b-a12b:free` | Exact approved free model |
| `OPENROUTER_MODEL_CONTEXT_TOKENS` | `128000` | Validated model context window |
| `OPENROUTER_MAX_OUTPUT_TOKENS` | `4000` | Per-response output ceiling |
| `OPENROUTER_CONNECT_TIMEOUT_SECONDS` | `10` | Connection timeout |
| `OPENROUTER_REQUEST_TIMEOUT_SECONDS` | `60` | Response timeout |
| `OPENROUTER_MAX_RETRIES` | `2` | Retries after the first attempt |
| `OPENROUTER_RETRY_INITIAL_DELAY_SECONDS` | `1` | Initial exponential-backoff delay |
| `OPENROUTER_RETRY_MAX_DELAY_SECONDS` | `8` | Maximum backoff delay |
| `OPENROUTER_MAX_REQUESTS_PER_RUN` | `25` | Physical request limit, including preflight and retries |
| `OPENROUTER_MAX_REQUESTS_PER_CHUNK` | `3` | Physical attempts per chunk |
| `OPENROUTER_MAX_INPUT_TOKENS_PER_RUN` | `100000` | Submitted input-token budget |
| `OPENROUTER_MAX_OUTPUT_TOKENS_PER_RUN` | `20000` | Reported output-token budget |
| `OPENROUTER_MAX_EXECUTION_SECONDS` | `300` | Provider run deadline |
| `OPENROUTER_MAX_RESPONSE_BYTES` | `256000` | Response parsing limit |
| `OPENROUTER_REQUIRE_STRUCTURED_OUTPUTS` | `true` | Require native JSON Schema support |
| `OPENROUTER_REQUIRE_ZERO_DATA_RETENTION` | `false` | Opt in to routing only through ZDR endpoints |
| `OPENROUTER_DENY_DATA_COLLECTION` | `false` | Opt out of routes that may collect or train on submitted data |
| `OPENROUTER_ALLOWED_PROVIDERS` | empty | Optional comma-separated route allowlist |
| `OPENROUTER_APP_URL` | empty | Optional `HTTP-Referer` attribution |
| `OPENROUTER_APP_TITLE` | empty | Optional application title |

Before reviewing content, the adapter queries the exact model's endpoints and verifies availability, runner/model context compatibility, maximum output size, route status, and native `response_format` plus `structured_outputs` parameters. Completion requests additionally set `provider.require_parameters`, `provider.data_collection`, and the configured `provider.zdr` value. ZDR and data-collection denial are disabled by default to support the selected free model. Submitted sanitized diff chunks may therefore be retained, collected, or used for training according to the selected endpoint's policy. Set `OPENROUTER_REQUIRE_ZERO_DATA_RETENTION=true` and `OPENROUTER_DENY_DATA_COLLECTION=true` when stricter privacy is required; unavailable compatible routing then fails open rather than weakening either configured control.

The system prompt limits analysis to the supplied changes and declares all metadata and diff content untrusted. The user message separately delimits trusted metadata and the sanitized diff. No tools or plugins are supplied. Source instructions cannot change the schema, request secrets, suppress findings, or expand review scope.

The strict response contains `summary`, `overall_risk`, and `findings`. Each finding requires `file_path`, nullable `line_number`, `severity`, `category`, `title`, `explanation`, `suggested_remediation`, and `confidence`. Severity is one of `INFO`, `LOW`, `MEDIUM`, `HIGH`, or `CRITICAL`; category is one of `STYLE`, `PERFORMANCE`, `SECURITY`, `BUG`, `RELIABILITY`, or `MAINTAINABILITY`; confidence is between `0.0` and `1.0`. Extra fields and incorrect types are rejected. Unknown files invalidate the response. Invalid line numbers are conservatively converted to file-level findings with `line_number: null`.

Rate limits, network failures, timeouts, gateway/provider failures, and temporary invalid structured responses use bounded exponential retries. Authentication, invalid configuration, unsupported parameters, privacy rejection, and oversized payloads are not retried. Request, token, retry, and duration quotas are checked before requests. One failed chunk does not stop later chunks unless a run quota or preflight requirement blocks the review. `failure_details`, `partial`, and `ai_review_skipped` describe safe advisory outcomes.

Stable failure categories are `configuration_error`, `authentication_error`, `model_unavailable`, `unsupported_capability`, `privacy_requirement_unavailable`, `rate_limited`, `provider_unavailable`, `network_timeout`, `payload_too_large`, `invalid_structured_response`, `quota_exhausted`, and `unexpected_provider_error`. Their reasons are sanitized and contain no provider response text.

Operational logs contain identifiers, token counts, duration, retries, validation outcome, and failure category only. API keys, authorization headers, prompts, diffs, and raw responses are never logged.

Run deterministic provider tests without network access or quota consumption:

```bash
pytest -q tests/test_openrouter_provider.py tests/test_review_runner.py
```

For an optional manual test, export `OPENROUTER_API_KEY` from a secret store, construct the provider with `OpenRouterConfig.load()`, call `prepare(ReviewContext(...))`, and submit one already-sanitized `ReviewChunk`. Do not use a repository diff containing secrets. Manual testing is intentionally separate from pytest and is not part of CI.

Free OpenRouter models are limited, rate-limited, mutable, and non-SLA-backed. Availability, endpoint capabilities, and privacy-compatible routes may change. Such changes produce a skipped advisory review rather than switching models or failing deterministic CI.

## GitHub Actions Integration

The integration has three trust boundaries:

1. `.github/workflows/ci.yml` runs every deterministic Pull Request check: branch naming, Ruff, pytest, dependency validation, Gitleaks, Checkov, the container build, and Trivy.
2. `.github/workflows/ai-review-after-ci.yml` runs from the default branch after `PR Quality and Security Validation` completes. Its job exists only when the completed run is a successful Pull Request run with resolved PR metadata.
3. `.github/workflows/reusable-ai-review.yml` checks out the exact base SHA, installs dependencies from that trusted revision, retrieves the PR diff through the GitHub API, and invokes `python -m review_runner.github_review`.

The `workflow_run` intermediary is intentional. A reusable workflow called directly by a `pull_request` workflow can be changed by a same-repository PR before receiving a repository secret. The completion event instead executes trusted default-branch workflow logic and passes fork content only as API-retrieved data. The review never checks out or executes the PR head.

The orchestrator requires the complete deterministic workflow conclusion to equal `success` and exactly one associated PR. Before provider preparation, the trusted driver resolves that run ID through the Actions API, binds it to the same PR, verifies every explicitly configured required job concluded `success`, and requires the head copy of `ci.yml` to be byte-for-byte identical to the trusted base copy. A failed, canceled, skipped, duplicated, missing, or PR-modified required check therefore cannot consume an OpenRouter request. A PR that intentionally changes `ci.yml` is conservatively skipped until that workflow change is merged and becomes trusted. No waiting comment is created before CI finishes. Deterministic CI remains authoritative; AI severity never changes a job exit status.

### Inputs And Secret

The trusted caller passes the PR number, `owner/repository`, base SHA, head SHA, reviewed SHA, deterministic workflow run ID, and a JSON list naming every required deterministic job. It may pass trusted runner configuration as a JSON object. The driver validates these values against current GitHub API metadata before retrieving the diff or calling OpenRouter. Branch environment variables and PR-controlled files are not authoritative metadata or configuration.

Configure one Actions secret:

```text
OPENROUTER_API_KEY
```

The caller passes that secret explicitly as `openrouter_api_key`; it does not use `secrets: inherit`. `GITHUB_TOKEN` is supplied automatically. No AWS, deployment, database, registry, environment, or organization-wide secrets are passed.

Required permissions are:

```yaml
permissions:
  actions: read
  contents: read
  pull-requests: write
```

`actions: read` validates the originating run and every required job conclusion before provider use. `contents: read` supports trusted base checkout, PR metadata/diff reads, and comparison of the head CI definition with the trusted base definition. `pull-requests: write` supports locating, creating, and updating the persistent PR issue comment. No cloud identity, repository write, package, release, deployment, Actions-management, or security-event write permission is used.

### Fork Policy

Fork PRs use the same data-only flow. Trusted default-branch code retrieves the fork diff through the GitHub API and sends only DEP-314-filtered, redacted chunks to DEP-315. The workflow does not check out the fork, install fork dependencies, read fork configuration, or execute fork commands. If API metadata or diff retrieval cannot be guaranteed, the review is skipped with a non-sensitive explanation and remains fail-open.

### Concurrency And Stale Runs

Reusable-workflow concurrency is scoped to `ai-review-<repository>-<pr-number>` with `cancel-in-progress: true`. A push to one PR cancels only that PR's older review. Immediately before final publication, the driver reads the PR again and requires its current head to equal the reviewed SHA. Stale findings are discarded without updating the comment.

### Persistent Comment

One comment is identified by the exact application marker:

```html
<!-- sports-store-ai-review:v1 -->
```

Only marker-bearing comments authored by `github-actions[bot]` qualify. The lowest comment ID is canonical when duplicates exist. The client updates it, logs a sanitized duplicate count, and does not create another. A delete-between-lookup-and-update race causes one re-list before creation. Rate limits, timeouts, and transient API failures use bounded retries.

The comment progresses from `In Progress` to `Completed`, `Partial`, `Skipped`, or `Failed Safely`. It includes the reviewed SHA, validated summary and findings, risk, location, severity, category, remediation, confidence, coverage, skipped-reason counts, and limited operational totals. Model-controlled fields are HTML/Markdown escaped, marker injection and unsafe URL schemes are neutralized, findings are severity-prioritized, and output is size-limited. Raw JSON, diffs, prompts, responses, stack traces, and provider diagnostics are never rendered.

Provider, validation, privacy, quota, timeout, and comment failures are advisory. The driver emits normalized `review_status`, `comment_status`, and `reviewed_commit_sha` outputs and exits successfully. `HIGH` and `CRITICAL` findings do not block the PR.

### Disable Safely

Disable reviews by disabling `.github/workflows/ai-review-after-ci.yml` in Actions or removing its `workflow_run` trigger on the default branch. Do not weaken the success condition, add `always()`, move the OpenRouter secret into `ci.yml`, or use `secrets: inherit`.

### Testing And Troubleshooting

Run the offline suite without OpenRouter traffic:

```bash
ruff check .
pytest
python -m pip check
```

Workflow contract tests verify the success-only gate, trusted checkout, permissions, explicit secret, data-only execution, and PR-scoped concurrency. Mocked integration tests cover findings, no findings, provider failures, stale results, fork review/skip behavior, persistent updates, duplicate markers, comment deletion races, and API retries.

For representative repository validation, open a dummy PR and verify:

1. Passing all jobs starts the AI workflow and creates one comment for the exact head SHA.
2. A second push cancels the older review and updates the same comment.
3. Failing Ruff, pytest, Gitleaks, Checkov, or Trivy leaves the AI workflow unstarted and does not consume provider quota.
4. Canceling or deliberately skipping a required job also leaves the AI workflow unstarted.
5. A fork PR is reviewed through API data only, or displays a safe skip if the API cannot provide the diff.
6. Temporarily selecting a mock/unavailable provider in a test repository produces an advisory state without changing deterministic CI conclusions.

If no review starts after passing CI, verify the completed workflow name is exactly `PR Quality and Security Validation`, the event is `pull_request`, PR metadata exists on the completion payload, and the default branch contains both AI workflow files. If comments fail, verify the repository permits Actions to create PR comments and that workflow permissions are not restricted below `pull-requests: write`. If the provider is skipped, inspect only the sanitized failure category in logs and verify model capability/privacy requirements and quota.

### Repository Rollout

Validate this repository first. For another application repository, copy the reusable workflow and trusted completion orchestrator from the validated default-branch revision, keep repository-specific deterministic checks in its main PR CI workflow, update the orchestrator's workflow name, configure only `OPENROUTER_API_KEY`, and retain the permissions, marker, concurrency, trusted base checkout, and success-only condition. Repository-specific review limits must be passed through the trusted `reviewer_config_json` input rather than read from the PR. Do not deploy to all repositories until one dummy-PR validation completes successfully.

## Limitations

- The parser supports standard Git unified patches, not arbitrary context or combined merge diffs.
- Files without `diff --git` sections are reported as unsupported.
- Rename metadata with unusual quoting outside standard Git output may be reported as malformed.
- The conservative estimator is intentionally much larger than a normal model tokenizer estimate.
- Redaction reduces transmission risk but is not a deterministic secret scanner and does not replace Gitleaks.
