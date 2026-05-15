#!/bin/bash
# Build the slim-gemini-agent image locally.
# Run from the Slim-Agent-Gemini/ directory.

set -e

cd "$(dirname "$0")"

# docker-compose handles the parent-directory context that lets the Dockerfile
# COPY ../slim-agent-core into the image.
docker compose build "$@"
