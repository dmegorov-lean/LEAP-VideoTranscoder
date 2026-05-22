#!/usr/bin/bash
set -euo pipefail

RESOURCE_GROUP_NAME="LEAP"
LOCATION="eastus2"
NAME="leap-video-transcoder"
# Storage account names must be globally unique, 3-24 lowercase alphanumeric chars
STORAGE_ACCOUNT_NAME="${NAME//-/}stg"   # leapvideotranscoder
BLOB_CONTAINER_NAME="leap-file-bin"

# Load DATABASE_URL from local .env (never commit the value)
if [ -f .env ]; then
  export $(grep -v '^\s*#' .env | grep DATABASE_URL | xargs)
fi

if [ -z "${DATABASE_URL:-}" ]; then
  echo "ERROR: DATABASE_URL is not set. Add it to .env before deploying." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Storage account + blob container (both commands are idempotent)
# ---------------------------------------------------------------------------

az storage account create \
  --name "${STORAGE_ACCOUNT_NAME}" \
  --resource-group "${RESOURCE_GROUP_NAME}" \
  --location "${LOCATION}" \
  --sku Standard_LRS \
  --kind StorageV2 \
  --allow-blob-public-access false \
  --only-show-errors

AZURE_STORAGE_CONNECTION_STRING=$(az storage account show-connection-string \
  --name "${STORAGE_ACCOUNT_NAME}" \
  --resource-group "${RESOURCE_GROUP_NAME}" \
  --query connectionString -o tsv)

az storage container create \
  --name "${BLOB_CONTAINER_NAME}" \
  --connection-string "${AZURE_STORAGE_CONNECTION_STRING}" \
  --only-show-errors

# ---------------------------------------------------------------------------
# Container App
# ---------------------------------------------------------------------------

az containerapp up \
  --name "${NAME}" \
  --resource-group "${RESOURCE_GROUP_NAME}" \
  --location "${LOCATION}" \
  --source . \
  --ingress external \
  --target-port 8000 \
  --secrets \
      "database-url=${DATABASE_URL}" \
      "azure-storage-connection-string=${AZURE_STORAGE_CONNECTION_STRING}" \
  --env-vars \
      "DATABASE_URL=secretref:database-url" \
      "AZURE_STORAGE_CONNECTION_STRING=secretref:azure-storage-connection-string" \
      "AZURE_STORAGE_CONTAINER=${BLOB_CONTAINER_NAME}"