FROM python:3.15-slim AS build
WORKDIR /app
RUN python -m venv /opt/venv
COPY pyproject.toml ./
COPY src/ ./src/
RUN /opt/venv/bin/pip install --no-cache-dir .

FROM python:3.15-slim
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DELIVERY_DATA_DIR=/data
RUN useradd --system --uid 10001 --no-create-home delivery \
    && mkdir -p /data && chown delivery /data
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
USER delivery
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=7s --start-period=30s --retries=3 \
    CMD ["python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/healthz', timeout=6).close()"]
# One process: the outbox worker and the live-update bus run inside it.
CMD ["python", "-m", "teva_delivery", "--host", "0.0.0.0", "--port", "8000"]
