#!/usr/bin/env bash
#
# Create a release tag on the commit being released (#1039). Pushing a version
# tag starts the Windows and macOS builds into a draft release, so the tag has
# to be right the first time.
#
#   scripts/release/tag.sh <tag> [--push]
#
# Refuses unless all of these hold:
#   - no tracked file has uncommitted changes (untracked files are ignored:
#     the tag names a commit, not the working tree);
#   - the current branch is main or big-release;
#   - after a fetch, HEAD equals the remote tip of that branch, and is an
#     ancestor of (or equal to) origin/main or origin/big-release;
#   - the tag matches the workflow guard's regex: v1.2.3, optionally
#     -rc.N, -beta.N or -alpha.N, no leading zeros;
#   - the tag exists neither locally nor on the remote.
#
# Then it prints what it will tag and asks for the word "tag" before creating
# an annotated tag. It pushes the tag only with --push.
#
# STEMDECK_RELEASE_REMOTE names the remote (default origin).

set -euo pipefail

REMOTE="${STEMDECK_RELEASE_REMOTE:-origin}"
PATTERN='^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-(rc|beta|alpha)\.(0|[1-9][0-9]*))?$'

refuse() {
  echo "Refusing to tag: $1" >&2
  exit 1
}

usage() {
  echo "usage: scripts/release/tag.sh <tag> [--push]" >&2
  exit 2
}

TAG=""
PUSH=0
for arg in "$@"; do
  case "$arg" in
    --push) PUSH=1 ;;
    -h|--help) usage ;;
    -*) usage ;;
    *) [ -z "$TAG" ] || usage; TAG="$arg" ;;
  esac
done
[ -n "$TAG" ] || usage

if ! [[ "$TAG" =~ $PATTERN ]]; then
  refuse "'$TAG' is not a release tag (expected v1.2.3 or v1.2.3-rc.1, -beta.1, -alpha.1)."
fi

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || refuse "this is not a git work tree."

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  git status --short --untracked-files=no >&2
  refuse "tracked files have uncommitted changes. Commit or stash them first."
fi

branch="$(git symbolic-ref --quiet --short HEAD || true)"
case "$branch" in
  main|big-release) ;;
  "") refuse "HEAD is detached. Check out main or big-release." ;;
  *) refuse "the current branch is '$branch'. A release is tagged from main or big-release." ;;
esac

echo "Fetching $REMOTE ..."
# --no-tags: the local and remote tag checks below stay separate questions.
git fetch --quiet --no-tags "$REMOTE" \
  "+refs/heads/main:refs/remotes/$REMOTE/main" \
  "+refs/heads/big-release:refs/remotes/$REMOTE/big-release" ||
  refuse "could not fetch main and big-release from $REMOTE."

head="$(git rev-parse HEAD)"
tip="$(git rev-parse --verify --quiet "refs/remotes/$REMOTE/$branch")" ||
  refuse "$REMOTE/$branch does not exist."
echo "HEAD:               $head"
echo "$REMOTE/$branch tip: $tip"
if [ "$head" != "$tip" ]; then
  refuse "HEAD is not the tip of $REMOTE/$branch. Pull or push first, so the tag names the commit everyone sees."
fi

on_branch=""
for b in main big-release; do
  rc=0
  git merge-base --is-ancestor "$head" "refs/remotes/$REMOTE/$b" || rc=$?
  case "$rc" in
    0) on_branch="$b"; break ;;
    1) ;;
    *) refuse "git could not check whether HEAD is on $REMOTE/$b (exit $rc)." ;;
  esac
done
[ -n "$on_branch" ] || refuse "HEAD is on neither $REMOTE/main nor $REMOTE/big-release."

if git rev-parse --verify --quiet "refs/tags/$TAG" >/dev/null; then
  refuse "the tag $TAG already exists locally."
fi
remote_tags="$(git ls-remote --tags "$REMOTE" "refs/tags/$TAG")" ||
  refuse "could not list the tags on $REMOTE."
if [ -n "$remote_tags" ]; then
  refuse "the tag $TAG already exists on $REMOTE."
fi

echo
echo "Will create the annotated tag $TAG"
echo "  on commit $head ($branch, equal to $REMOTE/$branch)"
echo "  $(git log -1 --format='%h %s' "$head")"
if [ "$PUSH" -eq 1 ]; then
  echo "  and push it to $REMOTE, which starts the Windows and macOS draft builds."
else
  echo "  without pushing it (add --push to push)."
fi
printf 'Type the word tag to continue: '
answer=""
read -r answer || true
if [ "$answer" != "tag" ]; then
  refuse "you typed '$answer', not 'tag'. Nothing was created."
fi

git tag -a "$TAG" -m "$TAG" "$head"
echo "Created the tag $TAG on $head."

if [ "$PUSH" -eq 1 ]; then
  git push "$REMOTE" "refs/tags/$TAG"
  echo "Pushed $TAG to $REMOTE."
else
  echo "Not pushed. To push: git push $REMOTE refs/tags/$TAG"
fi
