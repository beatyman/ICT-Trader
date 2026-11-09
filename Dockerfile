FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install ".[realtime]"

RUN useradd --create-home --uid 10001 ict \
    && mkdir -p /app/data/realtime \
    && chown -R ict:ict /app
USER ict

EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=2)"

CMD ["python", "-m", "ict_trader.realtime", "--mode", "live", "--host", "0.0.0.0", "--port", "8080"]
