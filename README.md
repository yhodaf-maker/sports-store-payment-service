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
