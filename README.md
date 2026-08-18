# Sports Store Payment Service

FastAPI mock payment provider responsible for idempotent charges and deterministic payment-decline behavior.

## Runtime

- Port: `8005`
- Database: `payment_db`
- Health endpoint: `/health`

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn main:app --reload --port 8005
```

Browser clients normally reach this service through the same-origin gateway.
Set `ALLOWED_ORIGINS` only for direct cross-origin browser access. It accepts a
comma-separated list of exact trusted origins. The default is empty; wildcards
and malformed origins are rejected during application import.

## Tests

```bash
pytest tests/ -v
```

Pull requests run the repository's hardened quality and security checks before
they are eligible for review.

## PR Diff Review Runner

The provider-independent pipeline and trusted post-CI GitHub Actions integration are documented in [`review_runner/README.md`](review_runner/README.md). Local use accepts a supplied unified patch and uses the mock provider; the trusted reusable workflow retrieves Pull Request diffs as data and invokes OpenRouter only after deterministic CI succeeds.
