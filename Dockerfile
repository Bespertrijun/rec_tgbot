FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic
COPY entrypoint.sh ./entrypoint.sh
RUN pip install --no-cache-dir .
RUN useradd --create-home --uid 10001 bot && mkdir -p /var/lib/reclaude-bot/cookies \
    && chown -R bot:bot /app /var/lib/reclaude-bot
USER bot
ENTRYPOINT ["/app/entrypoint.sh"]
