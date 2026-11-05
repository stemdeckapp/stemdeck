#!/usr/bin/env bash
#
# Tests for build/docker-reown.sh and the entrypoint that calls it (#994).
#
# The container entrypoint used to `chown -R` the whole library on every start.
# docker-reown.sh changes only the entries whose owner or group is wrong, and it
# runs as root. A mistake here is silent: a walk that follows a symlink out of
# the library re-owns files that are not StemDeck's, a chown given an absolute
# path can be redirected by a swapped parent directory, and one that skips too
# much leaves a root-owned file the app can no longer write. No Docker is
# needed: each case runs the real script against a throwaway tree, with a fake
# chown on PATH that records the directory it ran in and its arguments. Nothing
# outside the sandbox is touched.
#
# Some cases need two owners to tell apart. As root (the CI lint container)
# they are made with a real chown; otherwise with a second group the user
# belongs to. Cases that need that, or symlinks, are reported as skipped when
# the machine cannot provide them, and as failed in CI, where it always can.
#
# Run:  bash tests/ci/test_docker_reown_sh.sh

set -uo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
REOWN="${REPO_ROOT}/build/docker-reown.sh"
ENTRYPOINT="${REPO_ROOT}/build/docker-entrypoint.sh"
REAL_CHOWN="$(command -v chown)"

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
yes_if() { if "$@"; then echo 1; else echo 0; fi }

# The physical path, so it matches what the fake chown sees with pwd -P.
SANDBOX="$(cd -- "$(mktemp -d)" && pwd -P)"
trap 'rm -rf "$SANDBOX"' EXIT

# Fake commands. gosu, setpriv and touch record their arguments, one per line,
# after a CALL line. chown records CALL, the directory it ran in and each
# argument, NUL-separated, so a file name with a newline in it stays one
# argument. FAKE_CHOWN=fail fails on every entry the way a read-only mount
# does; FAKE_CHOWN=apply-fail runs the real chown and then reports failure.
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
{ printf 'CALL\0CWD %s\0' "\$(pwd -P)"; for a in "\$@"; do printf '%s\0' "\$a"; done; } >> "${SANDBOX}/chown.log"
case "\${FAKE_CHOWN:-}" in
    fail)
        for a in "\$@"; do
            case "\$a" in ./*) echo "chown: changing ownership of '\$a': Read-only file system" >&2 ;; esac
        done
        exit 1 ;;
    apply-fail)
        "${REAL_CHOWN}" "\$@"
        exit 1 ;;
esac
exit 0
EOF
chmod +x "${FAKEBIN}/chown"
# stat is real, but counts how often it was started, so a test can tell one
# stat per batch from one per entry.
REAL_STAT="$(command -v stat)"
cat > "${FAKEBIN}/stat" <<EOF
#!/bin/sh
echo CALL >> "${SANDBOX}/stat.log"
exec "${REAL_STAT}" "\$@"
EOF
chmod +x "${FAKEBIN}/stat"
reset_logs() { rm -f "${SANDBOX}"/*.log; }
calls_of() { local n; n="$({ tr '\0' '\n' < "${SANDBOX}/$1.log"; } 2>/dev/null | grep -c '^CALL$')"; echo "${n:-0}"; }

# Reads chown.log. ARGS: the path arguments as given. RESOLVED: each one
# resolved against the directory chown ran in. OPTS: everything else.
parse_chown() {
    ARGS=(); RESOLVED=(); OPTS=()
    local cwd="" a
    [[ -f "${SANDBOX}/chown.log" ]] || return 0
    while IFS= read -r -d '' a; do
        if [[ "$a" == CALL ]]; then continue
        elif [[ "$a" == "CWD "* ]]; then cwd="${a#CWD }"
        elif [[ "$a" == ./* ]]; then ARGS+=("$a"); RESOLVED+=("${cwd}/${a#./}")
        elif [[ "$a" =~ ^[0-9]+:[0-9]+$ || "$a" == -* ]]; then OPTS+=("$a")
        else ARGS+=("$a"); RESOLVED+=("$a")
        fi
    done < "${SANDBOX}/chown.log"
}
chowned() { parse_chown; [[ ${#RESOLVED[@]} -eq 0 ]] || printf '%s\n' "${RESOLVED[@]}" | sort; }
chown_args_line() { parse_chown; printf '%s ' "${ARGS[@]}"; }
has_opt() { parse_chown; local o; for o in "${OPTS[@]}"; do [[ "$o" == "$1" ]] && return 0; done; return 1; }
# 1 when every path argument is ./name: one component, relative to the
# directory find changed into. An absolute path would make chown resolve every
# parent again, which a second writer on the share can swap for a symlink.
all_relative() {
    parse_chown
    [[ ${#ARGS[@]} -gt 0 ]] || { echo 0; return; }
    local a
    for a in "${ARGS[@]}"; do [[ "$a" =~ ^\./[^/]+$ ]] || { echo 0; return; }; done
    echo 1
}

run_reown() { PATH="${FAKEBIN}:${PATH}" sh "$REOWN" "$@" 2>&1; }

# A small library: two songs, nested folders, a few files. 10 entries.
make_tree() {  # make_tree <dir>
    mkdir -p "$1/song-a/stems" "$1/song-b/stems"
    : > "$1/registry.json"
    : > "$1/song-a/source.wav"
    : > "$1/song-a/stems/vocals.wav"
    : > "$1/song-a/stems/drums.wav"
    : > "$1/song-b/stems/bass.wav"
}
owner_of() { stat -c '%u:%g' "$1"; }

# How this machine can give an entry an owner other than the tree's: as root a
# real chown to 4242:4242, otherwise a second group of this user.
OWNER_MODE=""
OTHER_GROUP=""
PROBE="${SANDBOX}/probe"
: > "$PROBE"
if [[ "$(id -u)" == "0" ]]; then
    OWNER_MODE=root
else
    for g in $(id -G); do
        [[ "$g" == "$(stat -c %g "$PROBE")" ]] || { OTHER_GROUP="$g"; break; }
    done
    if [[ -n "$OTHER_GROUP" ]] && chgrp -h "$OTHER_GROUP" "$PROBE" 2>/dev/null \
        && [[ "$(stat -c %g "$PROBE")" == "$OTHER_GROUP" ]]; then
        OWNER_MODE=group
    fi
fi
make_wrong() {  # make_wrong <path...>: give each its own owner, never followed
    if [[ "$OWNER_MODE" == root ]]; then "$REAL_CHOWN" -h 4242:4242 "$@"
    else chgrp -h "$OTHER_GROUP" "$@"
    fi
}
NO_OWNERS="not root and no second group to tell owners apart"

# Git for Windows copies instead of linking unless told otherwise.
link() { MSYS=winsymlinks:nativestrict ln -s "$1" "$2" 2>/dev/null; [[ -L "$2" ]]; }
NO_LINKS="this filesystem cannot make a symlink"

# ---------------------------------------------------------------------------
# 1. A tree that is already owned right: no chown at all, ctime untouched
# ---------------------------------------------------------------------------

T1="${SANDBOX}/owned"
make_tree "$T1"
IFS=: read -r U1 G1 <<< "$(owner_of "$T1")"
reset_logs
OUT="$(run_reown "$U1" "$G1" "$T1")"
check "owned: no chown call" "$(yes_if [ "$(calls_of chown)" = 0 ])" "$(calls_of chown) calls"
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

mixed_tree() {  # mixed_tree <dir>: make_tree with three entries owned wrong
    make_tree "$1"
    WRONG=("$1/song-a/stems" "$1/song-a/stems/vocals.wav" "$1/song-b/stems/bass.wav")
    make_wrong "${WRONG[@]}"
}
if [[ -z "$OWNER_MODE" ]]; then
    skip "mixed: only mismatched entries re-owned" "$NO_OWNERS"
else
    T2="${SANDBOX}/mixed"
    mixed_tree "$T2"
    IFS=: read -r U2 G2 <<< "$(owner_of "$T2")"
    reset_logs
    OUT="$(run_reown "$U2" "$G2" "$T2")"
    EXPECT="$(printf '%s\n' "${WRONG[@]}" | sort)"
    check "mixed: exactly the mismatched entries re-owned" "$([[ "$(chowned)" == "$EXPECT" ]] && echo 1 || echo 0)" "got: $(chowned | tr '\n' ' ')"
    check "mixed: chown told not to follow links (-h)" "$(yes_if has_opt -h)"
    check "mixed: chown given the target owner" "$(yes_if has_opt "${U2}:${G2}")"
    check "mixed: logs the count" "$([[ "$OUT" == *"re-owned 3 of 10 entries to ${U2}:${G2} in "*" ms"* ]] && echo 1 || echo 0)" "$OUT"
    check "mixed: chown given ./name, never an absolute path" "$(all_relative)" "$(chown_args_line)"

    # Leading zeros: 0099 must compare as 99, or a chown that worked but
    # reported failure would count every entry as failed.
    T2Z="${SANDBOX}/mixed-zeros"
    mixed_tree "$T2Z"
    reset_logs
    OUT="$(FAKE_CHOWN=apply-fail run_reown "00${U2}" "0${G2}" "$T2Z")"
    check "leading zeros: entries chown did change are not counted as failed" \
      "$([[ "$OUT" == *"re-owned 3 of 10 entries to ${U2}:${G2}"* && "$OUT" != *failed* ]] && echo 1 || echo 0)" "$OUT"
fi

# ---------------------------------------------------------------------------
# 3. chown acts from inside each directory, and its failures are counted
# ---------------------------------------------------------------------------

T4="${SANDBOX}/wrong"
make_tree "$T4"
ALL4="$(find "$T4" | sort)"
# An owner nothing has, so every entry counts as wrong. Runs as any user.
reset_logs
OUT="$(run_reown 4242 4243 "$T4")"
check "execdir: chown given ./name, never an absolute path" "$(all_relative)" "$(chown_args_line)"
check "execdir: every entry re-owned, resolved from its own directory" \
  "$([[ "$(chowned)" == "$ALL4" ]] && echo 1 || echo 0)" "got: $(chowned | tr '\n' ' ')"
check "execdir: logs the count" "$([[ "$OUT" == *"re-owned 10 of 10 entries to 4242:4243 in "*" ms"* && "$OUT" != *failed* ]] && echo 1 || echo 0)" "$OUT"

reset_logs
OUT="$(run_reown 004242 0004243 "$T4")"
check "leading zeros: stripped before use" \
  "$([[ "$(has_opt 4242:4243 && echo y)" == y && "$OUT" == *"entries to 4242:4243 in "* ]] && echo 1 || echo 0)" "$OUT"
reset_logs
OUT="$(run_reown 000 0 "$T4")"
# As root the tree is already 0:0 and chown is not called, so read the log.
check "leading zeros: a lone 0 stays 0" \
  "$([[ "$OUT" == *"owned by 0:0, nothing changed"* || "$OUT" == *"entries to 0:0 in "* ]] && echo 1 || echo 0)" "$OUT"

# A chown that fails on every entry, as on a read-only or root_squash mount.
reset_logs
OUT="$(FAKE_CHOWN=fail run_reown 4242 4243 "$T4")"; CODE=$?
check "chown fails: exit 0" "$(yes_if [ "$CODE" -eq 0 ])" "got $CODE"
check "chown fails: logs re-owned 0, failed 10" "$([[ "$OUT" == *"re-owned 0, failed 10 of 10 entries"* ]] && echo 1 || echo 0)" "$OUT"
check "chown fails: names a failing entry" "$([[ "$OUT" == *"stemdeck: ownership:   chown: "*"Read-only file system"* ]] && echo 1 || echo 0)" "$OUT"
check "chown fails: at most 5 error lines, the rest summed up" \
  "$([[ "$(grep -c 'Read-only file system' <<< "$OUT")" -eq 5 && "$OUT" == *"(5 more error lines not shown)"* ]] && echo 1 || echo 0)" "$OUT"

# A uid that passes the number check but that find refuses.
reset_logs
OUT="$(run_reown 99999999999999999999 100 "$T4")"; CODE=$?
check "uid find refuses: exit 0, no chown" "$([[ $CODE -eq 0 && "$(calls_of chown)" == 0 ]] && echo 1 || echo 0)" "$OUT"
check "uid find refuses: logs that it could not check" \
  "$([[ "$OUT" == *"could not be checked, ownership left unchanged"* && "$OUT" != *"already owned"* ]] && echo 1 || echo 0)" "$OUT"
check "uid find refuses: shows find's error" "$([[ "$OUT" == *"stemdeck: ownership:   "*"find"* ]] && echo 1 || echo 0)" "$OUT"

# ---------------------------------------------------------------------------
# 4. PATH: relative, empty and glob entries
# ---------------------------------------------------------------------------

# A relative or empty entry would make find refuse -execdir.
reset_logs
OUT="$(PATH="${FAKEBIN}::.:${PATH}" sh "$REOWN" 4242 4243 "$T4" 2>&1)"
check "relative PATH entries: dropped, the walk still runs" \
  "$([[ "$OUT" == *"re-owned 10 of 10 entries"* && "$(all_relative)" == 1 ]] && echo 1 || echo 0)" "$OUT"

# A glob in an entry must stay literal. Expanded, ${SANDBOX}/g* would put
# ${SANDBOX}/gbin, and the chown in it, ahead of every other entry.
mkdir -p "${SANDBOX}/gbin"
printf '#!/bin/sh\n: > "%s/GLOBBED"\nexit 0\n' "$SANDBOX" > "${SANDBOX}/gbin/chown"
chmod +x "${SANDBOX}/gbin/chown"
reset_logs
OUT="$(PATH="${SANDBOX}/g*:${FAKEBIN}:${PATH}" sh "$REOWN" 4242 4243 "$T4" 2>&1)"
check "glob PATH entry: kept literal, not expanded" \
  "$([[ ! -e "${SANDBOX}/GLOBBED" && "$(calls_of chown)" -gt 0 ]] && echo 1 || echo 0)" "$OUT"

# ---------------------------------------------------------------------------
# 5. No temporary files: a missing or unwritable TMPDIR changes nothing
# ---------------------------------------------------------------------------

mkdir -p "${SANDBOX}/ro-tmp"
chmod 500 "${SANDBOX}/ro-tmp"
for tmp in "${SANDBOX}/no-such-dir" "${SANDBOX}/ro-tmp" unset; do
    reset_logs
    if [[ "$tmp" == unset ]]; then
        OUT="$(env -u TMPDIR PATH="${FAKEBIN}:${PATH}" sh "$REOWN" 4242 4243 "$T4" 2>&1)"
    else
        OUT="$(env TMPDIR="$tmp" PATH="${FAKEBIN}:${PATH}" sh "$REOWN" 4242 4243 "$T4" 2>&1)"
    fi
    check "TMPDIR ${tmp##*/}: the walk still runs" "$([[ "$OUT" == *"re-owned 10 of 10 entries"* ]] && echo 1 || echo 0)" "$OUT"
done
chmod 700 "${SANDBOX}/ro-tmp"

# ---------------------------------------------------------------------------
# 6. Hostile file names reach chown as one ./name argument each, and run nothing
# ---------------------------------------------------------------------------

T5="${SANDBOX}/hostile"
mkdir -p "$T5/song"
# shellcheck disable=SC2016
HOSTILE=("a b.wav" "-rf" '$(touch PWNED)' $'two\nlines.wav' "--" '*' "x;touch PWNED2")
MADE=()
for name in "${HOSTILE[@]}"; do
    if : > "$T5/song/$name" 2>/dev/null && [[ -e "$T5/song/$name" ]]; then
        MADE+=("$name")
    else
        skip "hostile name: $(printf '%q' "$name")" "this filesystem refuses the name"
    fi
done
reset_logs
OUT="$(cd "$SANDBOX" && run_reown 4242 4243 "$T5")"
parse_chown
for name in "${MADE[@]}"; do
    n=0
    for a in "${ARGS[@]}"; do [[ "$a" == "./$name" ]] && n=$((n + 1)); done
    check "hostile name $(printf '%q' "$name"): exactly one ./name argument" "$(yes_if [ "$n" -eq 1 ])" "seen $n times"
done
check "hostile names: every argument is ./name" "$(all_relative)"
check "hostile names: nothing ran" \
  "$([[ -z "$(find "$SANDBOX" "$REPO_ROOT" -maxdepth 4 -name 'PWNED*' 2>/dev/null)" ]] && echo 1 || echo 0)"
check "hostile names: every entry counted once" \
  "$([[ "$OUT" == *"re-owned $((${#MADE[@]} + 2)) of $((${#MADE[@]} + 2)) entries"* ]] && echo 1 || echo 0)" "$OUT"

# ---------------------------------------------------------------------------
# 7. A symlink pointing out of the library is not followed
# ---------------------------------------------------------------------------

T3="${SANDBOX}/linked"
OUTSIDE="${SANDBOX}/outside"
make_tree "$T3"
mkdir -p "$OUTSIDE/private"
: > "$OUTSIDE/private/not-stemdeck.txt"
if ! link "$OUTSIDE" "$T3/song-a/escape" || ! link "$OUTSIDE" "${SANDBOX}/library-link"; then
    skip "symlink: not followed" "$NO_LINKS"
else
    # An owner nothing has, so every entry in the tree counts as wrong.
    reset_logs
    OUT="$(run_reown 4242 4243 "$T3")"
    GOT="$(chowned)"
    check "symlink: the link itself is re-owned" "$(yes_if grep -qx -- "$T3/song-a/escape" <<< "$GOT")"
    check "symlink: nothing behind the link is touched" "$(grep -q -- "$OUTSIDE" <<< "$GOT" && echo 0 || echo 1)" "$GOT"
    check "symlink: nothing reached through the link" "$(grep -q -- "escape/" <<< "$GOT" && echo 0 || echo 1)" "$GOT"
    check "symlink: every library entry re-owned" "$(yes_if [ "$(wc -l <<< "$GOT")" -eq 11 ])" "$(wc -l <<< "$GOT") paths"
    check "symlink: chown told not to follow links (-h)" "$(yes_if has_opt -h)"
    check "symlink: chown given ./name, never an absolute path" "$(all_relative)" "$(chown_args_line)"

    # A library path that is itself a symlink is skipped, not walked.
    reset_logs
    OUT="$(run_reown 4242 4243 "${SANDBOX}/library-link")"
    check "symlink: a linked library root is skipped" "$([[ "$(calls_of chown)" == 0 && "$OUT" == *"is not a directory, skipped"* ]] && echo 1 || echo 0)" "$OUT"
fi

# The failure check reads the link, not its target: a link owned wrong whose
# target is owned right still counts as failed when chown fails on it.
if [[ -z "$OWNER_MODE" ]]; then
    skip "symlink failure counted on the link" "$NO_OWNERS"
else
    T6="${SANDBOX}/link-owner"
    make_tree "$T6"
    : > "${SANDBOX}/right-owner.txt"
    if ! link "${SANDBOX}/right-owner.txt" "$T6/song-a/link"; then
        skip "symlink failure counted on the link" "$NO_LINKS"
    else
        make_wrong "$T6/song-a/link"
        IFS=: read -r U6 G6 <<< "$(owner_of "$T6")"
        reset_logs
        OUT="$(FAKE_CHOWN=fail run_reown "$U6" "$G6" "$T6")"
        check "symlink failure: counted on the link, not its target" \
          "$([[ "$OUT" == *"re-owned 0, failed 1 of 11 entries"* ]] && echo 1 || echo 0)" "$OUT"
    fi
fi

# ---------------------------------------------------------------------------
# 8. Paths that are missing, and ids that are not numbers
# ---------------------------------------------------------------------------

reset_logs
OUT="$(run_reown 99 100 "${SANDBOX}/does-not-exist")"; CODE=$?
check "missing dir: skipped, exit 0" "$([[ $CODE -eq 0 && "$(calls_of chown)" == 0 && "$OUT" == *"skipped"* ]] && echo 1 || echo 0)" "$OUT"

reset_logs
OUT="$(run_reown 'abc' 100 "$T1")"; CODE=$?
check "non-numeric PUID: nothing changed, exit 0" "$([[ $CODE -eq 0 && "$(calls_of chown)" == 0 && "$OUT" == *"not a number"* ]] && echo 1 || echo 0)" "$OUT"

# Longer than any real id: refused before the zero stripping, which grows with
# the square of the length (80k zeros took 23 s).
LONG="$(printf '%080000d' 0)"
reset_logs
START=$SECONDS
OUT="$(run_reown "$LONG" 100 "$T4")"; CODE=$?
TOOK=$((SECONDS - START))
check "PUID over 20 characters: refused, exit 0, no chown" \
  "$([[ $CODE -eq 0 && "$(calls_of chown)" == 0 && "$OUT" == *"longer than 20 characters"* ]] && echo 1 || echo 0)" "$OUT"
check "PUID over 20 characters: refused at once (took ${TOOK}s)" "$(yes_if [ "$TOOK" -lt 5 ])"
reset_logs
OUT="$(run_reown 4242 "000000000000000000000100" "$T4")"
check "PGID over 20 characters: refused" "$([[ "$OUT" == *"longer than 20 characters"* && "$(calls_of chown)" == 0 ]] && echo 1 || echo 0)" "$OUT"
reset_logs
OUT="$(run_reown 00000000000000004242 4243 "$T4")"
check "PUID of exactly 20 characters: accepted" "$([[ "$OUT" == *"entries to 4242:4243 in "* ]] && echo 1 || echo 0)" "$OUT"

# ---------------------------------------------------------------------------
# 8b. Every chown failing on a big library stays fast: one stat per batch
# ---------------------------------------------------------------------------

# A root_squash or read-only share fails every chown on every start, and the
# app waits for this pass. 3 folders of 1000 files: 3004 entries.
T7="${SANDBOX}/big"
for d in a b c; do
    mkdir -p "$T7/$d"
    (cd "$T7/$d" && touch f{0001..1000}.wav)
done
reset_logs
START=$SECONDS
OUT="$(FAKE_CHOWN=fail run_reown 4242 4243 "$T7")"; CODE=$?
TOOK=$((SECONDS - START))
STATS="$(calls_of stat)"
BATCHES="$(calls_of chown)"
check "big, every chown fails: exit 0, every entry counted failed" \
  "$([[ $CODE -eq 0 && "$OUT" == *"re-owned 0, failed 3004 of 3004 entries"* ]] && echo 1 || echo 0)" "$OUT"
check "big, every chown fails: one stat per chown batch, not per entry (${STATS} stats, ${BATCHES} batches)" \
  "$([ "$STATS" -ge 1 ] && [ "$STATS" -le "$BATCHES" ] && echo 1 || echo 0)"
check "big, every chown fails: finishes in under 120 s (took ${TOOK}s)" "$(yes_if [ "$TOOK" -lt 120 ])"

# ---------------------------------------------------------------------------
# 8c. With stdout closed the walk still runs
# ---------------------------------------------------------------------------

# A plain `exec 4>&1` with fd 1 closed ends a dash script before the walk.
reset_logs
PATH="${FAKEBIN}:${PATH}" sh "$REOWN" 4242 4243 "$T4" >&- 2>/dev/null; CODE=$?
check "stdout closed: exit 0 and the walk ran" "$([ "$CODE" -eq 0 ] && [ "$(calls_of chown)" -gt 0 ] && echo 1 || echo 0)" "exit $CODE, $(calls_of chown) chown calls"

# ---------------------------------------------------------------------------
# 9. The entrypoint: PUID/PGID unset still means 1001:1001, as before
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
reown_args() { tr '\n' ' ' < "${SANDBOX}/reown.args" 2>/dev/null; }

reset_logs; rm -f "${SANDBOX}/reown.args"
run_ep env -u PUID -u PGID >/dev/null; CODE=$?
check "entrypoint unset: exit 0" "$(yes_if [ "$CODE" -eq 0 ])" "got $CODE"
check "entrypoint unset: re-owns /app/jobs and /cache as 1001:1001" \
  "$([[ "$(reown_args)" == "1001 1001 /app/jobs /cache " ]] && echo 1 || echo 0)" "got: $(reown_args)"
if [[ "$(dropper)" == "gosu" ]]; then
    check "entrypoint unset: drops to 1001:1001 and runs the CMD" \
      "$([[ "$(grep -v '^CALL$' "${SANDBOX}/gosu.log" 2>/dev/null | tr '\n' ' ')" == "1001:1001 uvicorn app.main:app " ]] && echo 1 || echo 0)" \
      "got: $(tr '\n' ' ' < "${SANDBOX}/gosu.log" 2>/dev/null)"
fi

reset_logs; rm -f "${SANDBOX}/reown.args"
run_ep env PUID=99 PGID=100 >/dev/null
check "entrypoint PUID=99 PGID=100: re-owns as 99:100" \
  "$([[ "$(reown_args)" == "99 100 /app/jobs /cache " ]] && echo 1 || echo 0)" "got: $(reown_args)"
if [[ "$(dropper)" == "gosu" ]]; then
    check "entrypoint PUID=99 PGID=100: drops to 99:100" \
      "$(yes_if grep -qx '99:100' "${SANDBOX}/gosu.log")"
fi

# ---------------------------------------------------------------------------

echo
echo "$((PASS))/$((PASS + FAIL)) checks passed, ${SKIP} skipped"
[[ $FAIL -eq 0 ]] || exit 1
