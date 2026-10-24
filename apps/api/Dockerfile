FROM python:3.11-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/apps/api \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 meeting \
    && mkdir -p /app /data /cache \
    && chown meeting:meeting /app /data /cache

WORKDIR /app
COPY --chown=meeting:meeting pyproject.toml uv.lock ./
USER meeting
RUN uv sync --frozen --no-dev --no-install-project

COPY --chown=meeting:meeting apps/api/app ./apps/api/app

CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000", "--no-access-log"]
