import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cors_config import add_cors_middleware, parse_allowed_origins


def make_client(origins: str | None) -> TestClient:
    app = FastAPI()
    add_cors_middleware(app, origins)

    @app.get("/resource")
    def resource():
        return {"status": "ok"}

    return TestClient(app)


def test_trusted_and_untrusted_origins():
    client = make_client("https://shop.example.com")
    trusted = client.get("/resource", headers={"Origin": "https://shop.example.com"})
    untrusted = client.get("/resource", headers={"Origin": "https://attacker.example"})
    assert trusted.headers["access-control-allow-origin"] == "https://shop.example.com"
    assert "access-control-allow-origin" not in untrusted.headers


def test_multiple_origins_whitespace_and_duplicates():
    client = make_client(" http://localhost:5173, https://shop.example.com/, http://localhost:5173 ")
    for origin in ("http://localhost:5173", "https://shop.example.com"):
        assert client.get("/resource", headers={"Origin": origin}).headers[
            "access-control-allow-origin"
        ] == origin


@pytest.mark.parametrize("raw_value", [None, "", "  ", ", ,"])
def test_empty_configuration_fails_closed(raw_value):
    assert parse_allowed_origins(raw_value) == []


@pytest.mark.parametrize(
    "raw_value",
    ["*", "https://shop.example.com,*", "shop.example.com", "ftp://shop.example.com",
     "https://user:password@shop.example.com", "https://shop.example.com/api",
     "https://shop.example.com?trusted=true", "http://localhost:not-a-port"],
)
def test_wildcard_and_malformed_origins_are_rejected(raw_value):
    with pytest.raises(ValueError, match="ALLOWED_ORIGINS"):
        parse_allowed_origins(raw_value)


def test_payment_preflight_allows_post_and_required_headers():
    response = make_client("http://localhost:5173").options(
        "/resource",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST",
                 "Access-Control-Request-Headers": "authorization,content-type"},
    )
    assert response.status_code == 200
    assert "POST" in response.headers["access-control-allow-methods"]
    assert "authorization" in response.headers["access-control-allow-headers"].lower()
