# Sports Store Payment Service

Deterministic mock payment service for Sports Store. It demonstrates payment coordination and idempotency without contacting a real bank or charging real money.

## Role in the system

The order service calls this service on port `8005` during checkout. A charge is stored in the `payment_db` MongoDB database under an idempotency key, so retrying the same request returns the existing result instead of creating a second charge. Cards ending with `PAYMENT_FAILURE_SUFFIX` are deliberately declined.

> This is a demonstration component, not a production payment processor. Do not send real card data.

## Technology and structure

- Python, FastAPI, Uvicorn, Motor, Pydantic, PyJWT, and Prometheus instrumentation.
- `routes/payments.py`: create and retrieve mock charges.
- `models.py`, `database.py`, and `security.py`: data, persistence, and token verification.
- `tests/`, `.github/workflows/`, and `review_runner/`: tests, automation, and the [optional reviewer](review_runner/README.md).

## Configuration

| Variable | Purpose | Default/example |
| --- | --- | --- |
| `MONGO_URI` | MongoDB connection | `mongodb://localhost:27017` |
| `JWT_SECRET` | Verifies service/user tokens | local placeholder only |
| `PAYMENT_FAILURE_SUFFIX` | Card suffix that triggers a decline | `0000` |

Copy `.env.example` to `.env`. `OPENROUTER_*` is only for pull-request review. Keep credentials and real payment information out of source control.

## Run and inspect locally

Prerequisites: Python 3 and MongoDB. The complete system is available through [sports-store-local](https://github.com/Deploy-On-Friday2-0/sports-store-local).

```bash
python -m venv .venv
source .venv/bin/activate       # PowerShell: .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
cp .env.example .env
uvicorn main:app --reload --port 8005
```

Open `http://localhost:8005/docs` for OpenAPI.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/payments/charge` | Create or replay an idempotent mock charge |
| `GET` | `/api/payments/{idempotency_key}` | Retrieve a charge result |
| `GET` | `/health`, `/metrics` | Health and metrics |

## Validate and package

```bash
ruff check .
pytest
python -m pip check
docker build -t sports-store-payment-service:local .
docker run --rm -p 8005:8005 --env-file .env sports-store-payment-service:local
```

`PR Quality and Security Validation` runs Python checks plus Gitleaks, Checkov, and Trivy. `Publish Production Image` pushes a versioned Amazon ECR image and updates [sports-store-deployments](https://github.com/Deploy-On-Friday2-0/sports-store-deployments).

## Troubleshooting and contribution

- An intentional decline is expected when a submitted card ends in the configured suffix.
- MongoDB errors indicate an unreachable or invalid `MONGO_URI`; `401` indicates a missing or mismatched JWT secret.
- Production secrets belong in the infrastructure-managed secret path, not `.env` or Git. Follow [CONTRIBUTING.md](CONTRIBUTING.md) for changes.
