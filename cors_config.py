import os
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


def parse_allowed_origins(raw_value: str | None) -> list[str]:
    """Parse a comma-separated list of exact HTTP(S) origins."""
    if raw_value is None or not raw_value.strip():
        return []
    origins: list[str] = []
    for item in raw_value.split(","):
        origin = item.strip()
        if not origin:
            continue
        if origin == "*":
            raise ValueError("ALLOWED_ORIGINS must not contain '*'")
        parsed = urlsplit(origin)
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError(f"Invalid origin in ALLOWED_ORIGINS: {origin!r}") from exc
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError(f"Invalid origin in ALLOWED_ORIGINS: {origin!r}")
        normalized = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
        if normalized not in origins:
            origins.append(normalized)
    return origins


def add_cors_middleware(app: FastAPI, raw_value: str | None = None) -> None:
    if raw_value is None:
        raw_value = os.environ.get("ALLOWED_ORIGINS")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=parse_allowed_origins(raw_value),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )
