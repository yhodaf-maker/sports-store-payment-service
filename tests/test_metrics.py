"""DEP-263 2.1.2 - Prometheus metric scraping endpoint."""


def test_metrics_endpoint_exposes_prometheus_series(client):
    # Drive one recorded request so the histogram emits buckets. An unmatched
    # path is still instrumented (handler="none"); /health and /metrics are
    # intentionally excluded from instrumentation.
    client.get("/__unmatched_route_for_metric_sample__")

    response = client.get("/metrics")
    assert response.status_code == 200
    body = response.text

    # The two series the Sub-PRD 6 canary AnalysisTemplate queries.
    assert "http_requests_total" in body
    assert "http_request_duration_seconds_bucket" in body


def test_metrics_labels_exclude_high_cardinality_values(client):
    # AC 2.1.2.2 - user_id / order_id / cart_id must never become label values.
    body = client.get("/metrics").text
    for high_cardinality in ("user_id", "order_id", "cart_id"):
        assert high_cardinality not in body
