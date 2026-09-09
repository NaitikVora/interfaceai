# Reproducible runtime. Chromium is installed by the pinned Playwright package itself
# (`playwright install --with-deps`), so the browser always matches the library version.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app
RUN pip install --no-cache-dir uv==0.11.9

COPY pyproject.toml uv.lock README.md ./
COPY app ./app
COPY demo_app ./demo_app
RUN uv sync --frozen --no-dev --python /usr/local/bin/python3 \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

COPY policies ./policies
COPY profiles ./profiles
COPY artifacts ./artifacts
COPY Makefile .env.example ./

ENTRYPOINT ["python", "-m", "app.cli"]
CMD ["--help"]
