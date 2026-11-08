#!/usr/bin/env bash
#
# Tests for scripts/release/tag.sh (#1039).
#
# Each case runs the script in a throwaway clone of a throwaway bare
# repository, so nothing here touches the real repository or its remote.
#
# Run:  bash tests/ci/test_release_tag_sh.sh

set -uo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
TAG_SH="${REPO_ROOT}/scripts/release/tag.sh"

PASS=0
FAIL=0
ok()   { PASS=$((PASS + 1)); printf 'PASS  %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); printf 'FAIL  %s%s\n' "$1" "${2:+  -- $2}"; }
check(){ if [[ "$2" == "1" ]]; then ok "$1"; else bad "$1" "${3:-}"; fi }

SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1

# A bare "remote" with main and big-release, and a fresh clone per case.
git init -q --bare "$SANDBOX/remote.git"
git init -q -b main "$SANDBOX/seed"
(
  cd "$SANDBOX/seed" &&
    echo one > file.txt && git add file.txt && git commit -q -m one &&
    git checkout -q -b big-release && echo two >> file.txt && git commit -q -am two &&
    git push -q "$SANDBOX/remote.git" main big-release
)

fresh() {  # fresh <branch>: prints the path of a new clone on <branch>
  # Runs in a command substitution, so a counter would not survive; a
  # unique temp directory name does.
  local d
  d="$(mktemp -d "$SANDBOX/clone.XXXXXX")"
  rmdir "$d"
  git clone -q -b "$1" "$SANDBOX/remote.git" "$d"
  printf '%s' "$d"
}
run_tag() {  # run_tag <dir> <answer> <args...>
  local d="$1" answer="$2"
  shift 2
  (cd "$d" && printf '%s\n' "$answer" | bash "$TAG_SH" "$@" 2>&1)
}
local_tag() { git -C "$1" rev-parse --verify --quiet "refs/tags/$2" >/dev/null; }
remote_tag() { [ -n "$(git -C "$SANDBOX/remote.git" tag -l "$1")" ]; }

# ---------------------------------------------------------------------------
# 1. The regex is the workflow guard's, character for character
# ---------------------------------------------------------------------------

pat() { grep -o "pattern='[^']*'\|PATTERN='[^']*'" "$1" | head -1 | sed "s/^[A-Za-z]*='//; s/'$//"; }
SCRIPT_PAT="$(pat "$TAG_SH")"
for wf in release-candidate-guard.yml release-tag-draft.yml; do
  WF_PAT="$(pat "$REPO_ROOT/.github/workflows/$wf")"
  check "regex matches $wf" "$([ -n "$SCRIPT_PAT" ] && [ "$SCRIPT_PAT" = "$WF_PAT" ] && echo 1 || echo 0)" \
    "script '$SCRIPT_PAT' vs '$WF_PAT'"
done

# ---------------------------------------------------------------------------
# 2. Refusals
# ---------------------------------------------------------------------------

D="$(fresh main)"
for name in v1.02.0 v1.0 1.0.0 v1.0.0-rc1 v1.0.0-RC.1 v1.0.0-rc.01 'v1.0.0;id' 'v1.0.0 '; do
  OUT="$(run_tag "$D" tag "$name")"; CODE=$?
  check "bad name '$name' refused" "$([ $CODE -eq 1 ] && [[ "$OUT" == *"is not a release tag"* ]] && echo 1 || echo 0)" "exit $CODE: $OUT"
done
check "bad names created no tag" "$([ -z "$(git -C "$D" tag -l)" ] && echo 1 || echo 0)"

D="$(fresh main)"
echo dirty >> "$D/file.txt"
OUT="$(run_tag "$D" tag v1.0.0)"; CODE=$?
check "dirty tracked file refused" "$([ $CODE -eq 1 ] && [[ "$OUT" == *"uncommitted changes"* ]] && echo 1 || echo 0)" "exit $CODE: $OUT"
git -C "$D" add file.txt
OUT="$(run_tag "$D" tag v1.0.0)"; CODE=$?
check "staged change refused" "$([ $CODE -eq 1 ] && [[ "$OUT" == *"uncommitted changes"* ]] && echo 1 || echo 0)" "exit $CODE"

D="$(fresh main)"
git -C "$D" checkout -q -b feature
OUT="$(run_tag "$D" tag v1.0.0)"; CODE=$?
check "wrong branch refused" "$([ $CODE -eq 1 ] && [[ "$OUT" == *"current branch is 'feature'"* ]] && echo 1 || echo 0)" "exit $CODE: $OUT"
git -C "$D" checkout -q --detach main
OUT="$(run_tag "$D" tag v1.0.0)"; CODE=$?
check "detached HEAD refused" "$([ $CODE -eq 1 ] && [[ "$OUT" == *"detached"* ]] && echo 1 || echo 0)" "exit $CODE: $OUT"

D="$(fresh main)"
(cd "$D" && echo local >> file.txt && git commit -q -am "not pushed")
OUT="$(run_tag "$D" tag v1.0.0)"; CODE=$?
HEAD_SHA="$(git -C "$D" rev-parse HEAD)"; TIP_SHA="$(git -C "$SANDBOX/remote.git" rev-parse main)"
check "HEAD ahead of the remote tip refused" "$([ $CODE -eq 1 ] && [[ "$OUT" == *"not the tip of origin/main"* ]] && echo 1 || echo 0)" "exit $CODE: $OUT"
check "both shas printed" "$([[ "$OUT" == *"$HEAD_SHA"* && "$OUT" == *"$TIP_SHA"* ]] && echo 1 || echo 0)"

D="$(fresh main)"
git -C "$SANDBOX/seed" checkout -q main
(cd "$SANDBOX/seed" && echo three >> file.txt && git commit -q -am three && git push -q "$SANDBOX/remote.git" main)
OUT="$(run_tag "$D" tag v1.0.0)"; CODE=$?
check "HEAD behind the remote tip refused (after fetch)" "$([ $CODE -eq 1 ] && [[ "$OUT" == *"not the tip of origin/main"* ]] && echo 1 || echo 0)" "exit $CODE: $OUT"

D="$(fresh main)"
git -C "$D" tag v1.1.0
OUT="$(run_tag "$D" tag v1.1.0)"; CODE=$?
check "existing local tag refused" "$([ $CODE -eq 1 ] && [[ "$OUT" == *"already exists locally"* ]] && echo 1 || echo 0)" "exit $CODE: $OUT"

D="$(fresh main)"
git -C "$SANDBOX/seed" tag v1.2.0 && git -C "$SANDBOX/seed" push -q "$SANDBOX/remote.git" v1.2.0
git -C "$D" tag -d v1.2.0 >/dev/null 2>&1 || true
OUT="$(run_tag "$D" tag v1.2.0)"; CODE=$?
check "existing remote tag refused" "$([ $CODE -eq 1 ] && [[ "$OUT" == *"already exists on origin"* ]] && echo 1 || echo 0)" "exit $CODE: $OUT"

D="$(fresh main)"
OUT="$(run_tag "$D" yes v1.3.0)"; CODE=$?
check "answer other than 'tag' refused" "$([ $CODE -eq 1 ] && [[ "$OUT" == *"Nothing was created"* ]] && echo 1 || echo 0)" "exit $CODE: $OUT"
check "  and no tag was created" "$(! local_tag "$D" v1.3.0 && echo 1 || echo 0)"
OUT="$(cd "$D" && bash "$TAG_SH" v1.3.0 </dev/null 2>&1)"; CODE=$?
check "no answer at all refused" "$([ $CODE -eq 1 ] && ! local_tag "$D" v1.3.0 && echo 1 || echo 0)" "exit $CODE"

OUT="$(run_tag "$D" tag)"; CODE=$?
check "no tag argument: usage, exit 2" "$([ $CODE -eq 2 ] && echo 1 || echo 0)" "exit $CODE"
OUT="$(run_tag "$D" tag v1.3.0 --force)"; CODE=$?
check "unknown option: usage, exit 2" "$([ $CODE -eq 2 ] && echo 1 || echo 0)" "exit $CODE"

# ---------------------------------------------------------------------------
# 3. Success
# ---------------------------------------------------------------------------

D="$(fresh main)"
echo note > "$D/untracked.txt"
OUT="$(run_tag "$D" tag v1.4.0)"; CODE=$?
check "main, clean: tag created" "$([ $CODE -eq 0 ] && local_tag "$D" v1.4.0 && echo 1 || echo 0)" "exit $CODE: $OUT"
check "  untracked files do not block" "$([ $CODE -eq 0 ] && echo 1 || echo 0)"
check "  the tag is annotated" "$([ "$(git -C "$D" cat-file -t v1.4.0)" = tag ] && echo 1 || echo 0)"
check "  on HEAD" "$([ "$(git -C "$D" rev-parse 'v1.4.0^{commit}')" = "$(git -C "$D" rev-parse HEAD)" ] && echo 1 || echo 0)"
check "  not pushed without --push" "$(! remote_tag v1.4.0 && echo 1 || echo 0)"
check "  says what it will tag first" "$([[ "$OUT" == *"Will create the annotated tag v1.4.0"* ]] && echo 1 || echo 0)"

D="$(fresh big-release)"
OUT="$(run_tag "$D" tag v1.5.0-rc.1 --push)"; CODE=$?
check "big-release with --push: tag created and pushed" "$([ $CODE -eq 0 ] && remote_tag v1.5.0-rc.1 && echo 1 || echo 0)" "exit $CODE: $OUT"
check "  remote tag points at the big-release tip" \
  "$([ "$(git -C "$SANDBOX/remote.git" rev-parse 'v1.5.0-rc.1^{commit}')" = "$(git -C "$SANDBOX/remote.git" rev-parse big-release)" ] && echo 1 || echo 0)"

# ---------------------------------------------------------------------------

echo
echo "$((PASS))/$((PASS + FAIL)) checks passed"
[[ $FAIL -eq 0 ]] || exit 1
