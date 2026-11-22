# Small, non-root image for remote (HTTP) deployments.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    KAGGLE_MCP_TRANSPORT=http KAGGLE_MCP_HOST=0.0.0.0 KAGGLE_MCP_PORT=8000 \
    KAGGLE_MCP_WORKSPACE=/data/workspace

RUN useradd --create-home --uid 10001 app && mkdir -p /data/workspace && chown -R app /data
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && rm -rf /root/.cache

USER app
VOLUME ["/data/workspace"]
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1
# KAGGLE_API_TOKEN and KAGGLE_MCP_AUTH_TOKEN must be provided at runtime (never baked in).
CMD ["kaggle-mcp", "--transport", "http", "--stateless"]
