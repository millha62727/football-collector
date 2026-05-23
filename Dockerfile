FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY scripts/ ./scripts/
COPY run_collector.py .

# Default healthcheck targets the web container — `db_ping` is cheap and
# proves both the FastAPI process and the DB pool are reachable. The
# collector container overrides CMD via compose; its healthcheck is the
# heartbeat-based scripts/healthcheck_collector.py.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "from app.database import db_ping; import sys; sys.exit(0 if db_ping() else 1)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
