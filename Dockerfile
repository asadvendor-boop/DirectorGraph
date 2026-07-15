FROM --platform=linux/amd64 node:22-alpine AS web-build

WORKDIR /src/apps/web
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci
COPY apps/web ./
RUN npm run build

FROM --platform=linux/amd64 python:3.13-slim AS runtime

ENV APP_MODE=web \
    APP_VERSION=0.1.0 \
    FRONTEND_DIST=/app/static \
    MEDIA_ROOT=/tmp/directorgraph/media \
    DATABASE_URL=sqlite:////tmp/directorgraph/directorgraph.db \
    PUBLIC_MEDIA_BASE_URL=http://localhost:8000/media \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg espeak-ng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY services/api ./services/api
RUN python -m pip install --no-cache-dir ./services/api
COPY --from=web-build /src/apps/web/dist /app/static

ARG BUILD_SHA=local
ARG BUILD_TIMESTAMP=local

ENV BUILD_SHA=${BUILD_SHA} \
    BUILD_TIMESTAMP=${BUILD_TIMESTAMP}

LABEL org.opencontainers.image.title="DirectorGraph" \
    org.opencontainers.image.description="Self-correcting budget-aware AI showrunner for Qwen Cloud" \
    org.opencontainers.image.revision="${BUILD_SHA}" \
    org.opencontainers.image.created="${BUILD_TIMESTAMP}" \
    org.opencontainers.image.version="0.1.0"

RUN useradd --create-home --shell /usr/sbin/nologin directorgraph \
    && mkdir -p /tmp/directorgraph \
    && chown -R directorgraph:directorgraph /tmp/directorgraph /app

USER directorgraph
EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
