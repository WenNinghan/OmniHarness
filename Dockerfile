FROM node:24-bookworm-slim AS web-build
WORKDIR /build/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
COPY src/omniharness/data/reference-prices-v1.json /build/src/omniharness/data/reference-prices-v1.json
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.11.6 AS uv
FROM python:3.12-slim-bookworm AS application
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    OMNI_DATA_DIR=/data \
    OMNI_SECRET_DIR=/secrets \
    OMNI_STATIC_DIR=/app/web/dist
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ ./src/
COPY alembic.ini ./
RUN uv sync --frozen --no-dev --no-editable \
    && mkdir -p /data /secrets \
    && chown 1000:1000 /data /secrets
COPY --from=web-build /build/web/dist ./web/dist
USER 1000:1000
EXPOSE 8787
CMD ["omniharness", "serve"]
