FROM python:3.14-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:python3.14-bookworm-slim /usr/local/bin/uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --group ingest --no-install-project

COPY pyproject.toml uv.lock README.md ./
COPY backend/ backend/
COPY frontend/ frontend/
COPY .streamlit/config.toml .streamlit/config.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --group ingest

FROM python:3.14-slim-bookworm

ARG THEATERDB_COMMIT=""
RUN groupadd --system --gid 999 nonroot \
 && useradd --system --gid 999 --uid 999 --create-home nonroot
WORKDIR /app
COPY --from=builder /app /app
RUN mkdir -p /app/data/crawls && chown nonroot:nonroot /app/data/crawls

ENV PATH="/app/.venv/bin:$PATH" THEATERDB_COMMIT="${THEATERDB_COMMIT}"
USER nonroot
CMD ["sh", "-c", "python -m backend.db migrate && exec streamlit run frontend/app.py"]
