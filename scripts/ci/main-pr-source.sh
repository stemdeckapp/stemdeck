#!/usr/bin/env bash
#
# Decides whether a pull request may merge into main (#1086).
#
# main receives released code only. Two sources are allowed:
#   - next-release, the integration branch, at release time;
#   - hotfix/<name>, an urgent fix to a released version.
# Both must be branches of this repository. A fork can name its branch
# next-release, so the branch name alone proves nothing.
#
# Inputs, from the environment (main-pr-source.yml passes them from the event):
#   HEAD_REF   the pull request's source branch
#   HEAD_REPO  owner/name of the repository that branch lives in
#   BASE_REPO  owner/name of this repository
#
# Exit 0 when the source is allowed, 1 when it is not, 2 when an input is
# missing. Missing input refuses, so a broken event never reads as a pass.

set -euo pipefail

refuse() {
  if [ -n "${GITHUB_ACTIONS:-}" ]; then
    printf '::error title=main-pr-source::%s\n' "$1"
  fi
  printf 'main-pr-source: refused: %s\n' "$1" >&2
  exit "${2:-1}"
}

head_ref="${HEAD_REF:-}"
head_repo="${HEAD_REPO:-}"
base_repo="${BASE_REPO:-}"

[ -n "$head_ref" ] || refuse "HEAD_REF is empty." 2
[ -n "$head_repo" ] || refuse "HEAD_REPO is empty." 2
[ -n "$base_repo" ] || refuse "BASE_REPO is empty." 2

if [ "$head_repo" != "$base_repo" ]; then
  refuse "the source branch is in $head_repo, not $base_repo. Only next-release and hotfix/* branches of $base_repo merge into main."
fi

case "$head_ref" in
  next-release | hotfix/?*)
    printf 'main-pr-source: allowed: %s\n' "$head_ref"
    ;;
  *)
    refuse "'$head_ref' cannot merge into main. Open the pull request against next-release, or use a hotfix/* branch for an urgent fix to a released version."
    ;;
esac
