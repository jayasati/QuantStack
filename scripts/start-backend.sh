#!/bin/sh
set -e

cd /repo/backend
echo "Running database migrations..."
alembic upgrade head

echo "Starting QuantStack backend..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
