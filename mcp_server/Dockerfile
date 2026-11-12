FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.6 /uv /usr/local/bin/uv

# Bytecode is compiled at build time because the container runs with a read-only filesystem
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, so code changes don't reinstall them
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY applai_mcp/ ./applai_mcp/
RUN uv sync --locked --no-dev --no-editable

# Unprivileged runtime user (same UID as the ApplAI container, so job_applications/ stays readable)
RUN useradd --no-create-home --uid 1000 applai
USER applai

ENV PATH="/app/.venv/bin:$PATH" \
    MCP_TRANSPORT=http \
    HTTP_HOST=0.0.0.0 \
    HTTP_PORT=8080
EXPOSE 8080

CMD ["applai-mcp", "--transport", "http"]
