#!/usr/bin/env bash
#
# Tests for build/docker-reown.sh and the entrypoint that calls it (#994).
#
# The container entrypoint used to `chown -R` the whole library on every start.
# docker-reown.sh changes only the entries whose owner or group is wrong. A
# mistake here is silent: a walk that follows a symlink out of the library
# re-owns files that are not StemDeck's, and one that skips too much leaves a
# root-owned file the app can no longer write. No Docker is needed: each case
# runs the real script against a throwaway tree, with a fake chown on PATH that
# only records what it was asked to change. Nothing outside the sandbox is
# touched.
#
# The mismatched-tree case needs two owners to tell apart. As root (the CI lint
# container) it makes them with a real chown; otherwise it uses a second group
# the user belongs to, and is reported as skipped when there is none.
#
# Run:  bash tests/ci/test_docker_reown_sh.sh

set -uo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
REOWN="${REPO_ROOT}/build/docker-reown.sh"
ENTRYPOINT="${REPO_ROOT}/build/docker-entrypoint.sh"

PASS=0
FAIL=0
SKIP=0

ok()   { PASS=$((PASS + 1)); printf 'PASS  %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); printf 'FAIL  %s%s\n' "$1" "${2:+  -- $2}"; }
# In CI a skip is a failure: the lint container runs as root and can make
# symlinks, so every case must run there.
skip() {
    if [[ -n "${GITHUB_ACTIONS:-}" ]]; then bad "$1" "skipped in CI: $2"; return; fi
    SKIP=$((SKIP + 1)); printf 'SKIP  %s  -- %s\n' "$1" "$2"
}
check(){ if [[ "$2" == "1" ]]; then ok "$1"; else bad "$1" "${3:-}"; fi }

# The physical path, so it matches what the fake chown sees with pwd -P.
SANDBOX="$(cd -- "$(mktemp -d)" && pwd -P)"
trap 'rm -rf "$SANDBOX"' EXIT

# Fake commands: each records its arguments, one per line, after a CALL line.
# The fake chown also records the directory it ran in, and with
# FAKE_CHOWN_FAIL=1 it fails on every entry the way a read-only mount does.
FAKEBIN="${SANDBOX}/bin"
mkdir -p "$FAKEBIN"
for cmd in gosu setpriv touch; do
    cat > "${FAKEBIN}/${cmd}" <<EOF
#!/bin/sh
{ echo CALL; for a in "\$@"; do printf '%s\n' "\$a"; done; } >> "${SANDBOX}/${cmd}.log"
exit 0
EOF
    chmod +x "${FAKEBIN}/${cmd}"
done
cat > "${FAKEBIN}/chown" <<EOF
#!/bin/sh
{ echo CALL; echo "CWD \$(pwd -P)"; for a in "\$@"; do printf '%s\n' "\$a"; done; } >> "${SANDBOX}/chown.log"
if [ "\${FAKE_CHOWN_FAIL:-}" = 1 ]; then
    for a in "\$@"; do
        case "\$a" in -*|*:*) ;; *) echo "chown: changing ownership of '\$a': Read-only file system" >&2 ;; esac
    done
    exit 1
fi
exit 0
EOF
chmod +x "${FAKEBIN}/chown"
reset_logs() { rm -f "${SANDBOX}"/*.log; }
calls_of() { local n; n="$(grep -c '^CALL$' "${SANDBOX}/$1.log" 2>/dev/null)"; echo "${n:-0}"; }
# The path arguments chown was given, as given: every argument that is not the
# CALL or CWD line, an option, the -- separator or the uid:gid pair.
chown_args() { grep -v -e '^CALL$' -e '^CWD ' -e '^-' -e '^[0-9]*:[0-9]*$' "${SANDBOX}/chown.log" 2>/dev/null; }
# The same, resolved against the directory chown ran in: what it changed.
chowned() {
    awk '/^CALL$/ {next} /^CWD / {cwd = substr($0, 5); next}
         /^-/ || /^[0-9]*:[0-9]*$/ {next}
         /^\.\// {print cwd "/" substr($0, 3); next}
         {print}' "${SANDBOX}/chown.log" 2>/dev/null | sort
}
# 1 when every path argument is ./name: one component, relative to the
# directory find changed into. An absolute path would make chown resolve every
# parent again, which a second writer on the share can swap for a symlink.
all_relative() {
    local bad_args
    bad_args="$(chown_args | grep -v -E '^\./[^/]+$')"
    [[ -n "$(chown_args)" && -z "$bad_args" ]] && echo 1 || echo 0
}

run_reown() { PATH="${FAKEBIN}:${PATH}" sh "$REOWN" "$@" 2>&1; }

# A small library: two songs, nested folders, a few files.
make_tree() {  # make_tree <dir>
    mkdir -p "$1/song-a/stems" "$1/song-b/stems"
    : > "$1/registry.json"
    : > "$1/song-a/source.wav"
    : > "$1/song-a/stems/vocals.wav"
    : > "$1/song-a/stems/drums.wav"
    : > "$1/song-b/stems/bass.wav"
}
owner_of() { stat -c '%u:%g' "$1"; }

# ---------------------------------------------------------------------------
# 1. A tree that is already owned right: no chown at all, ctime untouched
# ---------------------------------------------------------------------------

T1="${SANDBOX}/owned"
make_tree "$T1"
IFS=: read -r U1 G1 <<< "$(owner_of "$T1")"
reset_logs
OUT="$(run_reown "$U1" "$G1" "$T1")"
check "owned: no chown call" "$([[ "$(calls_of chown)" == "0" ]] && echo 1 || echo 0)" "$(calls_of chown) calls"
check "owned: logs nothing changed" "$([[ "$OUT" == *"already owned by ${U1}:${G1}, nothing changed"* ]] && echo 1 || echo 0)" "$OUT"
check "owned: logs entries checked and time" "$([[ "$OUT" == *"(10 entries checked in "*" ms)"* ]] && echo 1 || echo 0)" "$OUT"

# The same run with the real chown, to show no file's change time moves.
BEFORE="$(find "$T1" -printf '%p %C@\n' | sort)"
sleep 1
sh "$REOWN" "$U1" "$G1" "$T1" >/dev/null 2>&1
AFTER="$(find "$T1" -printf '%p %C@\n' | sort)"
check "owned: real run leaves every ctime as it was" "$([[ "$BEFORE" == "$AFTER" ]] && echo 1 || echo 0)"

# ---------------------------------------------------------------------------
# 2. A tree with some entries owned wrong: only those are re-owned
# ---------------------------------------------------------------------------

T2="${SANDBOX}/mixed"
make_tree "$T2"
WRONG=("$T2/song-a/stems" "$T2/song-a/stems/vocals.wav" "$T2/song-b/stems/bass.wav")
MIXED=""
if [[ "$(id -u)" == "0" ]]; then
    chown -R 0:0 "$T2" && chown 4242:4242 "${WRONG[@]}" && MIXED="0:0"
else
    PRIMARY="$(owner_of "$T2")"
    OTHER=""
    for g in $(id -G); do
        [[ "$g" == "${PRIMARY#*:}" ]] || { OTHER="$g"; break; }
    done
    if [[ -n "$OTHER" ]] && chgrp "$OTHER" "${WRONG[@]}" 2>/dev/null \
        && [[ "$(stat -c %g "${WRONG[0]}")" == "$OTHER" ]]; then
        MIXED="$PRIMARY"
    fi
fi
if [[ -z "$MIXED" ]]; then
    skip "mixed: only mismatched entries re-owned" "not root and no second group to tell owners apart"
else
    IFS=: read -r U2 G2 <<< "$MIXED"
    reset_logs
    OUT="$(run_reown "$U2" "$G2" "$T2")"
    EXPECT="$(printf '%s\n' "${WRONG[@]}" | sort)"
    GOT="$(chowned)"
    check "mixed: exactly the mismatched entries re-owned" "$([[ "$GOT" == "$EXPECT" ]] && echo 1 || echo 0)" "got: $(echo "$GOT" | tr '\n' ' ')"
    check "mixed: chown told not to follow links (-h)" "$(grep -qx -- '-h' "${SANDBOX}/chown.log" && echo 1 || echo 0)"
    check "mixed: chown given the target owner" "$(grep -qx -- "${U2}:${G2}" "${SANDBOX}/chown.log" && echo 1 || echo 0)"
    check "mixed: logs the count" "$([[ "$OUT" == *"re-owned 3 of 10 entries to ${U2}:${G2} in "*" ms"* ]] && echo 1 || echo 0)" "$OUT"
    check "mixed: chown given ./name, never an absolute path" "$(all_relative)" "$(chown_args | tr '\n' ' ')"
fi

# ---------------------------------------------------------------------------
# 2b. chown acts from inside each directory, and its failures are counted
# ---------------------------------------------------------------------------

T4="${SANDBOX}/wrong"
make_tree "$T4"
ALL4="$(find "$T4" | sort)"
# An owner nothing has, so every entry counts as wrong. Runs as any user.
reset_logs
OUT="$(run_reown 4242 4243 "$T4")"
check "execdir: chown given ./name, never an absolute path" "$(all_relative)" "$(chown_args | tr '\n' ' ')"
check "execdir: every entry re-owned, resolved from its own directory" \
  "$([[ "$(chowned)" == "$ALL4" ]] && echo 1 || echo 0)" "got: $(chowned | tr '\n' ' ')"
check "execdir: logs the count" "$([[ "$OUT" == *"re-owned 10 of 10 entries to 4242:4243 in "*" ms"* && "$OUT" != *failed* ]] && echo 1 || echo 0)" "$OUT"

# A chown that fails on every entry, as on a read-only or root_squash mount.
reset_logs
OUT="$(FAKE_CHOWN_FAIL=1 run_reown 4242 4243 "$T4")"; CODE=$?
check "chown fails: exit 0" "$([[ $CODE -eq 0 ]] && echo 1 || echo 0)" "got $CODE"
check "chown fails: logs re-owned 0, failed 10" "$([[ "$OUT" == *"re-owned 0, failed 10 of 10 entries"* ]] && echo 1 || echo 0)" "$OUT"
check "chown fails: names a failing entry" "$([[ "$OUT" == *"Read-only file system"* ]] && echo 1 || echo 0)" "$OUT"
check "chown fails: at most 5 error lines, the rest summed up" \
  "$([[ "$(grep -c 'Read-only file system' <<< "$OUT")" -eq 5 && "$OUT" == *"(5 more error lines not shown)"* ]] && echo 1 || echo 0)" "$OUT"

# A uid that passes the number check but that find refuses.
reset_logs
OUT="$(run_reown 99999999999999999999 100 "$T4")"; CODE=$?
check "uid find refuses: exit 0, no chown" "$([[ $CODE -eq 0 && "$(calls_of chown)" == "0" ]] && echo 1 || echo 0)" "$OUT"
check "uid find refuses: logs that it could not check" \
  "$([[ "$OUT" == *"could not be checked, ownership left unchanged"* && "$OUT" != *"already owned"* ]] && echo 1 || echo 0)" "$OUT"
check "uid find refuses: shows find's error" "$([[ "$OUT" == *"stemdeck: ownership:   "*"find"* ]] && echo 1 || echo 0)" "$OUT"

# A relative or empty PATH entry would make find refuse -execdir.
reset_logs
OUT="$(PATH="${FAKEBIN}::.:${PATH}" sh "$REOWN" 4242 4243 "$T4" 2>&1)"
check "relative PATH entries: dropped, the walk still runs" \
  "$([[ "$OUT" == *"re-owned 10 of 10 entries"* && "$(all_relative)" == 1 ]] && echo 1 || echo 0)" "$OUT"

# ---------------------------------------------------------------------------
# 3. A symlink pointing out of the library is not followed
# ---------------------------------------------------------------------------

T3="${SANDBOX}/linked"
OUTSIDE="${SANDBOX}/outside"
make_tree "$T3"
mkdir -p "$OUTSIDE/private"
: > "$OUTSIDE/private/not-stemdeck.txt"
# Git for Windows copies instead of linking unless told otherwise.
MSYS=winsymlinks:nativestrict ln -s "$OUTSIDE" "$T3/song-a/escape" 2>/dev/null
MSYS=winsymlinks:nativestrict ln -s "$OUTSIDE" "${SANDBOX}/library-link" 2>/dev/null
if [[ ! -L "$T3/song-a/escape" || ! -L "${SANDBOX}/library-link" ]]; then
    skip "symlink: not followed" "this filesystem cannot make a symlink"
else
    # An owner nothing has, so every entry in the tree counts as wrong.
    reset_logs
    OUT="$(run_reown 4242 4243 "$T3")"
    GOT="$(chowned)"
    check "symlink: the link itself is re-owned" "$(grep -qx -- "$T3/song-a/escape" <<< "$GOT" && echo 1 || echo 0)"
    check "symlink: nothing behind the link is touched" "$(grep -q -- "$OUTSIDE" <<< "$GOT" && echo 0 || echo 1)" "$GOT"
    check "symlink: nothing reached through the link" "$(grep -q -- "escape/" <<< "$GOT" && echo 0 || echo 1)" "$GOT"
    check "symlink: every library entry re-owned" "$([[ "$(wc -l <<< "$GOT")" -eq 11 ]] && echo 1 || echo 0)" "$(wc -l <<< "$GOT") paths"
    check "symlink: chown told not to follow links (-h)" "$(grep -qx -- '-h' "${SANDBOX}/chown.log" && echo 1 || echo 0)"
    check "symlink: chown given ./name, never an absolute path" "$(all_relative)" "$(chown_args | tr '\n' ' ')"

    # A library path that is itself a symlink is skipped, not walked.
    reset_logs
    OUT="$(run_reown 4242 4243 "${SANDBOX}/library-link")"
    check "symlink: a linked library root is skipped" "$([[ "$(calls_of chown)" == "0" && "$OUT" == *"is not a directory, skipped"* ]] && echo 1 || echo 0)" "$OUT"
fi

# ---------------------------------------------------------------------------
# 4. Paths that are missing, and ids that are not numbers
# ---------------------------------------------------------------------------

reset_logs
OUT="$(run_reown 99 100 "${SANDBOX}/does-not-exist")"; CODE=$?
check "missing dir: skipped, exit 0" "$([[ $CODE -eq 0 && "$(calls_of chown)" == "0" && "$OUT" == *"skipped"* ]] && echo 1 || echo 0)" "$OUT"

reset_logs
OUT="$(run_reown 'abc' 100 "$T1")"; CODE=$?
check "non-numeric PUID: nothing changed, exit 0" "$([[ $CODE -eq 0 && "$(calls_of chown)" == "0" && "$OUT" == *"not a number"* ]] && echo 1 || echo 0)" "$OUT"

# ---------------------------------------------------------------------------
# 5. The entrypoint: PUID/PGID unset still means 1001:1001, as before
# ---------------------------------------------------------------------------

# A copy of the entrypoint beside a fake docker-reown.sh that records its
# arguments, so the real /app/jobs and /cache of this machine are never walked.
EP="${SANDBOX}/ep"
mkdir -p "$EP"
cp "$ENTRYPOINT" "$EP/docker-entrypoint.sh"
cat > "$EP/docker-reown.sh" <<EOF
#!/bin/sh
for a in "\$@"; do printf '%s\n' "\$a"; done > "${SANDBOX}/reown.args"
EOF
run_ep() { env -u STEMDECK_SSL_CERT -u STEMDECK_SSL_KEY PATH="${FAKEBIN}:${PATH}" "$@" sh "$EP/docker-entrypoint.sh" uvicorn app.main:app 2>&1; }
# The AMD branch hands off through setpriv rather than gosu.
dropper() { if [[ -e /dev/kfd ]]; then echo setpriv; else echo gosu; fi }

reset_logs; rm -f "${SANDBOX}/reown.args"
run_ep env -u PUID -u PGID >/dev/null; CODE=$?
check "entrypoint unset: exit 0" "$([[ $CODE -eq 0 ]] && echo 1 || echo 0)" "got $CODE"
check "entrypoint unset: re-owns /app/jobs and /cache as 1001:1001" \
  "$([[ "$(cat "${SANDBOX}/reown.args" 2>/dev/null | tr '\n' ' ')" == "1001 1001 /app/jobs /cache " ]] && echo 1 || echo 0)" \
  "got: $(tr '\n' ' ' < "${SANDBOX}/reown.args" 2>/dev/null)"
if [[ "$(dropper)" == "gosu" ]]; then
    check "entrypoint unset: drops to 1001:1001 and runs the CMD" \
      "$([[ "$(grep -v '^CALL$' "${SANDBOX}/gosu.log" 2>/dev/null | tr '\n' ' ')" == "1001:1001 uvicorn app.main:app " ]] && echo 1 || echo 0)" \
      "got: $(tr '\n' ' ' < "${SANDBOX}/gosu.log" 2>/dev/null)"
fi

reset_logs; rm -f "${SANDBOX}/reown.args"
run_ep env PUID=99 PGID=100 >/dev/null
check "entrypoint PUID=99 PGID=100: re-owns as 99:100" \
  "$([[ "$(cat "${SANDBOX}/reown.args" 2>/dev/null | tr '\n' ' ')" == "99 100 /app/jobs /cache " ]] && echo 1 || echo 0)" \
  "got: $(tr '\n' ' ' < "${SANDBOX}/reown.args" 2>/dev/null)"
if [[ "$(dropper)" == "gosu" ]]; then
    check "entrypoint PUID=99 PGID=100: drops to 99:100" \
      "$(grep -qx '99:100' "${SANDBOX}/gosu.log" 2>/dev/null && echo 1 || echo 0)"
fi

# ---------------------------------------------------------------------------

echo
echo "$((PASS))/$((PASS + FAIL)) checks passed, ${SKIP} skipped"
[[ $FAIL -eq 0 ]] || exit 1
