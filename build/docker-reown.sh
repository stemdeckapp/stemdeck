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
# It never leaves the trees it is given, and it runs as root, so this matters:
#   - find -P does not follow a symlink.
#   - chown runs through -execdir, so find changes into each directory itself
#     and chown is handed ./name, never an absolute path. With an absolute path
#     chown would resolve every parent again, and another writer on the share
#     could swap a parent directory for a symlink (to /etc, say) between find
#     listing an entry and chown acting on it.
#   - chown -h changes a link itself, never what it points at.
#   - A DIR that is itself a symlink, or not a directory, is skipped.
#
# Best effort, like the chown it replaces: it always exits 0, so a read-only
# mount cannot stop the app from starting. It prints one line per DIR saying
# what it did and how long it took, and the first lines of any error, so a
# read-only or root_squash mount names an entry that failed.

log() { echo "stemdeck: ownership: $*"; }

if [ "$#" -lt 3 ]; then
  log "usage: docker-reown.sh UID GID DIR..." >&2
  exit 0
fi

uid="$1"
gid="$2"
shift 2

case "$uid" in
  '' | *[!0-9]*)
    log "PUID '${uid}' is not a number, ownership left unchanged" >&2
    exit 0
    ;;
esac
case "$gid" in
  '' | *[!0-9]*)
    log "PGID '${gid}' is not a number, ownership left unchanged" >&2
    exit 0
    ;;
esac

# find refuses -execdir while PATH holds a relative or empty entry, because the
# command would then be looked up in each directory it visits. Keep only the
# absolute entries, in their order; with none left, use the system default.
safe_path=""
old_ifs="$IFS"
IFS=:
for p in $PATH; do
  case "$p" in
    /*) safe_path="${safe_path:+${safe_path}:}${p}" ;;
  esac
done
IFS="$old_ifs"
PATH="${safe_path:-/usr/sbin:/usr/bin:/sbin:/bin}"
export PATH

work="$(mktemp -d 2>/dev/null)" || work=""
if [ -z "$work" ] || [ ! -d "$work" ]; then
  log "could not make a temporary folder, ownership left unchanged" >&2
  exit 0
fi
trap 'rm -rf "$work"' EXIT

# Run by -execdir with a batch of ./name entries from one directory. When chown
# reports a failure, find out which entries it left wrong (stat without -L
# reads a link itself) and write one line per entry to the failure file, so the
# count is real without giving up -execdir's batching on the success path.
# chown's own error lines go to find's stderr, which is kept. Single-quoted on
# purpose: the batch shell expands these, not this one.
# shellcheck disable=SC2016
batch='chown -h -- "$REOWN_OWNER" "$@" && exit 0
for f in "$@"; do
  [ "$(stat -c %u:%g -- "$f" 2>/dev/null)" = "$REOWN_OWNER" ] || echo x >>"$REOWN_FAILS"
done
exit 1'
REOWN_OWNER="${uid}:${gid}"
export REOWN_OWNER

# Milliseconds since the epoch. GNU date prints %N; one that cannot falls back
# to whole seconds rather than breaking the arithmetic.
now_ms() {
  t="$(date +%s%N 2>/dev/null)"
  case "$t" in
    '' | *[!0-9]*) t="$(date +%s)000000000" ;;
  esac
  echo $((t / 1000000))
}

n=0
for dir in "$@"; do
  if [ -L "$dir" ] || [ ! -d "$dir" ]; then
    log "${dir} is not a directory, skipped"
    continue
  fi

  n=$((n + 1))
  REOWN_FAILS="${work}/fails.${n}"
  export REOWN_FAILS
  errs="${work}/errs.${n}"
  : >"$REOWN_FAILS"

  start="$(now_ms)"
  # One character per entry: x for an entry that was wrong and handed to chown,
  # c for one that was already right. Counting characters, not lines, keeps a
  # file name with a newline in it from skewing the count. -execdir ... {} +
  # always counts as true, so the failures come from the failure file instead.
  rc=0
  marks="$(find -P "$dir" \( ! -user "$uid" -o ! -group "$gid" \) \
    -execdir sh -c "$batch" reown {} + -printf x -o -printf c 2>"$errs")" || rc=$?
  took=$(($(now_ms) - start))

  total=${#marks}
  wrong="$(printf '%s' "$marks" | tr -cd x | wc -c)"
  wrong=$((wrong))
  failed="$(wc -l <"$REOWN_FAILS")"
  failed=$((failed))
  changed=$((wrong - failed))

  if [ "$rc" -ne 0 ] && [ "$total" -eq 0 ]; then
    log "${dir} could not be checked, ownership left unchanged (find exited ${rc})"
  elif [ "$failed" -gt 0 ] || [ "$rc" -ne 0 ]; then
    log "${dir}: re-owned ${changed}, failed ${failed} of ${total} entries to ${uid}:${gid} in ${took} ms (find exited ${rc})"
  elif [ "$wrong" -eq 0 ]; then
    log "${dir} already owned by ${uid}:${gid}, nothing changed (${total} entries checked in ${took} ms)"
  else
    log "${dir}: re-owned ${changed} of ${total} entries to ${uid}:${gid} in ${took} ms"
  fi
  # The first five error lines name what failed; the rest would flood the log
  # on a library where every entry fails the same way.
  if [ -s "$errs" ]; then
    head -n 5 "$errs" | sed 's/^/stemdeck: ownership:   /'
    lines="$(wc -l <"$errs")"
    [ "$((lines))" -gt 5 ] && log "  ($((lines - 5)) more error lines not shown)"
  fi
done

exit 0
