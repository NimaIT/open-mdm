# MDM Platform — no Node build stage required.
# React is vendored under app/static/vendor/, so the image needs no JS toolchain
# and the application works in air-gapped networks.
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# libpq for psycopg, curl for the container healthcheck
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/mdm

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY scripts/ ./scripts/
COPY alembic/ ./alembic/
COPY docs/ ./docs/
COPY README.md pytest.ini ./

# Run unprivileged.
RUN useradd --system --uid 10001 --create-home mdm \
 && chown -R mdm:mdm /srv/mdm
USER mdm

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
