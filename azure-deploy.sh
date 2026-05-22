#!/usr/bin/bash

RESOURCE_GROUP_NAME="LEAP"
LOCATION="eastus2"
NAME="leap-video-transcoder"

az containerapp up \
  --name "${NAME}" \
  --resource-group "${RESOURCE_GROUP_NAME}" \
  --location "${LOCATION}" \
  --source . \
  --ingress external \
  --target-port 8000