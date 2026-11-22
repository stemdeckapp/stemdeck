#!/usr/bin/env bash
#
# Tests for scripts/ci/main-pr-source.sh (#1086).
#
# The script is the only thing that stops a work branch or a Dependabot
# update from merging into main. A wrong pass is silent: unreleased code
# lands on main and nobody notices until users run it. Each case runs the
# script with a synthetic event, so nothing here touches git or the network.
#
# Run:  bash tests/ci/test_main_pr_source_sh.sh

set -uo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="${REPO_ROOT}/scripts/ci/main-pr-source.sh"
REPO="stemdeckapp/stemdeck"

PASS=0
FAIL=0

ok()   { PASS=$((PASS + 1)); printf 'PASS  %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); printf 'FAIL  %s%s\n' "$1" "${2:+  -- $2}"; }

# expect <name> <exit code> <output pattern> <head ref> <head repo> <base repo>
expect() {
  local out code
  out="$(env -u GITHUB_ACTIONS HEAD_REF="$4" HEAD_REPO="$5" BASE_REPO="$6" bash "$SCRIPT" 2>&1)"
  code=$?
  if [ "$code" -eq "$2" ] && printf '%s' "$out" | grep -Eq -- "$3"; then
    ok "$1"
  else
    bad "$1" "exit $code: $out"
  fi
}

# Allowed sources.
expect "next-release is allowed"            0 "allowed: next-release"    next-release        "$REPO" "$REPO"
expect "hotfix/<name> is allowed"           0 "allowed: hotfix/crash"    hotfix/crash        "$REPO" "$REPO"
expect "nested hotfix name is allowed"      0 "allowed: hotfix/1090/a"   hotfix/1090/a       "$REPO" "$REPO"

# Refused branch names.
expect "feature branch is refused"          1 "cannot merge into main"   feat/timeline-tags  "$REPO" "$REPO"
expect "dependabot branch is refused"       1 "cannot merge into main"   dependabot/github_actions/x "$REPO" "$REPO"
expect "big-release is refused"             1 "cannot merge into main"   big-release         "$REPO" "$REPO"
expect "bare hotfix/ is refused"            1 "cannot merge into main"   hotfix/             "$REPO" "$REPO"
expect "hotfix without slash is refused"    1 "cannot merge into main"   hotfix              "$REPO" "$REPO"
expect "prefix lookalike is refused"        1 "cannot merge into main"   next-release-2      "$REPO" "$REPO"
expect "suffix lookalike is refused"        1 "cannot merge into main"   my/next-release     "$REPO" "$REPO"
expect "main itself is refused"             1 "cannot merge into main"   main                "$REPO" "$REPO"

# Forks: the right name from another repository proves nothing.
expect "fork next-release is refused"       1 "is in someone/stemdeck"   next-release        someone/stemdeck "$REPO"
expect "fork hotfix is refused"             1 "is in someone/stemdeck"   hotfix/x            someone/stemdeck "$REPO"

# Missing input fails closed, with its own exit code.
expect "empty HEAD_REF refuses"             2 "HEAD_REF is empty"        ""                  "$REPO" "$REPO"
expect "empty HEAD_REPO refuses"            2 "HEAD_REPO is empty"       next-release        ""      "$REPO"
expect "empty BASE_REPO refuses"            2 "BASE_REPO is empty"       next-release        "$REPO" ""

# Inside Actions, a refusal also writes an error annotation.
out="$(GITHUB_ACTIONS=true HEAD_REF=feat/x HEAD_REPO="$REPO" BASE_REPO="$REPO" bash "$SCRIPT" 2>&1)"
if printf '%s' "$out" | grep -q '^::error title=main-pr-source::'; then
  ok "refusal writes an Actions error annotation"
else
  bad "refusal writes an Actions error annotation" "$out"
fi

printf '\n%d/%d checks passed\n' "$PASS" "$((PASS + FAIL))"
[ "$FAIL" -eq 0 ]
