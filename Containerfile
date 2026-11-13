# Streamable HTTP で動かすためのイメージ。
# mock (private submodule) は dev 依存なので、--no-dev で入れない。
FROM ghcr.io/astral-sh/uv:0.9.7-python3.12-trixie-slim AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-trixie
RUN useradd --system --uid 10001 --no-create-home app
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    MCP_HTTP_HOST=0.0.0.0 \
    MCP_HTTP_PORT=8000
USER 10001
EXPOSE 8000
ENTRYPOINT ["soundcraft-ui-mcp", "--transport", "http"]
