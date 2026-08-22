# Prophet backend (FastAPI + async SQLAlchemy). Poetry-managed.
FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    POETRY_VERSION=2.3.2 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install "poetry==${POETRY_VERSION}"

# Install dependencies first for layer caching.
COPY pyproject.toml poetry.lock ./
RUN poetry config virtualenvs.create false \
    && poetry install --no-root --only main --no-interaction --no-ansi

RUN groupadd --gid 10001 prophet \
    && useradd --uid 10001 --gid prophet --create-home --shell /usr/sbin/nologin prophet

COPY --chown=prophet:prophet . .
RUN mkdir -p /app/data /app/backups \
    && chown -R prophet:prophet /app/data /app/backups

USER prophet

EXPOSE 8000

# Runtime config (DB host, storage paths) is supplied by docker-compose env.
CMD ["python", "-m", "uvicorn", "investos.main:app", "--host", "0.0.0.0", "--port", "8000"]
