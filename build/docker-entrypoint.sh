#!/bin/sh
set -e
# Run as the requested UID/GID so files written to the mounted appdata paths are
# owned consistently. This matches the Unraid/NAS convention (defaults there are
# nobody:users = 99:100). When PUID/PGID are unset, fall back to the image's
# original non-root app user (1001), preserving prior behaviour.
PUID="${PUID:-1001}"
PGID="${PGID:-1001}"

# Chown the only paths the app writes to before dropping privileges:
#   /app/jobs           registry.json, downloaded audio, and stems
#   /cache              torch/Demucs model weights (TORCH_HOME, XDG_CACHE_HOME)
#   /app/settings.json  best-effort settings persistence (created on demand)
# App code and the venv under /app stay world-readable, so a different UID can
# still import and run them. Re-chowning is also what fixes a bind mount that
# Docker created as root on first run.
chown -R "${PUID}:${PGID}" /app/jobs /cache 2>/dev/null || true
touch /app/settings.json 2>/dev/null && chown "${PUID}:${PGID}" /app/settings.json 2>/dev/null || true

# Drop to the target user and exec the CMD. gosu accepts a numeric UID:GID even
# when no matching named user exists.
exec gosu "${PUID}:${PGID}" "$@"
