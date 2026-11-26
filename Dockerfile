FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app
COPY perf/__init__.py perf/compare.py ./perf/
COPY scripts/serve.sh ./scripts/serve.sh
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 api
USER api

EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=3s --retries=30 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"
# Seeds the database, then starts the API. PERF_BUGS picks the problems.
CMD ["bash", "scripts/serve.sh"]
