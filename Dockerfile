FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic
COPY entrypoint.sh ./entrypoint.sh
RUN pip install --no-cache-dir .
RUN apt-get update \
    && apt-get install --no-install-recommends --yes gosu \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 bot \
    && mkdir -p /var/lib/reclaude-bot/cookies /var/lib/reclaude-bot/logs \
    && chown -R bot:bot /app /var/lib/reclaude-bot
USER root
ENTRYPOINT ["/app/entrypoint.sh"]
