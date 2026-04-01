FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md .env.example /app/
COPY qqbot /app/qqbot
COPY scripts /app/scripts

RUN pip install --upgrade pip setuptools wheel \
    && pip install -e .

RUN useradd --create-home --home-dir /home/qqbot --shell /usr/sbin/nologin qqbot \
    && mkdir -p /app/logs /app/runtime_data/images \
    && chown -R qqbot:qqbot /app

USER qqbot

VOLUME ["/app/logs", "/app/runtime_data"]

EXPOSE 7500

CMD ["python", "-m", "qqbot"]
