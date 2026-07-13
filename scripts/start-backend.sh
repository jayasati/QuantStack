#!/bin/sh
set -e

cd /repo/backend
echo "Running database migrations..."
alembic upgrade head

# If you ever add --workers N here, also set DEPLOYMENT_WORKERS=N in the
# environment -- the app refuses to start otherwise (see
# OpportunityLifecycleManager's startup guard in prediction/lifecycle.py).
echo "Starting QuantStack backend..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
