#!/usr/bin/env bash
#
# Time the e2e setup steps and write them to the job summary (#838), so the next
# stalled install is visible on the run page without opening a log.
#
#   setup-timing.sh begin <step>          at the top of a step
#   setup-timing.sh end <step> [note]     as its last line (note: cache hit/miss)
#   setup-timing.sh summary               once, in an `if: always()` step
#
# A step that failed, or was killed by its timeout-minutes, never reaches its
# `end` line; the summary shows it as "did not finish" with the time it had
# run until the summary step started.

set -euo pipefail

log="${RUNNER_TEMP:-/tmp}/e2e-setup-timing.tsv"

case "${1:-}" in
    begin)
        printf 'begin\t%s\t%s\t\n' "$2" "$(date +%s)" >> "$log"
        ;;
    end)
        printf 'end\t%s\t%s\t%s\n' "$2" "$(date +%s)" "${3:-}" >> "$log"
        ;;
    summary)
        out="${GITHUB_STEP_SUMMARY:-/dev/stdout}"
        now="$(date +%s)"
        {
            echo "### e2e setup, shard ${SHARD:-?}"
            echo
            echo "| Step | Seconds | Cache | Result |"
            echo "|---|---:|---|---|"
            if [[ -f "$log" ]]; then
                awk -F'\t' -v now="$now" '
                    $1 == "begin" { order[++n] = $2; start[$2] = $3 }
                    $1 == "end"   { stop[$2] = $3; note[$2] = $4 }
                    END {
                        for (i = 1; i <= n; i++) {
                            s = order[i]
                            if (s in stop)
                                printf "| %s | %d | %s | ok |\n", s, stop[s] - start[s], (note[s] == "" ? "-" : note[s])
                            else
                                printf "| %s | %d+ | - | did not finish |\n", s, now - start[s]
                        }
                    }' "$log"
            fi
        } >> "$out"
        ;;
    *)
        echo "usage: setup-timing.sh begin|end <step> [note] | summary" >&2
        exit 2
        ;;
esac
