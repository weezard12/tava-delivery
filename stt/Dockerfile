# Speech recognition runs in its own image: faster-whisper's wheels lag behind new Pythons.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/models
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --system --uid 10002 --create-home stt \
    && mkdir -p /models && chown stt /models
COPY app.py ./
USER stt
EXPOSE 9000
# No HEALTHCHECK: the first start downloads the model, which can outlast a deploy's wait.
# /healthz answers 503 until the model is loaded; the warehouse service copes meanwhile.
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9000", "--no-access-log"]
