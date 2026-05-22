#!/usr/bin/bash
set -euo pipefail

RESOURCE_GROUP_NAME="LEAP"
NAME="leap-video-transcoder"

# Load DATABASE_URL from local .env (never commit the value)
if [ -f .env ]; then
  export $(grep -v '^\s*#' .env | grep DATABASE_URL | xargs)
fi

if [ -z "${DATABASE_URL:-}" ]; then
  echo "ERROR: DATABASE_URL is not set. Add it to .env before rotating." >&2
  exit 1
fi

az containerapp secret set \
  --name "${NAME}" \
  --resource-group "${RESOURCE_GROUP_NAME}" \
  --secrets "database-url=${DATABASE_URL}"

az containerapp update \
  --name "${NAME}" \
  --resource-group "${RESOURCE_GROUP_NAME}"
