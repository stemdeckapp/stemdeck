#!/usr/bin/env bash
#
# Run a command up to N times, each attempt bounded by its own timeout, with an
# increasing pause between attempts (#838).
#
# For CI steps that fail because the network or a mirror is slow, not because
# anything is wrong: a browser download, an apt install. Every attempt is
# logged with its exit code and duration, and a retry that ends in success
# still leaves a warning annotation, so a flaky step stays visible instead of
# quietly passing. When every attempt fails, the last exit code is returned
# unchanged (124 means the last attempt hit its timeout).
#
# Usage: retry.sh [--attempts N] [--timeout SECONDS] [--delay SECONDS] -- command [args...]
#   --attempts  how many times to try (default 3)
#   --timeout   limit for each attempt, in seconds (default 300); the command
#               and everything it started get SIGTERM, then SIGKILL 10 s later
#   --delay     pause before the second attempt, doubled before each later
#               one (default 10)
#
# The command sees RETRY_ATTEMPT (1, 2, ...), so a later attempt can do
# something different from the first, such as use another mirror.
#
# The timeout kills the command's whole process group, so run this script as
# the same user as the command: `sudo retry.sh -- apt-get ...`, not
# `retry.sh -- sudo apt-get ...`, or a root child survives the kill and keeps
# holding the dpkg lock.

set -uo pipefail

attempts=3
per_attempt=300
delay=10

usage() {
    echo "usage: retry.sh [--attempts N] [--timeout SECONDS] [--delay SECONDS] -- command [args...]" >&2
    exit 2
}

is_count() { [[ "$1" =~ ^[0-9]+$ ]]; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --attempts) is_count "${2:-}" && [[ "$2" -ge 1 ]] || usage; attempts="$2"; shift 2 ;;
        --timeout)  is_count "${2:-}" && [[ "$2" -ge 1 ]] || usage; per_attempt="$2"; shift 2 ;;
        --delay)    is_count "${2:-}" || usage; delay="$2"; shift 2 ;;
        --) shift; break ;;
        *) usage ;;
    esac
done
[[ $# -gt 0 ]] || usage

if command -v timeout >/dev/null 2>&1; then
    timeout_cmd=timeout
elif command -v gtimeout >/dev/null 2>&1; then
    timeout_cmd=gtimeout
else
    echo "retry: no 'timeout' command (GNU coreutils) on PATH" >&2
    exit 2
fi

annotate() {  # annotate <level> <message>: a GitHub annotation in CI; outside CI the stderr line says it already
    if [[ -n "${GITHUB_ACTIONS:-}" ]]; then
        echo "::$1::$2"
    fi
}

cmd_text="$*"
pause="$delay"
code=0

for ((n = 1; n <= attempts; n++)); do
    echo "retry: attempt $n/$attempts (timeout ${per_attempt}s): $cmd_text" >&2
    start=$SECONDS
    RETRY_ATTEMPT=$n "$timeout_cmd" --kill-after=10 "$per_attempt" "$@"
    code=$?
    took=$((SECONDS - start))
    if [[ $code -eq 0 ]]; then
        echo "retry: attempt $n/$attempts succeeded after ${took}s" >&2
        if [[ $n -gt 1 ]]; then
            annotate warning "retry: '$cmd_text' needed $n attempts; see the log for why the earlier ones failed"
        fi
        exit 0
    fi
    if [[ $code -eq 124 || $code -eq 137 ]]; then
        echo "retry: attempt $n/$attempts timed out after ${took}s (exit $code)" >&2
    else
        echo "retry: attempt $n/$attempts failed with exit $code after ${took}s" >&2
    fi
    if [[ $n -lt $attempts ]]; then
        echo "retry: waiting ${pause}s before the next attempt" >&2
        sleep "$pause"
        pause=$((pause * 2))
    fi
done

echo "retry: giving up after $attempts attempts, last exit $code: $cmd_text" >&2
annotate error "retry: '$cmd_text' failed $attempts times, last exit $code"
exit "$code"
