# What's On — container build for Coolify (kenlill.com).
# Python 3.11 to match the known-working Render runtime (render.yaml pins it;
# repo history has psycopg/Python-version churn — bump deliberately).
FROM python:3.11-slim

WORKDIR /app

# curl is required for Coolify's container healthcheck (it probes with curl
# INSIDE the container; slim images ship without it and get marked unhealthy).
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# SQLite lives on a persistent volume mounted at /data (configure in Coolify).
RUN mkdir -p /data
ENV DATABASE_PATH=/data/whats_on.db

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
