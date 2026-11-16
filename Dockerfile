FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock \
    && useradd --uid 10001 --create-home appuser \
    && mkdir -p /data /private/index /cache \
    && chown -R appuser:appuser /data /private /cache
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser schema.sql schema_postgres.sql ./
COPY --chown=appuser:appuser scripts/migrate_sqlite_to_postgres.py scripts/ingest.py ./scripts/
USER appuser
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
