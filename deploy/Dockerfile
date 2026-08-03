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
# Container Apps uses its own probes, but this keeps the image self-describing
# and gives `docker run` local health for free.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://localhost:8501/_stcore/health', timeout=4)"
CMD ["streamlit", "run", "lulu_ops_center.py", \
     "--server.port=8501", "--server.address=0.0.0.0", \
     "--server.headless=true", "--server.enableCORS=false", \
     "--server.enableXsrfProtection=false", "--browser.gatherUsageStats=false"]
