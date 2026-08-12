#!/usr/bin/env bash
#
# Tests for packaging/linux/install.sh.
#
# Runs the real installer against a synthetic package in a throwaway HOME, so
# nothing here touches the machine it runs on. Every case that the reference
# installer in #342 got wrong has a test here, because each of them is silent
# at the point of failure and only shows up as a user with no working app.
#
# Run:  bash tests/linux/test_install_sh.sh

set -uo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
INSTALLER="${REPO_ROOT}/packaging/linux/install.sh"

PASS=0
FAIL=0

ok()   { PASS=$((PASS + 1)); printf 'PASS  %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); printf 'FAIL  %s%s\n' "$1" "${2:+  -- $2}"; }
check(){ if [[ "$2" == "1" ]]; then ok "$1"; else bad "$1" "${3:-}"; fi }

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

# A package shaped like what make-portable.sh produces.
make_package() {
    local dir="$1" version="$2" variant="${3:-CPU}"
    mkdir -p "$dir/backend/static" "$dir/backend/app" "$dir/python/bin" "$dir/packaging"
    printf '#!/bin/sh\necho stemdeck\n' > "$dir/StemDeck"
    chmod +x "$dir/StemDeck"
    printf '{ "version": "%s" }\n' "$version" > "$dir/backend/static/version.json"
    printf 'x' > "$dir/python/bin/python"
    printf 'PNG-placeholder' > "$dir/packaging/stemdeck.png"
    cp "${REPO_ROOT}/packaging/linux/stemdeck.desktop.in" "$dir/packaging/stemdeck.desktop.in"
    cp "$INSTALLER" "$dir/install.sh"
    chmod +x "$dir/install.sh"
    [[ "$variant" == "CPU" ]] && : > "$dir/cpu-only"
    return 0
}

# Each case gets its own HOME so manifests never leak between tests.
new_home() {
    local h="${SANDBOX}/home-$1"
    mkdir -p "$h"
    printf '%s' "$h"
}

run_installer() {  # run_installer <home> <pkg> [args...]
    local home="$1" pkg="$2"; shift 2
    env HOME="$home" \
        XDG_CONFIG_HOME="$home/.config" \
        XDG_DATA_HOME="$home/.local/share" \
        STEMDECK_INSTALL_ARCH="${FORCE_ARCH:-x86_64}" \
        bash "$pkg/install.sh" "$@" 2>&1
}

manifest_of() { cat "$1/.config/stemdeck/install-manifest" 2>/dev/null || true; }
mvalue() { manifest_of "$1" | sed -n "s/^$2=//p" | head -1; }

# ---------------------------------------------------------------------------
# 1. Fresh install
# ---------------------------------------------------------------------------

H="$(new_home fresh)"
PKG="${SANDBOX}/pkg-1"
make_package "$PKG" "0.8.0-alpha.17"
OUT="$(run_installer "$H" "$PKG" --local --yes)"
DIR="$H/.local/opt/stemdeck"

check "fresh install: exit 0" "$([[ $? -eq 0 ]] && echo 1 || echo 0)"
check "fresh install: binary in place" "$([[ -x "$DIR/StemDeck" ]] && echo 1 || echo 0)"
check "fresh install: backend copied" "$([[ -d "$DIR/backend/app" ]] && echo 1 || echo 0)"
check "fresh install: desktop entry written" \
  "$([[ -f "$H/.local/share/applications/stemdeck.desktop" ]] && echo 1 || echo 0)"
check "fresh install: icon written" \
  "$([[ -f "$H/.local/share/icons/stemdeck.png" ]] && echo 1 || echo 0)"
check "fresh install: version read from the package, not hardcoded" \
  "$([[ "$(mvalue "$H" Version)" == "0.8.0-alpha.17" ]] && echo 1 || echo 0)" "got '$(mvalue "$H" Version)'"
check "fresh install: variant read from the cpu-only marker" \
  "$([[ "$(mvalue "$H" Variant)" == "CPU" ]] && echo 1 || echo 0)" "got '$(mvalue "$H" Variant)'"
check "fresh install: no staging dirs left behind" \
  "$([[ ! -e "${DIR}.new" && ! -e "${DIR}.old" ]] && echo 1 || echo 0)"

# The bug that made the fork's entry unusable on a custom path.
EXEC_LINE="$(sed -n 's/^Exec=//p' "$H/.local/share/applications/stemdeck.desktop")"
check "fresh install: Exec is quoted" \
  "$([[ "$EXEC_LINE" == '"'*'"' ]] && echo 1 || echo 0)" "Exec=$EXEC_LINE"

# ---------------------------------------------------------------------------
# 2. NVIDIA variant is detected from the package, never asked
# ---------------------------------------------------------------------------

H="$(new_home nvidia)"
PKG="${SANDBOX}/pkg-nv"
make_package "$PKG" "0.8.0-alpha.17" "NVIDIA"
run_installer "$H" "$PKG" --local --yes >/dev/null
check "NVIDIA package records the NVIDIA variant" \
  "$([[ "$(mvalue "$H" Variant)" == "NVIDIA" ]] && echo 1 || echo 0)" "got '$(mvalue "$H" Variant)'"

# ---------------------------------------------------------------------------
# 3. Upgrade replaces the install and updates the manifest
# ---------------------------------------------------------------------------

H="$(new_home upgrade)"
PKG_OLD="${SANDBOX}/pkg-old"; make_package "$PKG_OLD" "0.8.0-alpha.16"
PKG_NEW="${SANDBOX}/pkg-new"; make_package "$PKG_NEW" "0.8.0-alpha.17"
run_installer "$H" "$PKG_OLD" --local --yes >/dev/null
printf 'marker-new\n' > "$PKG_NEW/NEWFILE"
OUT="$(run_installer "$H" "$PKG_NEW" --yes)"
DIR="$H/.local/opt/stemdeck"
check "upgrade: manifest now records the new version" \
  "$([[ "$(mvalue "$H" Version)" == "0.8.0-alpha.17" ]] && echo 1 || echo 0)" "got '$(mvalue "$H" Version)'"
check "upgrade: new content is present" "$([[ -f "$DIR/NEWFILE" ]] && echo 1 || echo 0)"
check "upgrade: reused the recorded location without asking" \
  "$([[ -x "$DIR/StemDeck" ]] && echo 1 || echo 0)"

# ---------------------------------------------------------------------------
# 4. THE BLOCKER: a failed upgrade must leave the working install alone
# ---------------------------------------------------------------------------

H="$(new_home failed)"
PKG_OK="${SANDBOX}/pkg-ok"; make_package "$PKG_OK" "0.8.0-alpha.16"
run_installer "$H" "$PKG_OK" --local --yes >/dev/null
DIR="$H/.local/opt/stemdeck"
printf 'user-was-here\n' > "$DIR/sentinel"

# A package that passes the up-front check but cannot be copied: the executable
# is unreadable, so cp fails partway.
PKG_BAD="${SANDBOX}/pkg-bad"; make_package "$PKG_BAD" "0.8.0-alpha.17"
mkdir -p "$PKG_BAD/backend/app/unreadable"
chmod 000 "$PKG_BAD/backend/app/unreadable"

OUT="$(run_installer "$H" "$PKG_BAD" --yes)"; RC=$?
chmod 755 "$PKG_BAD/backend/app/unreadable" 2>/dev/null || true

check "failed upgrade: the install directory survives" \
  "$([[ -x "$DIR/StemDeck" ]] && echo 1 || echo 0)"
check "failed upgrade: the user's files survive" \
  "$([[ -f "$DIR/sentinel" ]] && echo 1 || echo 0)"
check "failed upgrade: the desktop entry survives" \
  "$([[ -f "$H/.local/share/applications/stemdeck.desktop" ]] && echo 1 || echo 0)"
check "failed upgrade: the manifest survives, still on the old version" \
  "$([[ "$(mvalue "$H" Version)" == "0.8.0-alpha.16" ]] && echo 1 || echo 0)" "got '$(mvalue "$H" Version)'"
check "failed upgrade: no staging dirs left behind" \
  "$([[ ! -e "${DIR}.new" ]] && echo 1 || echo 0)"

# ---------------------------------------------------------------------------
# 5. A missing manifest key must not kill the script
# ---------------------------------------------------------------------------

H="$(new_home corrupt)"
PKG="${SANDBOX}/pkg-c"; make_package "$PKG" "0.8.0-alpha.17"
run_installer "$H" "$PKG" --local --yes >/dev/null
# Strip Variant, as an older installer's manifest would lack newer keys.
grep -v '^Variant=' "$H/.config/stemdeck/install-manifest" > "$H/m.tmp"
mv "$H/m.tmp" "$H/.config/stemdeck/install-manifest"
OUT="$(run_installer "$H" "$PKG" --yes)"; RC=$?
check "missing manifest key: installer still runs" "$([[ $RC -eq 0 ]] && echo 1 || echo 0)" "rc=$RC"
check "missing manifest key: repaired on write" \
  "$([[ "$(mvalue "$H" Variant)" == "CPU" ]] && echo 1 || echo 0)"

# A manifest with no InstallDir is corrupt: uninstall must say so, not flail.
H="$(new_home corrupt2)"
mkdir -p "$H/.config/stemdeck"
printf 'Version=0.8.0-alpha.17\n' > "$H/.config/stemdeck/install-manifest"
PKG="${SANDBOX}/pkg-c2"; make_package "$PKG" "0.8.0-alpha.17"
OUT="$(run_installer "$H" "$PKG" --uninstall --yes)"; RC=$?
check "corrupt manifest: uninstall refuses with a message" \
  "$([[ $RC -ne 0 ]] && grep -qi "corrupt" <<<"$OUT" && echo 1 || echo 0)" "rc=$RC out=$(head -3 <<<"$OUT" | tr '\n' ' ')"

# ---------------------------------------------------------------------------
# 6. Install path containing a space
# ---------------------------------------------------------------------------

H="$(new_home spaces)"
PKG="${SANDBOX}/pkg-sp"; make_package "$PKG" "0.8.0-alpha.17"
PREFIX="${SANDBOX}/My Apps"
mkdir -p "$PREFIX"
OUT="$(run_installer "$H" "$PKG" --prefix "$PREFIX" --yes)"; RC=$?
check "spaces: install succeeds" "$([[ $RC -eq 0 ]] && echo 1 || echo 0)" "$(tail -2 <<<"$OUT")"
check "spaces: binary in place" "$([[ -x "$PREFIX/stemdeck/StemDeck" ]] && echo 1 || echo 0)"
EXEC_LINE="$(sed -n 's/^Exec=//p' "$H/.local/share/applications/stemdeck.desktop")"
check "spaces: Exec parses as a single argument" \
  "$(python3 -c "
import shlex,sys
parts=shlex.split(sys.argv[1])
print(1 if len(parts)==1 and parts[0].endswith('/stemdeck/StemDeck') else 0)" "$EXEC_LINE")" "Exec=$EXEC_LINE"

# ---------------------------------------------------------------------------
# 7. Uninstall
# ---------------------------------------------------------------------------

H="$(new_home uninstall)"
PKG="${SANDBOX}/pkg-u"; make_package "$PKG" "0.8.0-alpha.17"
run_installer "$H" "$PKG" --local --yes >/dev/null
DIR="$H/.local/opt/stemdeck"
# Stand in for the user's real data, which lives outside the install dir.
mkdir -p "$H/Documents/StemDeck/jobs/abc" "$H/.local/share/stemdeck/models"
printf 'stems' > "$H/Documents/StemDeck/jobs/abc/vocals.wav"
printf 'model' > "$H/.local/share/stemdeck/models/htdemucs"

OUT="$(run_installer "$H" "$PKG" --uninstall --yes)"; RC=$?
check "uninstall: exit 0" "$([[ $RC -eq 0 ]] && echo 1 || echo 0)"
check "uninstall: install directory gone" "$([[ ! -e "$DIR" ]] && echo 1 || echo 0)"
check "uninstall: desktop entry gone" \
  "$([[ ! -f "$H/.local/share/applications/stemdeck.desktop" ]] && echo 1 || echo 0)"
check "uninstall: icon gone" "$([[ ! -f "$H/.local/share/icons/stemdeck.png" ]] && echo 1 || echo 0)"
check "uninstall: manifest gone" \
  "$([[ ! -f "$H/.config/stemdeck/install-manifest" ]] && echo 1 || echo 0)"
check "uninstall: stems untouched" \
  "$([[ -f "$H/Documents/StemDeck/jobs/abc/vocals.wav" ]] && echo 1 || echo 0)"
check "uninstall: runtime and models untouched" \
  "$([[ -f "$H/.local/share/stemdeck/models/htdemucs" ]] && echo 1 || echo 0)"

# ---------------------------------------------------------------------------
# 8. Legacy in-install data/ is never destroyed
# ---------------------------------------------------------------------------

H="$(new_home legacy)"
PKG_A="${SANDBOX}/pkg-la"; make_package "$PKG_A" "0.8.0-alpha.16"
PKG_B="${SANDBOX}/pkg-lb"; make_package "$PKG_B" "0.8.0-alpha.17"
run_installer "$H" "$PKG_A" --local --yes >/dev/null
DIR="$H/.local/opt/stemdeck"
mkdir -p "$DIR/data/jobs/old"
printf 'irreplaceable' > "$DIR/data/jobs/old/vocals.wav"

run_installer "$H" "$PKG_B" --yes >/dev/null
check "legacy data: carried across an upgrade" \
  "$([[ -f "$DIR/data/jobs/old/vocals.wav" ]] && echo 1 || echo 0)"

OUT="$(run_installer "$H" "$PKG_B" --uninstall --yes)"
check "legacy data: uninstall refuses to delete it" \
  "$([[ -f "$DIR/data/jobs/old/vocals.wav" ]] && echo 1 || echo 0)"
check "legacy data: uninstall says why the folder was kept" \
  "$(grep -qi "data/" <<<"$OUT" && echo 1 || echo 0)"

# ---------------------------------------------------------------------------
# 9. Rejecting a package that is not one
# ---------------------------------------------------------------------------

H="$(new_home badpkg)"
PKG="${SANDBOX}/pkg-broken"
mkdir -p "$PKG"
cp "$INSTALLER" "$PKG/install.sh"
OUT="$(run_installer "$H" "$PKG" --local --yes)"; RC=$?
check "incomplete package: refused" "$([[ $RC -ne 0 ]] && echo 1 || echo 0)"
check "incomplete package: nothing installed" \
  "$([[ ! -e "$H/.local/opt/stemdeck" ]] && echo 1 || echo 0)"
check "incomplete package: no manifest written" \
  "$([[ ! -f "$H/.config/stemdeck/install-manifest" ]] && echo 1 || echo 0)"

# ---------------------------------------------------------------------------
# 10. Refusing to install over the copy we are running from
# ---------------------------------------------------------------------------

H="$(new_home selfinstall)"
PKG="${SANDBOX}/pkg-self"; make_package "$PKG" "0.8.0-alpha.17"
run_installer "$H" "$PKG" --local --yes >/dev/null
DIR="$H/.local/opt/stemdeck"
OUT="$(env HOME="$H" XDG_CONFIG_HOME="$H/.config" XDG_DATA_HOME="$H/.local/share" \
       STEMDECK_INSTALL_ARCH=x86_64 bash "$DIR/install.sh" --yes 2>&1)"; RC=$?
check "self-install: refused with a message" \
  "$([[ $RC -ne 0 ]] && grep -qi "extracted tarball" <<<"$OUT" && echo 1 || echo 0)" "rc=$RC"
check "self-install: install left intact" "$([[ -x "$DIR/StemDeck" ]] && echo 1 || echo 0)"

# ---------------------------------------------------------------------------
# 10b. The generated launcher is a valid desktop entry
#
# Only checkable where desktop-file-utils exists, which is CI. A malformed entry
# is silently ignored by the desktop environment, so nothing else would notice.
# ---------------------------------------------------------------------------

if command -v desktop-file-validate >/dev/null 2>&1; then
    H="$(new_home validate)"
    PKG="${SANDBOX}/pkg-val"; make_package "$PKG" "0.8.0-alpha.17"
    run_installer "$H" "$PKG" --local --yes >/dev/null
    ENTRY="$H/.local/share/applications/stemdeck.desktop"
    if OUT="$(desktop-file-validate "$ENTRY" 2>&1)"; then
        ok "desktop entry validates"
    else
        bad "desktop entry validates" "$(head -3 <<<"$OUT" | tr '\n' ' ')"
    fi
else
    echo "SKIP  desktop entry validates (desktop-file-validate not installed)"
fi

# ---------------------------------------------------------------------------
# 11. A machine the package cannot run on
# ---------------------------------------------------------------------------

H="$(new_home arch)"
PKG="${SANDBOX}/pkg-arch"; make_package "$PKG" "0.8.0-alpha.17"
OUT="$(FORCE_ARCH=aarch64 run_installer "$H" "$PKG" --local --yes)"; RC=$?
check "wrong arch: refused" "$([[ $RC -ne 0 ]] && echo 1 || echo 0)"
check "wrong arch: names the machine type" \
  "$(grep -q "aarch64" <<<"$OUT" && echo 1 || echo 0)" "$(head -2 <<<"$OUT" | tr '\n' ' ')"
check "wrong arch: nothing installed" \
  "$([[ ! -e "$H/.local/opt/stemdeck" ]] && echo 1 || echo 0)"

# ---------------------------------------------------------------------------
# 12. Semver comparison
#
# sort -V gets the pre-release cases wrong, which is why this is hand-rolled.
# ---------------------------------------------------------------------------

vcmp() {
    bash -c '
        set -euo pipefail
        # Sourcing runs no install logic: main() is guarded on BASH_SOURCE.
        source "$1"
        version_cmp "$2" "$3"
    ' _ "$INSTALLER" "$1" "$2"
}

expect_cmp() {
    local got; got="$(vcmp "$1" "$2")"
    check "semver: $1 vs $2 -> $3" "$([[ "$got" == "$3" ]] && echo 1 || echo 0)" "got $got"
}

expect_cmp "0.8.0-alpha.17" "0.8.0-alpha.7"  "1"
expect_cmp "0.8.0-alpha.7"  "0.8.0-alpha.17" "-1"
expect_cmp "0.8.0-alpha.17" "0.8.0-alpha.17" "0"
expect_cmp "0.8.0-alpha.18" "0.8.0-alpha.17" "1"
expect_cmp "0.9.0"          "0.8.0"          "1"
expect_cmp "1.0.0"          "0.9.9"          "1"
# The case sort -V gets wrong: a stable release outranks its own pre-releases.
expect_cmp "0.8.0"          "0.8.0-alpha.17" "1"
expect_cmp "0.8.0-alpha.17" "0.8.0"          "-1"
expect_cmp "0.8.0-alpha"    "0.8.0-alpha.1"  "-1"
expect_cmp "0.8.0-beta.1"   "0.8.0-alpha.9"  "1"

# ---------------------------------------------------------------------------

echo
echo "$((PASS))/$((PASS + FAIL)) checks passed"
[[ $FAIL -eq 0 ]] || exit 1
