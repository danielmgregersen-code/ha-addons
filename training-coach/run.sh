#!/usr/bin/with-contenv bashio
# Launch the app through with-contenv so SUPERVISOR_TOKEN (exposed via the S6
# container environment) is present — a bare CMD does not inherit it.
cd /app
exec python3 -m uvicorn main:app --host 0.0.0.0 --port 8000
