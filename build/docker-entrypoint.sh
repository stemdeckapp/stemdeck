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

# Serve TLS when a certificate is supplied. Appended to the CMD rather than
# baked into it so `docker run ... uvicorn ...` overrides still work, and so an
# install with no certificate is unchanged.
#
# StemDeck never generates a certificate: that needs a dependency, and a
# self-signed one hands every client a full-page browser warning. Bring one from
# a reverse proxy, `tailscale serve`, or mkcert. A proxy that terminates TLS in
# front of the container needs nothing here at all -- it forwards
# X-Forwarded-Proto and the app trusts that.
if [ -n "${STEMDECK_SSL_CERT:-}" ] && [ -n "${STEMDECK_SSL_KEY:-}" ]; then
  if [ ! -r "${STEMDECK_SSL_CERT}" ] || [ ! -r "${STEMDECK_SSL_KEY}" ]; then
    echo "STEMDECK_SSL_CERT/STEMDECK_SSL_KEY are set but not readable" >&2
    exit 1
  fi
  set -- "$@" --ssl-certfile "${STEMDECK_SSL_CERT}" --ssl-keyfile "${STEMDECK_SSL_KEY}"
fi

# Drop to the target user and exec the CMD. gosu accepts a numeric UID:GID even
# when no matching named user exists.
exec gosu "${PUID}:${PGID}" "$@"
