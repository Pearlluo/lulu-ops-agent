# Lulu Operations Center — Streamlit web app for Azure Container Apps.
# Gold parquet (~3MB) is baked in. Secrets (OPENAI/DEEPSEEK keys) are injected as
# env vars at runtime via Container App secrets — never baked.
# Build context = repo root:  az acr build -r <acr> -t lulu-app:latest -f deploy/Dockerfile .
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY deploy/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# agent modules + yaml/md config + static assets, and the baked gold lake
COPY data/agent /app/data/agent
COPY data/gold  /app/data/gold

WORKDIR /app/data/agent

EXPOSE 8501
CMD ["streamlit", "run", "lulu_ops_center.py", \
     "--server.port=8501", "--server.address=0.0.0.0", \
     "--server.headless=true", "--server.enableCORS=false", \
     "--server.enableXsrfProtection=false", "--browser.gatherUsageStats=false"]
