import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator, metrics
from pymongo.errors import PyMongoError

from database import payments_collection
from routes import payments

logger = logging.getLogger("payment-service")

app = FastAPI(title="Sports Store — Payment Service")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(payments.router, prefix="/api")

# Prometheus metrics (DEP-263, Sub-PRD 7 2.1.2). Exposes GET /metrics with
# http_requests_total and http_request_duration_seconds — the series the
# Sub-PRD 6 canary AnalysisTemplate queries. Labels are limited to
# method/handler/status; `handler` is the templated route path, never the raw
# id, so high-cardinality values (user_id/order_id/cart_id) are never emitted
# as label values (AC 2.1.2.2).
Instrumentator(excluded_handlers=["/metrics", "/health"]).add(
    metrics.requests(metric_name="http_requests_total")
).add(
    metrics.latency(metric_name="http_request_duration_seconds")
).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)


@app.on_event("startup")
async def create_indexes():
    try:
        await payments_collection.create_index("idempotency_key", unique=True)
    except PyMongoError as exc:  # Mongo may be unavailable (e.g. unit tests)
        logger.warning("Index creation skipped: %s", exc)


@app.get("/health")
def health():
    return {"status": "ok", "service": "payment-service"}
