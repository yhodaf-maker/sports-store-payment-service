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

## Tests

```bash
pytest tests/ -v
```

## PR Diff Review Runner

The provider-independent pipeline and trusted post-CI GitHub Actions integration are documented in [`review_runner/README.md`](review_runner/README.md). Local use accepts a supplied unified patch and uses the mock provider; the trusted reusable workflow retrieves Pull Request diffs as data and invokes OpenRouter only after deterministic CI succeeds.
