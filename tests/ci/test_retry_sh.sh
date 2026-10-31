#!/usr/bin/env bash
#
# Tests for scripts/ci/retry.sh (#838).
#
# The wrapper guards the e2e install steps, where a mistake is silent in the
# worst way: a retry that swallows a real failure, or a timeout that never
# fires, only shows up as a red or hung shard much later. Each case runs a tiny
# synthetic command in a throwaway directory, so nothing here touches the
# machine it runs on. Delays are 0 or 1 s so the whole file runs in seconds.
#
# Run:  bash tests/ci/test_retry_sh.sh

set -uo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
RETRY="${REPO_ROOT}/scripts/ci/retry.sh"

PASS=0
FAIL=0

ok()   { PASS=$((PASS + 1)); printf 'PASS  %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); printf 'FAIL  %s%s\n' "$1" "${2:+  -- $2}"; }
check(){ if [[ "$2" == "1" ]]; then ok "$1"; else bad "$1" "${3:-}"; fi }

SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

# A command that fails until it has been run <succeed_on> times, counting its
# runs in a file. It exits <code> on each failing run.
flaky() {  # flaky <name> <succeed_on> <code>
    local f="${SANDBOX}/$1"
    cat > "$f" <<EOF
#!/usr/bin/env bash
n=\$(( \$(cat "$f.count" 2>/dev/null || echo 0) + 1 ))
echo "\$n" > "$f.count"
[[ \$n -ge $2 ]] && exit 0
exit $3
EOF
    chmod +x "$f"
    printf '%s' "$f"
}
runs_of() { cat "${SANDBOX}/$1.count" 2>/dev/null || echo 0; }

# Unset so the cases do not depend on whether they run inside Actions.
run_retry() { env -u GITHUB_ACTIONS bash "$RETRY" "$@" 2>&1; }

# ---------------------------------------------------------------------------
# 1. Success on the first try
# ---------------------------------------------------------------------------

CMD="$(flaky first 1 1)"
OUT="$(run_retry --delay 0 -- "$CMD")"; CODE=$?
check "first try: exit 0" "$([[ $CODE -eq 0 ]] && echo 1 || echo 0)" "got $CODE"
check "first try: ran once" "$([[ "$(runs_of first)" == "1" ]] && echo 1 || echo 0)" "ran $(runs_of first)"
check "first try: attempt logged" "$([[ "$OUT" == *"attempt 1/3"* ]] && echo 1 || echo 0)"

# ---------------------------------------------------------------------------
# 2. Success on the third try, with every failed attempt logged
# ---------------------------------------------------------------------------

CMD="$(flaky third 3 7)"
OUT="$(run_retry --delay 0 -- "$CMD")"; CODE=$?
check "third try: exit 0" "$([[ $CODE -eq 0 ]] && echo 1 || echo 0)" "got $CODE"
check "third try: ran three times" "$([[ "$(runs_of third)" == "3" ]] && echo 1 || echo 0)" "ran $(runs_of third)"
check "third try: attempt 1 failure logged with its exit code" \
  "$([[ "$OUT" == *"attempt 1/3 failed with exit 7"* ]] && echo 1 || echo 0)"
check "third try: attempt 2 failure logged with its exit code" \
  "$([[ "$OUT" == *"attempt 2/3 failed with exit 7"* ]] && echo 1 || echo 0)"
check "third try: success logged" "$([[ "$OUT" == *"attempt 3/3 succeeded"* ]] && echo 1 || echo 0)"

# Inside Actions a retried success still leaves a warning annotation.
rm -f "${SANDBOX}/third.count"
OUT="$(env GITHUB_ACTIONS=true bash "$RETRY" --delay 0 -- "$CMD" 2>&1)"
check "third try: warning annotation in CI" \
  "$([[ "$OUT" == *"::warning::"*"needed 3 attempts"* ]] && echo 1 || echo 0)"

# ---------------------------------------------------------------------------
# 3. Failure after three tries, exit code preserved
# ---------------------------------------------------------------------------

CMD="$(flaky never 99 42)"
OUT="$(run_retry --delay 0 -- "$CMD")"; CODE=$?
check "always fails: exit code 42 preserved" "$([[ $CODE -eq 42 ]] && echo 1 || echo 0)" "got $CODE"
check "always fails: ran exactly three times" "$([[ "$(runs_of never)" == "3" ]] && echo 1 || echo 0)" "ran $(runs_of never)"
check "always fails: clear giving-up message" \
  "$([[ "$OUT" == *"giving up after 3 attempts, last exit 42"* ]] && echo 1 || echo 0)"

rm -f "${SANDBOX}/never.count"
OUT="$(env GITHUB_ACTIONS=true bash "$RETRY" --delay 0 -- "$CMD" 2>&1)"
check "always fails: error annotation in CI" "$([[ "$OUT" == *"::error::"* ]] && echo 1 || echo 0)"

# --attempts is honoured, and a different code is preserved too.
CMD="$(flaky twice 99 3)"
run_retry --attempts 2 --delay 0 -- "$CMD" >/dev/null; CODE=$?
check "--attempts 2: ran twice" "$([[ "$(runs_of twice)" == "2" ]] && echo 1 || echo 0)" "ran $(runs_of twice)"
check "--attempts 2: exit code 3 preserved" "$([[ $CODE -eq 3 ]] && echo 1 || echo 0)" "got $CODE"

# ---------------------------------------------------------------------------
# 4. A hung command is killed by the per-attempt timeout
# ---------------------------------------------------------------------------

# The child it starts must die too: the hang in CI is apt's download method, a
# grandchild of the step, not the command itself.
HANG="${SANDBOX}/hang"
cat > "$HANG" <<EOF
#!/usr/bin/env bash
echo \$\$ >> "$HANG.pids"
sleep 300 &
echo \$! >> "$HANG.pids"
wait
EOF
chmod +x "$HANG"
START=$SECONDS
OUT="$(run_retry --attempts 2 --timeout 1 --delay 0 -- "$HANG")"; CODE=$?
TOOK=$((SECONDS - START))
check "hang: killed, exit 124" "$([[ $CODE -eq 124 ]] && echo 1 || echo 0)" "got $CODE"
check "hang: both attempts bounded (took ${TOOK}s)" "$([[ $TOOK -lt 15 ]] && echo 1 || echo 0)"
check "hang: timeout logged" "$([[ "$OUT" == *"attempt 1/2 timed out"* ]] && echo 1 || echo 0)"
sleep 1
# A killed process whose parent died first is reparented to PID 1. In a
# container that PID 1 may never reap it, so it stays a zombie: dead, but
# kill -0 still finds it. Read the state from /proc where there is one.
alive() {
    if [[ -r "/proc/$1/stat" ]]; then
        [[ "$(awk '{print $3}' "/proc/$1/stat" 2>/dev/null)" != "Z" ]]
    else
        kill -0 "$1" 2>/dev/null
    fi
}
SEEN=0
ALIVE=0
while read -r pid; do
    SEEN=$((SEEN + 1))
    alive "$pid" && ALIVE=$((ALIVE + 1))
done < <(cat "$HANG.pids" 2>/dev/null)
check "hang: both attempts started the command and its child" \
  "$([[ $SEEN -eq 4 ]] && echo 1 || echo 0)" "saw $SEEN pids"
check "hang: no process from either attempt survives" "$([[ $ALIVE -eq 0 ]] && echo 1 || echo 0)" "$ALIVE alive"

# A command that is slow but finishes inside its limit is not cut short.
OUT="$(run_retry --timeout 5 --delay 0 -- sleep 1)"; CODE=$?
check "slow but in time: exit 0" "$([[ $CODE -eq 0 ]] && echo 1 || echo 0)" "got $CODE"

# ---------------------------------------------------------------------------
# 5. The pause grows between attempts
# ---------------------------------------------------------------------------

CMD="$(flaky pauses 99 1)"
OUT="$(run_retry --delay 1 -- "$CMD")"
check "pause: 1 s before attempt 2" "$([[ "$OUT" == *"waiting 1s"* ]] && echo 1 || echo 0)"
check "pause: 2 s before attempt 3" "$([[ "$OUT" == *"waiting 2s"* ]] && echo 1 || echo 0)"
check "pause: none after the last attempt" \
  "$([[ "$(grep -c 'waiting' <<< "$OUT")" == "2" ]] && echo 1 || echo 0)"

# ---------------------------------------------------------------------------
# 6. Arguments pass through untouched; bad usage is an error, not a run
# ---------------------------------------------------------------------------

OUT="$(run_retry --delay 0 -- printf '%s|' "a b" "c")"
check "args: spaces kept" "$([[ "$OUT" == *"a b|c|"* ]] && echo 1 || echo 0)" "got $OUT"

# Each attempt is told which one it is.
OUT="$(run_retry --delay 0 -- bash -c 'echo "n=$RETRY_ATTEMPT"; [ "$RETRY_ATTEMPT" -ge 2 ]')"; CODE=$?
check "RETRY_ATTEMPT: 1 then 2" \
  "$([[ $CODE -eq 0 && "$OUT" == *"n=1"*"n=2"* && "$OUT" != *"n=3"* ]] && echo 1 || echo 0)" "got $OUT"

run_retry --attempts 0 -- true >/dev/null; CODE=$?
check "usage: --attempts 0 rejected with exit 2" "$([[ $CODE -eq 2 ]] && echo 1 || echo 0)" "got $CODE"
run_retry --timeout x -- true >/dev/null; CODE=$?
check "usage: non-numeric --timeout rejected" "$([[ $CODE -eq 2 ]] && echo 1 || echo 0)" "got $CODE"
run_retry >/dev/null; CODE=$?
check "usage: no command rejected" "$([[ $CODE -eq 2 ]] && echo 1 || echo 0)" "got $CODE"

# ---------------------------------------------------------------------------

echo
echo "$((PASS))/$((PASS + FAIL)) checks passed"
[[ $FAIL -eq 0 ]] || exit 1
