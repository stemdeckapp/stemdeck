# shellcheck shell=bash
# Shared signing helpers for make-app.sh and make-dmg.sh. Source it; do not run it.
#
# Signing uses APPLE_SIGNING_IDENTITY (a "Developer ID Application" identity in
# the keychain). Notarization uses an App Store Connect API key:
#   APPLE_API_ISSUER    issuer id
#   APPLE_API_KEY       key id
#   APPLE_API_KEY_PATH  path to the AuthKey_<id>.p8 file (only the path is used)
#   NOTARIZE_TIMEOUT    how long to wait per submission (default 4h); a first
#                       submission for a new app can take well over 30 minutes

# True when every credential notarization needs is present.
notarize_credentials_set() {
  [[ -n "${APPLE_SIGNING_IDENTITY:-}" && -n "${APPLE_API_ISSUER:-}" \
    && -n "${APPLE_API_KEY:-}" && -n "${APPLE_API_KEY_PATH:-}" ]]
}

# notarize_submit <file>
# Submits a .zip or .dmg to the notary service and waits. Prints the submission
# id; on any status other than Accepted prints the notary log and fails.
notarize_submit() {
  local file="$1"
  if [[ ! -f "$APPLE_API_KEY_PATH" ]]; then
    echo "ERROR: APPLE_API_KEY_PATH does not point to a file" >&2
    return 1
  fi
  local out err
  out="$(mktemp -t stemdeck-notary)"
  err="$(mktemp -t stemdeck-notary-err)"
  echo "==> Submitting $(basename "$file") for notarization (this can take several minutes)"
  # --wait can exit non-zero for a rejected or timed-out submission; the status
  # is read below. On success the JSON result is on stdout; on an error (for
  # example a timeout) notarytool writes it to stderr instead.
  xcrun notarytool submit "$file" \
    --key "$APPLE_API_KEY_PATH" \
    --key-id "$APPLE_API_KEY" \
    --issuer "$APPLE_API_ISSUER" \
    --wait --timeout "${NOTARIZE_TIMEOUT:-4h}" \
    --output-format json > "$out" 2> "$err" || true

  local id status
  id="$(plutil -extract id raw -o - "$out" 2>/dev/null ||
    plutil -extract id raw -o - "$err" 2>/dev/null || true)"
  status="$(plutil -extract status raw -o - "$out" 2>/dev/null || true)"
  echo "    submission id: ${id:-unknown}"
  echo "    status: ${status:-unknown}"
  if [[ "$status" != "Accepted" ]]; then
    echo "ERROR: notarization of $(basename "$file") was not accepted" >&2
    cat "$out" "$err" >&2
    if [[ -n "$id" && -n "$status" ]]; then
      xcrun notarytool log "$id" \
        --key "$APPLE_API_KEY_PATH" \
        --key-id "$APPLE_API_KEY" \
        --issuer "$APPLE_API_ISSUER" >&2 || true
    elif [[ -n "$id" ]]; then
      echo "The submission may still be running at Apple. Check it with:" >&2
      echo "  xcrun notarytool info $id --key <p8 path> --key-id <key id> --issuer <issuer>" >&2
    fi
    rm -f "$out" "$err"
    return 1
  fi
  rm -f "$out" "$err"
}

# staple_and_validate <path>
staple_and_validate() {
  xcrun stapler staple "$1"
  xcrun stapler validate "$1"
}
