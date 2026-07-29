FROM python:3.11.15-alpine3.24 AS build

WORKDIR /build

RUN apk upgrade --no-cache \
    && apk add --no-cache --virtual .build-deps build-base

COPY requirements.txt .
RUN python -m pip install --no-cache-dir --prefix=/install -r requirements.txt \
    && PYTHONPATH=/install/lib/python3.11/site-packages python -m pip check

FROM python:3.11.15-alpine3.24

RUN apk upgrade --no-cache \
    && python -m pip uninstall --yes pip setuptools wheel \
    && addgroup -S -g 10001 payment \
    && adduser -S -D -H -u 10001 -G payment payment

WORKDIR /app

COPY --from=build /install /usr/local
COPY --chown=payment:payment main.py database.py models.py security.py ./
COPY --chown=payment:payment routes ./routes

USER 10001

EXPOSE 8005

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8005/health', timeout=2)"]

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8005"]
