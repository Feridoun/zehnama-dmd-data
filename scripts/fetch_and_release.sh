#!/usr/bin/env bash
# fetch_and_release.sh — fetch the latest NHS TRUD dm+d, convert to the
# Zehnama JSON pack format, and publish a GitHub release on this repo.
#
# Requires:
#   TRUD_API_KEY env var — your NHS TRUD API key
#     (isd.digital.nhs.uk/trud → account → API key). The dm+d pack licence
#     must have been accepted once manually in the TRUD web UI.
#   gh CLI authenticated with `write` on releases for this repo.
#   python3, jq, curl, unzip on PATH.
#
# Safety checks (match the in-app DmdUpdateService floor + drawer warnings):
#   - Aborts if the converted pack has fewer than 100 medications.
#   - Warns and asks for confirmation if size deviates >50% from the previous
#     full (non-prerelease) release.
#   - Refuses to clobber an existing release with the same tag.
#
# Usage:
#   TRUD_API_KEY=xxx ./scripts/fetch_and_release.sh [--dry-run] [--version YYYY.MM.DD]

set -euo pipefail

DRY_RUN=0
VERSION_OVERRIDE=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --dry-run) DRY_RUN=1; shift ;;
    --version) VERSION_OVERRIDE="$2"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

: "${TRUD_API_KEY:?Set TRUD_API_KEY env var (isd.digital.nhs.uk/trud -> account -> API key)}"

REPO="Feridoun/zehnama-dmd-data"
ITEM_ID=24   # NHS BSA dm+d weekly release pack
TRUD_API="https://isd.digital.nhs.uk/trud/api/v1"

for tool in gh python3 jq curl unzip; do
  command -v "$tool" >/dev/null 2>&1 || { echo "$tool not on PATH" >&2; exit 2; }
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONVERTER="$REPO_ROOT/scripts/convert_trud_dmd.py"
[[ -f "$CONVERTER" ]] || { echo "Converter missing at $CONVERTER" >&2; exit 2; }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# --- 1. Query TRUD for latest release ---------------------------------------
echo "[1/6] Querying TRUD for the latest dm+d release..."
META=$(curl -fsS "$TRUD_API/keys/$TRUD_API_KEY/items/$ITEM_ID/releases?latest")
ARCHIVE_URL=$(echo "$META" | jq -r '.releases[0].archiveFileUrl // empty')
RELEASE_DATE=$(echo "$META" | jq -r '.releases[0].releaseDate // empty')
if [[ -z "$ARCHIVE_URL" || -z "$RELEASE_DATE" ]]; then
  echo "TRUD returned no release. Is the dm+d pack subscribed under your account?" >&2
  exit 1
fi

VERSION="${VERSION_OVERRIDE:-${RELEASE_DATE//-/.}}"
echo "      TRUD release date: $RELEASE_DATE -> tag: $VERSION"

if gh release view "$VERSION" --repo "$REPO" >/dev/null 2>&1; then
  echo "Release $VERSION already exists on $REPO. Pass --version to override." >&2
  exit 1
fi

# --- 2-5. Download / extract / convert / sanity-check ------------------------
echo "[2/6] Downloading TRUD archive..."
curl -fsSL "$ARCHIVE_URL" -o "$WORK/trud.zip"
ZIP_BYTES=$(stat -c%s "$WORK/trud.zip" 2>/dev/null || stat -f%z "$WORK/trud.zip")
echo "      archive: $((ZIP_BYTES / 1024 / 1024)) MB"

echo "[3/6] Extracting..."
mkdir -p "$WORK/extract" "$WORK/dist"
unzip -q "$WORK/trud.zip" -d "$WORK/extract"

echo "[4/6] Converting -> JSON pack..."
python3 "$CONVERTER" "$WORK/extract" --version "$VERSION" --out "$WORK/dist"

PACK="$WORK/dist/dmd_medications.json"
DIGEST="$WORK/dist/dmd_medications.json.sha256"
if [[ ! -f "$PACK" || ! -f "$DIGEST" ]]; then
  echo "Converter did not produce both pack + sha256 sidecar." >&2
  exit 1
fi

COUNT=$(python3 -c "import json,sys; print(len(json.load(open(sys.argv[1]))['medications']))" "$PACK")
echo "[5/6] Sanity-checking: $COUNT medications"
if [[ "$COUNT" -lt 100 ]]; then
  echo "ABORT: pack has $COUNT entries -- below the 100-entry production floor." >&2
  exit 1
fi

# Size delta vs previous full (non-prerelease) release.
PREV=$(gh release list --repo "$REPO" --limit 10 --json tagName,isPrerelease \
       | jq -r '[.[] | select(.isPrerelease==false)][0].tagName // empty')
if [[ -n "$PREV" ]]; then
  PREV_SIZE=$(gh release view "$PREV" --repo "$REPO" --json assets \
              | jq -r '.assets[] | select(.name=="dmd_medications.json") | .size // empty')
  NEW_SIZE=$(stat -c%s "$PACK" 2>/dev/null || stat -f%z "$PACK")
  if [[ -n "$PREV_SIZE" && "$PREV_SIZE" -gt 0 ]]; then
    DELTA=$(( (NEW_SIZE - PREV_SIZE) * 100 / PREV_SIZE ))
    ABS_DELTA=${DELTA#-}
    echo "      previous $PREV: $PREV_SIZE bytes; new: $NEW_SIZE bytes (delta ${DELTA}%)"
    if [[ "$ABS_DELTA" -gt 50 ]]; then
      echo "      WARNING: size delta exceeds +/-50%. Inspect $PACK before publishing."
      if [[ "$DRY_RUN" -eq 0 ]]; then
        read -rp "      Continue? [y/N] " ans
        [[ "$ans" == "y" ]] || exit 1
      fi
    fi
  fi
else
  echo "      No prior full release on record; skipping size-delta check."
fi

# --- 6. Publish --------------------------------------------------------------
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "[6/6] --dry-run set; skipping GitHub release."
  echo "      Pack:   $PACK"
  echo "      Digest: $DIGEST"
  exit 0
fi

echo "[6/6] Publishing release $VERSION to $REPO..."
NOTES="NHS BSA dm+d release $RELEASE_DATE -- $COUNT medications.

SHA-256 in sidecar; verified by Zehnama before install."

gh release create "$VERSION" "$PACK" "$DIGEST" \
  --repo "$REPO" \
  --title "dm+d Pack $VERSION" \
  --notes "$NOTES"

echo "Done. Triggering check for the validate-release workflow..."
sleep 5
gh run list --repo "$REPO" --workflow validate-release.yml --limit 1 || true
echo "Watch: gh run watch --repo $REPO"
