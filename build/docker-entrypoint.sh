#!/bin/sh
set -e
# Run as the requested UID/GID so files written to the mounted appdata paths are
# owned consistently. This matches the Unraid/NAS convention (defaults there are
# nobody:users = 99:100). When PUID/PGID are unset, fall back to the image's
# original non-root app user (1001), preserving prior behaviour.
PUID="${PUID:-1001}"
PGID="${PGID:-1001}"

# Own the only paths the app writes to before dropping privileges:
#   /app/jobs           registry.json, downloaded audio, and stems
#   /cache              torch/Demucs model weights (TORCH_HOME, XDG_CACHE_HOME)
#   /app/settings.json  best-effort settings persistence (created on demand)
# App code and the venv under /app stay world-readable, so a different UID can
# still import and run them. Re-owning is also what fixes a bind mount that
# Docker created as root on first run. docker-reown.sh changes only the entries
# whose owner or group is wrong, so a library that is already right is read but
# not rewritten (#994).
sh "$(dirname -- "$0")/docker-reown.sh" "${PUID}" "${PGID}" /app/jobs /cache || true
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

# An AMD GPU (the :rocm image, run with --device /dev/kfd --device /dev/dri,
# #825) is reached through device nodes owned by a group, usually render, and
# often mode 0660. gosu with a numeric UID:GID drops every supplementary group,
# so the app would lose the GPU it was handed: keep the groups those nodes
# belong to. Only when /dev/kfd was handed in, so a container without an AMD
# GPU starts exactly as before. 0 and 65534 are skipped: root's group is not one
# to hand out, and 65534 is the overflow group of a rootless runtime, which
# setgroups refuses.
if [ -e /dev/kfd ]; then
  gpu_groups=""
  for dev in /dev/kfd /dev/dri/renderD*; do
    [ -e "$dev" ] || continue
    gid="$(stat -c %g "$dev")"
    case "$gid" in 0|65534) continue ;; esac
    case ",${gpu_groups}," in
      *",${gid},"*) ;;
      *) gpu_groups="${gpu_groups:+${gpu_groups},}${gid}" ;;
    esac
  done
  if [ -n "$gpu_groups" ]; then
    # setpriv, unlike gosu, leaves HOME at root's; set it the way gosu would.
    HOME="$(getent passwd "${PUID}" | cut -d: -f6)"
    export HOME="${HOME:-/}"
    exec setpriv --reuid="${PUID}" --regid="${PGID}" --groups="${gpu_groups}" -- "$@"
  fi
fi

# Drop to the target user and exec the CMD. gosu accepts a numeric UID:GID even
# when no matching named user exists.
exec gosu "${PUID}:${PGID}" "$@"
