#!/usr/bin/env bash
#
# StemDeck Linux installer (#342).
#
# Ships inside the portable tarball and installs the package it sits in, so it
# never downloads anything: the version and the CPU/NVIDIA variant are read out
# of the package, which means the installer and the build can never disagree
# about what is being installed.
#
# Desktop integration is the whole point. StemDeck stays portable -- extract the
# tarball and run ./StemDeck and nothing here is required.
#
# Usage:
#   ./install.sh                 install, or upgrade an existing install in place
#   ./install.sh --global        install to /opt/stemdeck (needs sudo)
#   ./install.sh --local         install to ~/.local/opt/stemdeck
#   ./install.sh --prefix DIR    install to DIR/stemdeck
#   ./install.sh --uninstall     remove what the manifest records
#   ./install.sh --yes           never prompt
#
# User data is never touched by any of this. Stems live in ~/Documents/StemDeck
# and the runtime, models, ffmpeg and logs in ~/.local/share/stemdeck (or
# $XDG_DATA_HOME/stemdeck); neither is inside the install directory.

set -euo pipefail

PKG_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/stemdeck"
MANIFEST="${CONFIG_DIR}/install-manifest"

GLOBAL_APPS="/usr/share/applications"
GLOBAL_ICONS="/usr/share/pixmaps"
LOCAL_APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
LOCAL_ICONS="${XDG_DATA_HOME:-$HOME/.local/share}/icons"

ASSUME_YES=0
MODE="install"
WANT_SCOPE=""
WANT_PREFIX=""

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

info() { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf '\nerror: %s\n\n' "$*" >&2; exit 1; }

confirm() {
    local prompt="$1" answer
    [[ $ASSUME_YES -eq 1 ]] && return 0
    while true; do
        read -r -p "$prompt [y/n]: " answer || return 1
        case "$answer" in
            y|Y|yes|YES) return 0 ;;
            n|N|no|NO)   return 1 ;;
            *)           echo "Please answer y or n." ;;
        esac
    done
}

# sudo only where it is actually needed, and only after telling the user why.
run_privileged() {
    if [[ $EUID -eq 0 ]]; then
        "$@"
    else
        sudo "$@"
    fi
}

# ---------------------------------------------------------------------------
# Manifest
#
# Deliberately not `grep | head | cut`: under `set -euo pipefail` a key that is
# not present makes grep exit 1, which kills the script with no message and
# makes the "corrupt manifest" handling below unreachable. A missing key must
# read as empty.
# ---------------------------------------------------------------------------

manifest_value() {
    local key="$1" line
    [[ -f "$MANIFEST" ]] || return 0
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" == "${key}="* ]]; then
            printf '%s\n' "${line#*=}"
            return 0
        fi
    done < "$MANIFEST"
    return 0
}

# ---------------------------------------------------------------------------
# Version comparison
#
# Semver, not `sort -V`: sort -V orders 0.8.0-alpha.17 above 0.8.0, so once a
# stable release ships every pre-release user would be told they are current.
# ---------------------------------------------------------------------------

_ver_core() { printf '%s' "${1%%-*}"; }
_ver_pre()  { case "$1" in *-*) printf '%s' "${1#*-}" ;; *) printf '' ;; esac; }
_is_num()   { [[ "$1" =~ ^[0-9]+$ ]]; }

# Prints -1, 0 or 1 for $1 relative to $2.
version_cmp() {
    local a="$1" b="$2" i x y
    local -a A B AP BP

    IFS=. read -r -a A <<< "$(_ver_core "$a")"
    IFS=. read -r -a B <<< "$(_ver_core "$b")"
    for i in 0 1 2; do
        x="${A[i]:-0}"; x="${x//[!0-9]/}"; x="${x:-0}"
        y="${B[i]:-0}"; y="${y//[!0-9]/}"; y="${y:-0}"
        if (( 10#$x > 10#$y )); then echo 1; return 0; fi
        if (( 10#$x < 10#$y )); then echo -1; return 0; fi
    done

    local ap bp
    ap="$(_ver_pre "$a")"
    bp="$(_ver_pre "$b")"
    if [[ -z "$ap" && -z "$bp" ]]; then echo 0; return 0; fi
    # A version with no pre-release tag outranks one that has it: 0.8.0 > 0.8.0-alpha.17.
    if [[ -z "$ap" ]]; then echo 1;  return 0; fi
    if [[ -z "$bp" ]]; then echo -1; return 0; fi

    IFS=. read -r -a AP <<< "$ap"
    IFS=. read -r -a BP <<< "$bp"
    local n=${#AP[@]}
    (( ${#BP[@]} > n )) && n=${#BP[@]}
    for (( i = 0; i < n; i++ )); do
        x="${AP[i]-}"
        y="${BP[i]-}"
        # A shorter identifier list sorts first: alpha < alpha.1.
        if [[ -z "$x" ]]; then echo -1; return 0; fi
        if [[ -z "$y" ]]; then echo 1;  return 0; fi
        if _is_num "$x" && _is_num "$y"; then
            if (( 10#$x > 10#$y )); then echo 1; return 0; fi
            if (( 10#$x < 10#$y )); then echo -1; return 0; fi
        else
            if [[ "$x" > "$y" ]]; then echo 1; return 0; fi
            if [[ "$x" < "$y" ]]; then echo -1; return 0; fi
        fi
    done
    echo 0
}

# ---------------------------------------------------------------------------
# Reading the package
# ---------------------------------------------------------------------------

package_version() {
    local file="${PKG_DIR}/backend/static/version.json" line
    [[ -f "$file" ]] || { printf 'unknown'; return 0; }
    # Avoid a jq dependency; the file is written by make-portable.sh as
    # { "version": "0.8.0-alpha.17" }.
    line="$(tr -d ' \t\n' < "$file")"
    line="${line#*\"version\":\"}"
    line="${line%%\"*}"
    [[ -n "$line" ]] || line="unknown"
    printf '%s' "$line"
}

package_variant() {
    if [[ -f "${PKG_DIR}/cpu-only" ]]; then printf 'CPU'; else printf 'NVIDIA'; fi
}

verify_package() {
    [[ -x "${PKG_DIR}/StemDeck" || -f "${PKG_DIR}/StemDeck" ]] \
        || die "this does not look like a StemDeck package: no StemDeck executable next to the installer."
    [[ -d "${PKG_DIR}/backend/app" ]] \
        || die "the package is incomplete: backend/app is missing."
    [[ -d "${PKG_DIR}/python" ]] \
        || die "the package is incomplete: the bundled Python runtime is missing."
    [[ -f "${PKG_DIR}/packaging/stemdeck.png" && -f "${PKG_DIR}/packaging/stemdeck.desktop.in" ]] \
        || die "the package is incomplete: packaging/ assets are missing."

    # STEMDECK_INSTALL_ARCH overrides the detected machine type, for the test
    # suite and for anyone deliberately installing under an x86_64 emulation
    # layer. Everything else gets a clear refusal rather than a binary that
    # cannot run.
    local arch
    arch="${STEMDECK_INSTALL_ARCH:-$(uname -m)}"
    if [[ "$arch" != "x86_64" ]]; then
        die "StemDeck ships x86_64 binaries only; this machine reports ${arch}."
    fi
}

# ---------------------------------------------------------------------------
# Where to install
# ---------------------------------------------------------------------------

resolve_scope() {
    local choice
    if [[ -n "$WANT_PREFIX" ]]; then
        SCOPE="Custom"
        INSTALL_DIR="${WANT_PREFIX%/}/stemdeck"
        return 0
    fi
    case "$WANT_SCOPE" in
        global) SCOPE="Global"; INSTALL_DIR="/opt/stemdeck"; return 0 ;;
        local)  SCOPE="Local";  INSTALL_DIR="${HOME}/.local/opt/stemdeck"; return 0 ;;
    esac

    if [[ $ASSUME_YES -eq 1 ]]; then
        SCOPE="Local"; INSTALL_DIR="${HOME}/.local/opt/stemdeck"; return 0
    fi

    echo
    echo "Where should StemDeck be installed?"
    echo
    echo "  1) Just me      ${HOME}/.local/opt/stemdeck"
    echo "  2) All users    /opt/stemdeck  (needs sudo)"
    echo "  3) Somewhere else"
    echo
    while true; do
        read -r -p "Choice [1-3] (default 1): " choice
        case "${choice:-1}" in
            1) SCOPE="Local";  INSTALL_DIR="${HOME}/.local/opt/stemdeck"; return 0 ;;
            2) SCOPE="Global"; INSTALL_DIR="/opt/stemdeck"; return 0 ;;
            3)
                local dir
                read -r -p "Directory: " dir
                dir="${dir%/}"
                [[ -n "$dir" ]] || { echo "Enter a directory."; continue; }
                # A tilde typed at this prompt arrives as a literal character:
                # `read` does no expansion. Matching it is the point, so the
                # usual "tilde does not expand in quotes" warning is inverted.
                # shellcheck disable=SC2088
                case "$dir" in "~") dir="$HOME" ;; "~/"*) dir="${HOME}/${dir#\~/}" ;; esac
                [[ "$dir" = /* ]] || { echo "Use an absolute path."; continue; }
                SCOPE="Custom"; INSTALL_DIR="${dir}/stemdeck"; return 0 ;;
            *) echo "Enter 1, 2 or 3." ;;
        esac
    done
}

# Global installs put the launcher and icon where every user can see them.
# Getting this wrong is invisible to the person installing and broken for
# everyone else on the machine.
set_integration_paths() {
    if [[ "$SCOPE" == "Global" ]]; then
        DESKTOP_FILE="${GLOBAL_APPS}/stemdeck.desktop"
        ICON_FILE="${GLOBAL_ICONS}/stemdeck.png"
    else
        DESKTOP_FILE="${LOCAL_APPS}/stemdeck.desktop"
        ICON_FILE="${LOCAL_ICONS}/stemdeck.png"
    fi
}

privileged_for_scope() {
    if [[ "$SCOPE" == "Global" ]]; then run_privileged "$@"; else "$@"; fi
}

# ---------------------------------------------------------------------------
# Install / upgrade
# ---------------------------------------------------------------------------

# Copy into <target>.new, prove it, then swap. A half-finished copy -- a full
# disk, a snapped network mount -- must never be able to leave the machine with
# no working StemDeck, which is what removing the old copy first would risk.
stage_and_swap() {
    local staging="${INSTALL_DIR}.new"
    local previous="${INSTALL_DIR}.old"

    privileged_for_scope rm -rf "$staging" "$previous"
    privileged_for_scope mkdir -p "$(dirname "$INSTALL_DIR")"

    # Every failure below has to clean up after itself. Under `set -e` a bare
    # `cp` that dies partway would abort the script on the spot, leaving a
    # half-written copy the size of the package sitting on the disk.
    discard_staging() { privileged_for_scope rm -rf "$staging"; }

    info "Copying the package to ${INSTALL_DIR} ..."
    if ! privileged_for_scope cp -a "$PKG_DIR" "$staging"; then
        discard_staging
        die "could not copy the package into place; nothing was changed."
    fi
    if ! privileged_for_scope chmod +x "${staging}/StemDeck"; then
        discard_staging
        die "could not make the copied executable runnable; nothing was changed."
    fi
    if [[ ! -x "${staging}/StemDeck" || ! -d "${staging}/backend/app" || ! -d "${staging}/python" ]]; then
        discard_staging
        die "the copy came out incomplete; nothing was changed."
    fi

    if [[ -e "$INSTALL_DIR" ]]; then
        # Pre-migration builds kept user data inside the install directory. The
        # app moves it out on first launch, so a copy that has never run a
        # current build can still hold the only copy of someone's work.
        if [[ -d "${INSTALL_DIR}/data" ]]; then
            info "Carrying forward legacy data/ from the previous install"
            privileged_for_scope cp -a "${INSTALL_DIR}/data" "${staging}/data"
        fi
        privileged_for_scope mv "$INSTALL_DIR" "$previous"
    fi

    if ! privileged_for_scope mv "$staging" "$INSTALL_DIR"; then
        # Put the old one back rather than leaving the user with nothing.
        [[ -e "$previous" ]] && privileged_for_scope mv "$previous" "$INSTALL_DIR"
        die "could not move the new install into place; the previous install was restored."
    fi
    privileged_for_scope rm -rf "$previous"
}

install_integration() {
    local tmp
    info "Installing the desktop entry ..."

    privileged_for_scope mkdir -p "$(dirname "$ICON_FILE")" "$(dirname "$DESKTOP_FILE")"
    privileged_for_scope cp "${INSTALL_DIR}/packaging/stemdeck.png" "$ICON_FILE"
    privileged_for_scope chmod 644 "$ICON_FILE"

    tmp="$(mktemp)"
    # The template quotes Exec, so an install path containing a space still
    # launches. sed with | as the delimiter keeps / in paths from ending it.
    sed -e "s|@EXEC@|${INSTALL_DIR}/StemDeck|g" \
        -e "s|@ICON@|${ICON_FILE}|g" \
        "${INSTALL_DIR}/packaging/stemdeck.desktop.in" > "$tmp"
    privileged_for_scope cp "$tmp" "$DESKTOP_FILE"
    privileged_for_scope chmod 644 "$DESKTOP_FILE"
    rm -f "$tmp"

    if command -v desktop-file-validate >/dev/null 2>&1; then
        desktop-file-validate "$DESKTOP_FILE" \
            || warn "the desktop entry did not validate cleanly; the launcher may not appear."
    fi
    if command -v update-desktop-database >/dev/null 2>&1; then
        privileged_for_scope update-desktop-database "$(dirname "$DESKTOP_FILE")" >/dev/null 2>&1 || true
    fi
}

write_manifest() {
    mkdir -p "$CONFIG_DIR"
    # Written last: it is the record that the install completed, so a failure
    # anywhere above must not leave one behind claiming otherwise.
    cat > "$MANIFEST" <<EOF
# StemDeck installation manifest. Written by install.sh; edit at your own risk.
Version=${VERSION}
Variant=${VARIANT}
Scope=${SCOPE}
InstallDir=${INSTALL_DIR}
Exec=${INSTALL_DIR}/StemDeck
DesktopFile=${DESKTOP_FILE}
IconFile=${ICON_FILE}
InstalledAt=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF
}

do_install() {
    local installed_version installed_dir cmp

    verify_package
    VERSION="$(package_version)"
    VARIANT="$(package_variant)"

    installed_version="$(manifest_value Version)"
    installed_dir="$(manifest_value InstallDir)"

    if [[ -n "$installed_dir" && -z "$WANT_SCOPE" && -z "$WANT_PREFIX" ]]; then
        # Upgrade in place: the location is already the user's decision.
        SCOPE="$(manifest_value Scope)"
        [[ -n "$SCOPE" ]] || SCOPE="Local"
        INSTALL_DIR="$installed_dir"

        echo
        echo "StemDeck ${installed_version:-unknown} is installed at ${INSTALL_DIR}."
        echo "This package is ${VERSION} (${VARIANT})."
        echo
        if [[ -n "$installed_version" && "$installed_version" != "unknown" && "$VERSION" != "unknown" ]]; then
            cmp="$(version_cmp "$VERSION" "$installed_version")"
            if [[ "$cmp" == "0" ]]; then
                confirm "Reinstall the same version?" || { echo "Nothing to do."; exit 0; }
            elif [[ "$cmp" == "-1" ]]; then
                confirm "That is OLDER than what is installed. Downgrade?" || { echo "Nothing to do."; exit 0; }
            else
                confirm "Upgrade?" || { echo "Nothing to do."; exit 0; }
            fi
        else
            confirm "Reinstall?" || { echo "Nothing to do."; exit 0; }
        fi
    else
        resolve_scope
        if [[ -e "$INSTALL_DIR" ]]; then
            echo
            echo "${INSTALL_DIR} already exists and will be replaced."
            confirm "Continue?" || { echo "Nothing to do."; exit 0; }
        fi
    fi

    # Installing from inside the destination would move the running script out
    # from under bash mid-execution. Cheaper to refuse than to rely on the
    # kernel keeping a deleted-but-open file readable.
    if [[ "$PKG_DIR" == "$INSTALL_DIR" || "$PKG_DIR" == "$INSTALL_DIR"/* ]]; then
        die "run this from the extracted tarball, not from the installed copy at ${INSTALL_DIR}."
    fi

    set_integration_paths

    echo
    info "StemDeck ${VERSION} (${VARIANT})"
    info "Installing to ${INSTALL_DIR}"
    [[ "$SCOPE" == "Global" ]] && info "This needs sudo to write outside your home directory."

    stage_and_swap
    install_integration
    write_manifest

    echo
    echo "Done. StemDeck ${VERSION} is installed."
    echo
    echo "  Location : ${INSTALL_DIR}"
    echo "  Launcher : ${DESKTOP_FILE}"
    echo "  Run      : ${INSTALL_DIR}/StemDeck"
    echo
    echo "Your tracks and settings are untouched, and live outside this folder."
    echo "To remove StemDeck later: ${INSTALL_DIR}/install.sh --uninstall"
    echo
}

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------

do_uninstall() {
    local dir desktop icon scope version

    [[ -f "$MANIFEST" ]] || die "no StemDeck installation is recorded at ${MANIFEST}."

    version="$(manifest_value Version)"
    dir="$(manifest_value InstallDir)"
    desktop="$(manifest_value DesktopFile)"
    icon="$(manifest_value IconFile)"
    scope="$(manifest_value Scope)"
    [[ -n "$scope" ]] || scope="Local"
    SCOPE="$scope"

    [[ -n "$dir" ]] || die "the manifest records no install directory; it may be corrupt. Nothing was removed."

    echo
    echo "StemDeck ${version:-unknown} at ${dir}"
    echo
    echo "This removes the application only. Your tracks and settings stay where"
    echo "they are, outside the install directory."
    echo
    confirm "Remove StemDeck?" || { echo "Nothing to do."; exit 0; }

    # Legacy in-install user data: refuse to be the thing that deletes it.
    if [[ -d "${dir}/data" ]]; then
        warn "leaving ${dir} in place: it contains a data/ folder from an older"
        warn "StemDeck that may hold your tracks. Move it somewhere safe, then"
        warn "delete the folder by hand."
    elif [[ -e "$dir" ]]; then
        info "Removing ${dir}"
        privileged_for_scope rm -rf "$dir"
    fi

    if [[ -n "$desktop" && -f "$desktop" ]]; then
        info "Removing ${desktop}"
        privileged_for_scope rm -f "$desktop"
    fi
    if [[ -n "$icon" && -f "$icon" ]]; then
        info "Removing ${icon}"
        privileged_for_scope rm -f "$icon"
    fi
    if [[ -n "$desktop" ]] && command -v update-desktop-database >/dev/null 2>&1; then
        privileged_for_scope update-desktop-database "$(dirname "$desktop")" >/dev/null 2>&1 || true
    fi

    rm -f "$MANIFEST"
    rmdir "$CONFIG_DIR" 2>/dev/null || true

    echo
    echo "StemDeck has been removed."
    echo
    echo "Your tracks and settings were not touched:"
    echo "  ~/Documents/StemDeck"
    echo "  ${XDG_DATA_HOME:-$HOME/.local/share}/stemdeck"
    echo
}

usage() {
    sed -n '3,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --global)    WANT_SCOPE="global" ;;
            --local)     WANT_SCOPE="local" ;;
            --prefix)    shift; [[ $# -gt 0 ]] || die "--prefix needs a directory."; WANT_PREFIX="$1" ;;
            --uninstall) MODE="uninstall" ;;
            --yes|-y)    ASSUME_YES=1 ;;
            --help|-h)   usage; exit 0 ;;
            *)           die "unknown option: $1 (try --help)" ;;
        esac
        shift
    done

    case "$MODE" in
        uninstall) do_uninstall ;;
        *)         do_install ;;
    esac
}

# Only act when executed. Sourcing the script exposes the functions on their
# own, which is how the test suite exercises version_cmp without installing
# anything.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
