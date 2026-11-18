#!/bin/sh
# booking API (restarts itself if it ever stops), then nginx in the foreground
mkdir -p /data
( while true; do python3 /app/api.py; echo "booking api stopped, restarting" >&2; sleep 2; done ) &
exec nginx -g 'daemon off;'
