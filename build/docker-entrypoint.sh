#!/bin/sh
set -e
# Re-chown /app/jobs in case the bind-mount was created by Docker as root
# on first run (before the host directory existed). Safe no-op if already
# owned by app:app.
chown -R app:app /app/jobs 2>/dev/null || true
# Drop to the non-root app user and exec the CMD.
exec gosu app "$@"
