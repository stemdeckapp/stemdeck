#!/bin/sh
# Give the folders the app writes to the owner the container runs as, changing
# only the entries that are wrong (#994).
#
# Usage: docker-reown.sh UID GID DIR...
#
# The entrypoint used to run `chown -R` over the whole library on every start.
# That rewrote every file even when all of them were already right: slow on a
# large Unraid share or NAS, and it bumped every file's change time, so backup
# and sync tools saw the whole library as changed. This walks each tree once,
# reading only, and chowns just the entries whose owner or group differs. A
# tree that is already right is left exactly as it was, and the repairs the
# recursive chown was there for still happen: a bind mount Docker created as
# root on first run, and files a root process left inside the library.
#
# It never leaves the trees it is given: find -P does not follow a symlink, and
# chown -h changes the link itself, never what it points at. A DIR that is
# itself a symlink, or not a directory, is skipped.
#
# Best effort, like the chown it replaces: it always exits 0, so a read-only
# mount cannot stop the app from starting. It prints one line per DIR saying
# what it did and how long it took.

if [ "$#" -lt 3 ]; then
  echo "stemdeck: ownership: usage: docker-reown.sh UID GID DIR..." >&2
  exit 0
fi

uid="$1"
gid="$2"
shift 2

case "$uid" in
  '' | *[!0-9]*)
    echo "stemdeck: ownership: PUID '${uid}' is not a number, ownership left unchanged" >&2
    exit 0
    ;;
esac
case "$gid" in
  '' | *[!0-9]*)
    echo "stemdeck: ownership: PGID '${gid}' is not a number, ownership left unchanged" >&2
    exit 0
    ;;
esac

# Milliseconds since the epoch. GNU date prints %N; one that cannot falls back
# to whole seconds rather than breaking the arithmetic.
now_ms() {
  t="$(date +%s%N 2>/dev/null)"
  case "$t" in
    '' | *[!0-9]*) t="$(date +%s)000000000" ;;
  esac
  echo $((t / 1000000))
}

for dir in "$@"; do
  if [ -L "$dir" ] || [ ! -d "$dir" ]; then
    echo "stemdeck: ownership: ${dir} is not a directory, skipped"
    continue
  fi

  start="$(now_ms)"
  # One character per entry: x for an entry that was re-owned, c for one that
  # was already right. Counting characters, not lines, keeps a file name with a
  # newline in it from skewing the count. -exec ... {} + returns true, so a
  # matched entry always prints x; find still exits non-zero if a chown fails.
  rc=0
  marks="$(find -P "$dir" \( ! -user "$uid" -o ! -group "$gid" \) \
    -exec chown -h -- "${uid}:${gid}" {} + -printf x -o -printf c 2>/dev/null)" || rc=$?
  took=$(($(now_ms) - start))

  total=${#marks}
  changed="$(printf '%s' "$marks" | tr -cd x | wc -c)"
  changed=$((changed))

  if [ "$changed" -eq 0 ]; then
    msg="${dir} already owned by ${uid}:${gid}, nothing changed (${total} entries checked in ${took} ms)"
  else
    msg="${dir}: re-owned ${changed} of ${total} entries to ${uid}:${gid} in ${took} ms"
  fi
  if [ "$rc" -ne 0 ]; then
    msg="${msg}; some entries could not be checked or re-owned, for example on a read-only mount"
  fi
  echo "stemdeck: ownership: ${msg}"
done

exit 0
