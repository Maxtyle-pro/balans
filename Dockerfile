FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client ca-certificates fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
COPY requirements.lock pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.lock
COPY balans ./balans
COPY migrations ./migrations
COPY scripts ./scripts
RUN groupadd --gid 10001 balans && useradd --uid 10001 --gid balans --create-home balans && mkdir -p /app/.local/receipts /app/.local/documents && chown -R balans:balans /app/.local
USER 10001:10001
CMD ["python", "-m", "balans"]
