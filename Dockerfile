# What's On — container build for Coolify (kenlill.com).
# Python 3.11 to match the known-working Render runtime (render.yaml pins it;
# repo history has psycopg/Python-version churn — bump deliberately).
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# SQLite lives on a persistent volume mounted at /data (configure in Coolify).
RUN mkdir -p /data
ENV DATABASE_PATH=/data/whats_on.db

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
