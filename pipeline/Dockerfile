# Lulu lake refresh pipeline — runs run_pipeline.py once per scheduled invocation.
# Credentials are injected as ENV VARS at runtime (Container App secrets), never baked in.
FROM python:3.11-slim

# nest two levels so the scripts' DATA_DIR (SCRIPT_DIR.parent.parent) resolves to /app/lulu,
# i.e. bronze/silver/gold get written under /app/lulu/* on the container's ephemeral disk.
WORKDIR /app/lulu/api
RUN pip install --no-cache-dir \
    requests python-dotenv pandas pyarrow azure-storage-blob

# scripts only (no credential/.env — env vars are provided by the Container Apps Job)
COPY *.py ./

CMD ["python", "-u", "run_pipeline.py"]
