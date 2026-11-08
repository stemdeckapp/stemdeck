#!/usr/bin/env bash
#
# Tests for the step scripts of .github/workflows/release-tag-draft.yml and
# release-candidate-guard.yml (#1039, #1051).
#
# The scripts are taken out of the workflow files as they are committed, so
# a change to a step is tested as written. They then run against:
#   - a throwaway bare git repository served over file://, for the tag shape,
#     peel and ancestry checks;
#   - a fake `gh` that keeps the releases in a JSON file, for the
#     ensure-draft step and the manual guard. It can fail the list, lag the
#     list behind a create, or create two releases at once.
# Nothing here talks to GitHub.
#
# Needs bash, git, jq and Python with PyYAML (or uv to supply PyYAML).
# Run:  bash tests/ci/test_release_tag_draft_sh.sh

set -uo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
WF="$REPO_ROOT/.github/workflows"

PASS=0
FAIL=0
SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT
OUT="$SANDBOX/out.txt"

check() {  # check <name> <expected exit> <actual exit> [extended regex the output must match]
  local good=1
  [ "$2" = "$3" ] || good=0
  if [ -n "${4:-}" ] && ! grep -qE -- "$4" "$OUT"; then good=0; fi
  if [ "$good" = 1 ]; then
    PASS=$((PASS + 1)); printf 'PASS  %s\n' "$1"
  else
    FAIL=$((FAIL + 1)); printf 'FAIL  %s (want exit %s, got %s)\n' "$1" "$2" "$3"; sed 's/^/      /' "$OUT"
  fi
}
ok_if() {  # ok_if <name> <command...>: passes when the command succeeds
  if "${@:2}"; then PASS=$((PASS + 1)); printf 'PASS  %s\n' "$1"
  else FAIL=$((FAIL + 1)); printf 'FAIL  %s\n' "$1"; fi
}

# ---------------------------------------------------------------------------
# Tools: Python with PyYAML for the extraction, and a jq that writes LF only
# (jq on Windows writes CRLF, which the Linux runners never see).
# ---------------------------------------------------------------------------

if python3 -c 'import yaml' >/dev/null 2>&1; then
  PY=(python3)
elif command -v uv >/dev/null 2>&1; then
  PY=(uv run --quiet --no-project --with pyyaml python)
else
  echo "needs Python with PyYAML, or uv" >&2
  exit 2
fi
REAL_JQ="$(command -v jq)" || { echo "needs jq" >&2; exit 2; }

BIN="$SANDBOX/bin"
SCRIPTS="$SANDBOX/scripts"
mkdir -p "$BIN" "$SCRIPTS"
printf '#!/usr/bin/env bash\n"%s" "$@" | tr -d '"'"'\\r'"'"'\nexit "${PIPESTATUS[0]}"\n' "$REAL_JQ" > "$BIN/jq"
chmod +x "$BIN/jq"

# Take each step's `run:` text out of the workflow files.
"${PY[@]}" - "$WF" "$SCRIPTS" <<'PYEOF' || { echo "could not extract the step scripts" >&2; exit 2; }
import pathlib, sys, yaml
wf, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
def steps(name, job):
    return yaml.safe_load((wf / name).read_text(encoding="utf-8"))["jobs"][job]["steps"]
def by_id(name, job, step_id):
    return next(s for s in steps(name, job) if s.get("id") == step_id)["run"]
(out / "tag.sh").write_text(by_id("release-tag-draft.yml", "ensure", "tag"), newline="\n")
(out / "draft.sh").write_text(by_id("release-tag-draft.yml", "ensure", "draft"), newline="\n")
(out / "guard.sh").write_text(by_id("release-candidate-guard.yml", "check", "check"), newline="\n")
PYEOF

# The fake gh. State lives in $STATE.
#   GH_FAIL_LIST=1  the release list fails
#   GH_RACE=1       a create makes two releases with the tag
#   GH_STALE=N      the first N lists after a create leave the new one out
#   GH_APPEAR=N     the Nth list call adds a draft for $GH_APPEAR_TAG (id 900),
#                   as if the other platform's job had just created it
cat > "$BIN/gh" <<'GHEOF'
#!/usr/bin/env bash
set -euo pipefail
[ "$1" = "api" ] || { echo "fake gh: only api" >&2; exit 2; }
shift
method=GET; jqf=""; path=""; declare -A F=()
while [ $# -gt 0 ]; do
  case "$1" in
    --paginate) ;;
    -X) method="$2"; shift ;;
    --jq) jqf="$2"; shift ;;
    -f|-F) k="${2%%=*}"; v="${2#*=}"; F[$k]="$v"; shift ;;
    *) path="$1" ;;
  esac
  shift
done
emit() { if [ -n "$jqf" ]; then jq -r "$jqf"; else cat; fi; }
R="$STATE/releases.json"
case "$method $path" in
  "GET repos/"*"/releases?per_page=100")
    [ -z "${GH_FAIL_LIST:-}" ] || { echo "HTTP 502" >&2; exit 1; }
    n=$(( $(cat "$STATE/lists" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$STATE/lists"
    if [ -n "${GH_APPEAR:-}" ] && [ "$n" -eq "$GH_APPEAR" ]; then
      jq --arg t "$GH_APPEAR_TAG" '. + [{id: 900, tag_name: $t, draft: true, prerelease: true, target_commitish: "x"}]' "$R" > "$R.tmp" && mv "$R.tmp" "$R"
    fi
    stale=$(cat "$STATE/stale" 2>/dev/null || echo 0)
    if [ "$stale" -gt 0 ]; then
      echo $((stale - 1)) > "$STATE/stale"
      jq --argjson hide "$(cat "$STATE/last_post")" 'map(select(.id != $hide))' "$R" | emit
    else
      emit < "$R"
    fi ;;
  "GET repos/"*"/releases/"*)
    id="${path##*/}"
    out="$(jq -c --argjson id "$id" '.[] | select(.id == $id)' "$R")"
    [ -n "$out" ] || { echo "HTTP 404" >&2; exit 1; }
    printf '%s\n' "$out" | emit ;;
  "POST repos/"*"/releases")
    echo "POST ${F[tag_name]} prerelease=${F[prerelease]} target=${F[target_commitish]}" >> "$STATE/posts.log"
    first=""
    for _ in $(seq "$([ -n "${GH_RACE:-}" ] && echo 2 || echo 1)"); do
      id=$(( $(jq 'map(.id) | max // 100' "$R") + 1 ))
      [ -n "$first" ] || first="$id"
      jq --argjson id "$id" --arg t "${F[tag_name]}" --arg c "${F[target_commitish]}" --argjson p "${F[prerelease]}" \
        '. + [{id: $id, tag_name: $t, draft: true, prerelease: $p, target_commitish: $c}]' "$R" > "$R.tmp" && mv "$R.tmp" "$R"
    done
    echo "$first" > "$STATE/last_post"
    echo "${GH_STALE:-0}" > "$STATE/stale"
    jq -c --argjson id "$first" '.[] | select(.id == $id)' "$R" | emit ;;
  "GET repos/"*"/git/matching-refs/tags/"*)
    if [ -f "$STATE/refs.json" ]; then cat "$STATE/refs.json"; else echo '[]'; fi | emit ;;
  "GET repos/"*"/commits/"*)
    key="${path#*/commits/}"; key="${key//\//_}"
    [ -f "$STATE/commits/$key" ] || { echo "HTTP 422" >&2; exit 1; }
    printf '{"sha":"%s"}' "$(cat "$STATE/commits/$key")" | emit ;;
  *) echo "fake gh: unhandled $method $path" >&2; exit 2 ;;
esac
GHEOF
chmod +x "$BIN/gh"
export PATH="$BIN:$PATH"

# ---------------------------------------------------------------------------
# A served repository: main c1-c2, big-release c1-c2-c3, and c4 off both.
# ---------------------------------------------------------------------------

export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
mkdir -p "$SANDBOX/srv/o"
git init -q --bare "$SANDBOX/srv/o/r.git"
git -C "$SANDBOX/srv/o/r.git" config uploadpack.allowFilter true
W="$SANDBOX/work"
git init -q -b main "$W"
git -C "$W" commit -q --allow-empty -m c1; C1=$(git -C "$W" rev-parse HEAD)
git -C "$W" commit -q --allow-empty -m c2; C2=$(git -C "$W" rev-parse HEAD)
git -C "$W" checkout -q -b big-release
git -C "$W" commit -q --allow-empty -m c3; C3=$(git -C "$W" rev-parse HEAD)
git -C "$W" checkout -q --detach "$C1"
git -C "$W" commit -q --allow-empty -m off; C4=$(git -C "$W" rev-parse HEAD)
git -C "$W" tag -a v1.0.0 -m v1.0.0 "$C2"       # annotated, on main
git -C "$W" tag v1.1.0-rc.1 "$C3"               # lightweight, on big-release only
git -C "$W" tag -a v1.2.0-rc.1 -m x "$C4"       # on neither branch
git -C "$W" tag v1.02.0 "$C2"; git -C "$W" tag v1.0.0-rc1 "$C2"; git -C "$W" tag v1.0.0-RC.1 "$C2"
git -C "$W" push -q "$SANDBOX/srv/o/r.git" main big-release --tags
TAGOBJ=$(git -C "$W" rev-parse v1.0.0)          # the annotated tag object, not a commit
SERVER="file://$(cd "$SANDBOX/srv" && (pwd -W 2>/dev/null || pwd))"

# ---------------------------------------------------------------------------
# 1. Tag shape, peel and ancestry
# ---------------------------------------------------------------------------

run_tag() {  # run_tag <tag> <github.sha> [event] [ref]
  local rt; rt="$(mktemp -d)"; : > "$rt/out"
  env GH_TOKEN=fake SERVER_URL="$SERVER" REPO=o/r EVENT="${3:-push}" REF="${4:-refs/tags/$1}" TAG="$1" RUN_SHA="$2" \
    RUNNER_TEMP="$rt" GITHUB_OUTPUT="$rt/out" bash "$SCRIPTS/tag.sh" > "$OUT" 2>&1
  local rc=$?; cat "$rt/out" >> "$OUT"; rm -rf "$rt"; return $rc
}

echo "== tag check =="
run_tag v1.0.0 "$C2"; check "annotated tag on main, github.sha the commit: passes" 0 $? "is on main\.|^commit=$C2$"
run_tag v1.0.0 "$TAGOBJ"; check "annotated tag, github.sha the tag object: peeled, passes" 0 $? "github.sha is a tag object; its commit is $C2"
grep -q "^commit=$C2$" "$OUT"; check "  hands the peeled commit to the build" 0 $?
run_tag v1.1.0-rc.1 "$C3"; check "lightweight rc tag on big-release only: passes" 0 $? "is on big-release\.|^commit=$C3$"
run_tag v1.2.0-rc.1 "$C4"; check "tag on neither branch: refused" 1 $? "on neither main nor big-release"
for bad in v1.02.0 v1.0.0-rc1 v1.0.0-RC.1 'v1.0.0;id' 'v1.0.0 '; do
  run_tag "$bad" "$C2"; check "bad tag '$bad': refused" 1 $? "not a release tag"
done
run_tag v1.0.0 "$C3"; check "tag on another commit than github.sha: refused" 1 $? "points at $C2, but this run builds $C3"
run_tag v1.0.0 "$(printf 'a%.0s' {1..40})"; check "github.sha not in the fetched history: refused" 1 $? "is not a commit or tag"
run_tag v1.0.0 "$C2" workflow_dispatch; check "not a push: refused" 1 $? "only runs for a tag push"
run_tag v1.0.0 "$C2" push refs/heads/v1.0.0; check "a branch named like a tag: refused" 1 $? "not a release tag"
run_tag v9.9.9 "$C2"; check "tag missing on the server: fetch fails closed" 1 $? "could not fetch"
git -C "$SANDBOX/srv/o/r.git" branch -q -m big-release gone
run_tag v1.0.0 "$C2"; check "big-release missing: fetch fails closed" 1 $? "could not fetch"
git -C "$SANDBOX/srv/o/r.git" branch -q -m gone big-release
run_tag v1.0.0 "$C2"
check "the base64 credential is masked" 0 0 "^::add-mask::$(printf 'x-access-token:%s' fake | base64 | tr -d '\n')$"

# ---------------------------------------------------------------------------
# 2. Ensure exactly one draft
# ---------------------------------------------------------------------------

new_state() { STATE="$(mktemp -d "$SANDBOX/state.XXXXXX")"; export STATE; mkdir -p "$STATE/commits"; echo "${1:-[]}" > "$STATE/releases.json"; }
run_draft() {  # run_draft <tag> [may_create, default true]
  local o; o="$(mktemp)"
  env GH_TOKEN=fake REPO=o/r TAG="$1" COMMIT="$C2" MAY_CREATE="${2:-true}" GITHUB_OUTPUT="$o" \
    LIST_WAIT_TRIES="${LIST_WAIT_TRIES:-5}" LIST_WAIT_SECONDS=0 bash "$SCRIPTS/draft.sh" > "$OUT" 2>&1
  local rc=$?; cat "$o" >> "$OUT"; rm -f "$o"; return $rc
}
posts() { if [ -f "$STATE/posts.log" ]; then wc -l < "$STATE/posts.log" | tr -d ' '; else echo 0; fi; }
releases() { jq length "$STATE/releases.json" | tr -d '\r'; }

echo "== ensure draft (Windows, may create) =="
new_state; run_draft v1.1.0-rc.1; check "none: creates the rc draft as a pre-release" 0 $? "^prerelease=true$"
check "  the id comes from the create response" 0 0 "^release_id=101$"
check "  build=true" 0 0 "^build=true$"
ok_if "  one POST, pre-release true, target the peeled commit" grep -q "prerelease=true target=$C2" "$STATE/posts.log"
run_draft v1.1.0-rc.1 false; check "macOS afterwards reuses it" 0 $? "^release_id=101$"
ok_if "  still one POST and one release" [ "$(posts)/$(releases)" = "1/1" ]
new_state; GH_STALE=3 run_draft v1.1.0-rc.1; check "list lags 3 reads after the create: waits, then builds" 0 $? "is in the release list"
ok_if "  no second POST" [ "$(posts)" = 1 ]
new_state; GH_STALE=99 LIST_WAIT_TRIES=3 run_draft v1.1.0-rc.1; check "list never shows it: refused, says how to re-run safely" 1 $? "reuses draft 101"
new_state; run_draft v1.0.0; check "none, final tag: draft is not a pre-release" 0 $? "^prerelease=false$"
new_state '[{"id":5,"tag_name":"v1.0.0","draft":false,"prerelease":false}]'
run_draft v1.0.0; check "published: exits 0 with build=false" 0 $? "^build=false$"
check "  green notice" 0 0 "::notice::Release v1.0.0 is already published"
ok_if "  no release id" bash -c "! grep -q '^release_id=' '$OUT'"
ok_if "  no POST" [ "$(posts)" = 0 ]
new_state '[{"id":5,"tag_name":"v1.0.0-rc.1","draft":false,"prerelease":true}]'
run_draft v1.0.0-rc.1; check "published pre-release: exits 0 with build=false" 0 $? "^build=false$"
new_state '[{"id":5,"tag_name":"v1.0.0","draft":true,"prerelease":false},{"id":6,"tag_name":"v1.0.0","draft":true,"prerelease":false}]'
run_draft v1.0.0; check "two drafts: refused, both ids named" 1 $? "ids: 5 6"
new_state '[{"id":5,"tag_name":"v1.0.0","draft":false,"prerelease":false},{"id":6,"tag_name":"v1.0.0","draft":true,"prerelease":false}]'
run_draft v1.0.0; check "published plus a draft: refused" 1 $? "2 releases"
new_state; GH_RACE=1 run_draft v1.1.0-rc.1; check "a create that leaves two: refused, the other named" 1 $? "ids: 102"
new_state '[{"id":7,"tag_name":"v1.1.0-rc.1","draft":true,"prerelease":false}]'
run_draft v1.1.0-rc.1; check "rc draft not a pre-release: refused" 1 $? "pre-release suffix"
new_state '[{"id":7,"tag_name":"v1.0.0","draft":true,"prerelease":true}]'
run_draft v1.0.0; check "final draft marked pre-release: kept" 0 $? "^prerelease=true$"
new_state '[{"id":7,"tag_name":"v1.0.0-rc.10","draft":true,"prerelease":true}]'
run_draft v1.0.0-rc.1; check "a similar tag is not a match: creates" 0 $? "in the list: 0"
new_state; GH_FAIL_LIST=1 run_draft v1.0.0; check "list fails: step fails" 1 $?
ok_if "  no POST" [ "$(posts)" = 0 ]
new_state; run_draft ""; check "no tag: refused" 1 $? "set no tag or commit"

echo "== ensure draft (macOS, never creates) =="
new_state; GH_APPEAR=3 GH_APPEAR_TAG=v1.1.0-rc.1 run_draft v1.1.0-rc.1 false
check "draft appears on the 3rd list: waits, then reuses it" 0 $? "^release_id=900$"
ok_if "  never POSTs" [ "$(posts)" = 0 ]
new_state; LIST_WAIT_TRIES=4 run_draft v1.1.0-rc.1 false
check "draft never appears: refused, names the Windows job" 1 $? "The Windows tag job creates the draft"
ok_if "  never POSTs, no release made" [ "$(posts)/$(releases)" = "0/0" ]
new_state '[{"id":5,"tag_name":"v1.0.0","draft":false,"prerelease":false}]'
run_draft v1.0.0 false; check "published: exits 0 with build=false" 0 $? "^build=false$"

# ---------------------------------------------------------------------------
# 3. The manual-run guard (#1028), with and without allow_final_draft
# ---------------------------------------------------------------------------

run_guard() {  # run_guard <tag> <allow_final_draft>
  local o; o="$(mktemp)"
  env GH_TOKEN=fake REPO=o/r TAG="$1" RUN_SHA="$C2" ALLOW_FINAL_DRAFT="$2" GITHUB_OUTPUT="$o" bash "$SCRIPTS/guard.sh" > "$OUT" 2>&1
  local rc=$?; cat "$o" >> "$OUT"; rm -f "$o"; return $rc
}
g_state() {  # g_state <tag> <draft> <prerelease>
  new_state "[{\"id\":9,\"tag_name\":\"$1\",\"draft\":$2,\"prerelease\":$3,\"target_commitish\":\"$C2\"}]"
  echo "$C2" > "$STATE/commits/$C2"
}

echo "== manual guard =="
g_state v1.0.0-rc.1 true true; run_guard v1.0.0-rc.1 false; check "Linux: draft pre-release passes" 0 $? "^prerelease=true$"
g_state v1.0.0 true false; run_guard v1.0.0 false; check "Linux: final draft not a pre-release refused" 1 $? "not marked pre-release"
g_state v1.0.0 true false; run_guard v1.0.0 true; check "Win/mac: final draft not a pre-release passes" 0 $? "^prerelease=false$"
g_state v1.0.0-rc.1 true false; run_guard v1.0.0-rc.1 true; check "Win/mac: rc draft not a pre-release refused" 1 $? "not marked pre-release"
g_state v1.0.0 false false; run_guard v1.0.0 true; check "Win/mac: published final refused" 1 $? "is published"
g_state v1.0.0-rc.1 false true; run_guard v1.0.0-rc.1 true; check "Win/mac: published rc refused" 1 $? "is published"
g_state v1.0.0 true false; echo '[{"ref":"refs/tags/v1.0.0"}]' > "$STATE/refs.json"; echo "$C3" > "$STATE/commits/refs_tags_v1.0.0"
run_guard v1.0.0 true; check "existing tag on another commit refused" 1 $? "points at $C3"
g_state v1.0.0 true false; run_guard 'v1.0.0 ' true; check "tag with a trailing space refused" 1 $? "not a release tag"
new_state; run_guard v1.0.0 true; check "no release with the tag refused" 1 $? "no release has the tag"

# ---------------------------------------------------------------------------

echo
echo "$((PASS))/$((PASS + FAIL)) checks passed"
[[ $FAIL -eq 0 ]] || exit 1
