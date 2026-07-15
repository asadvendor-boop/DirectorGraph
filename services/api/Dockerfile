FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg \
      espeak \
      fonts-lato \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY app ./app
RUN python -m pip install --upgrade pip && python -m pip install .

RUN useradd --create-home --uid 10001 directorgraph \
    && mkdir -p /var/lib/directorgraph/media \
    && chown -R directorgraph:directorgraph /var/lib/directorgraph /app
USER directorgraph

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
