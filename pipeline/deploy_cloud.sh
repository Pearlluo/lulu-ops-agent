#!/usr/bin/env bash
# One-time deploy of the lake refresh pipeline as an Azure Container Apps Job (nightly cron).
# Reads secret values from credential/.env (never echoed). Idempotent-ish: re-run safe for most steps.
set -euo pipefail
cd "$(dirname "$0")"

# force UTF-8 so az CLI log streaming doesn't crash on the Windows cp1252 console
export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1
export AZURE_CORE_NO_COLOR=true

ENVFILE=credential/.env
getval(){ python -c "from dotenv import dotenv_values;print(dotenv_values('$ENVFILE').get('$1',''))"; }

RG=${RG:-lulu-rg}
LOC=${LOC:-australiaeast}
# reuse a previously-created ACR if present, else make one
ACR=${ACR:-$( [ -f .acr_name ] && cat .acr_name || echo agentacr$RANDOM )}
ENVN=${ENVN:-lulu-env}
JOB=${JOB:-lulu-refresh}
echo "RG=$RG LOC=$LOC ACR=$ACR ENVN=$ENVN JOB=$JOB"

echo "== register providers =="
az config set extension.use_dynamic_install=yes_without_prompt -o none
az provider register -n Microsoft.App --wait
az provider register -n Microsoft.ContainerRegistry --wait
az provider register -n Microsoft.OperationalInsights --wait

echo "== resource group =="
az group create -n "$RG" -l "$LOC" -o none

echo "== container registry =="
az acr create -n "$ACR" -g "$RG" --sku Basic --admin-enabled true -o none

echo "== build image in cloud (ACR Tasks, no local docker) =="
az acr build -r "$ACR" -t lulu-pipeline:latest .

echo "== container apps environment =="
az containerapp env create -n "$ENVN" -g "$RG" -l "$LOC" -o none

echo "== read secrets from .env =="
BLOBCONN=$(getval BLOB_CONNECTION_STRING)
OPMSID=$(getval OPMS_CLIENT_ID); OPMSSEC=$(getval OPMS_CLIENT_SECRET)
SPTID=$(getval SHAREPOINT_TENANT_ID); SPCID=$(getval SHAREPOINT_CLIENT_ID); SPCSEC=$(getval SHAREPOINT_CLIENT_SECRET)

echo "== create scheduled job (cron 0 18 UTC = 02:00 Perth, timeout 1.5h) =="
az containerapp job create \
  -n "$JOB" -g "$RG" --environment "$ENVN" \
  --trigger-type Schedule --cron-expression "0 18 * * *" \
  --replica-timeout 5400 --replica-retry-limit 1 --parallelism 1 \
  --image "$ACR.azurecr.io/lulu-pipeline:latest" \
  --cpu 1.0 --memory 2.0Gi \
  --registry-server "$ACR.azurecr.io" \
  --secrets blobconn="$BLOBCONN" opmsid="$OPMSID" opmssecret="$OPMSSEC" sptid="$SPTID" spcid="$SPCID" spcsecret="$SPCSEC" \
  --env-vars \
      BLOB_CONNECTION_STRING=secretref:blobconn \
      OPMS_CLIENT_ID=secretref:opmsid \
      OPMS_CLIENT_SECRET=secretref:opmssecret \
      OPMS_TENANT=your_opms_tenant \
      SHAREPOINT_TENANT_ID=secretref:sptid \
      SHAREPOINT_CLIENT_ID=secretref:spcid \
      SHAREPOINT_CLIENT_SECRET=secretref:spcsecret \
      SHAREPOINT_HOST=yourtenant.sharepoint.com \
  -o none

echo "$ACR" > .acr_name
echo "DEPLOY DONE: job '$JOB' in '$RG' (registry $ACR). Cron nightly 02:00 Perth."
