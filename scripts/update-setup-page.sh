#!/usr/bin/env bash
# Vendors the goto-sms setup page from a goto-sms GitHub release into src/aiogotoconnect/sms/setup/.
#   scripts/update-setup-page.sh v0.2.1
# Needs an authenticated `gh` and python3 (used to unzip; `unzip` isn't always installed).
# Afterwards update SETUP_PAGE_SHA256 in tests/sms/test_asgi.py and run the tests.
set -euo pipefail

tag="${1:?usage: scripts/update-setup-page.sh vX.Y.Z}"
[[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "not a vX.Y.Z tag: $tag" >&2; exit 1; }

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dest="$repo_root/src/aiogotoconnect/sms/setup"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

gh release download "$tag" -R oneofthegeeks/goto-sms -p 'goto-sms-setup-ui-*.zip' -D "$work/zip"
zips=("$work"/zip/*.zip)
[[ ${#zips[@]} -eq 1 ]] || { echo "expected one setup-ui zip in $tag" >&2; exit 1; }
mkdir "$work/out"
# -I: isolated mode, so nothing in the download directory is importable.
python3 -I -m zipfile -e "${zips[0]}" "$work/out"
[[ -f "$work/out/index.html" ]] || { echo "the zip has no index.html" >&2; exit 1; }

mkdir -p "$dest"
# Replace the page files; SETUP_UI_VERSION is rewritten below.
find "$dest" -mindepth 1 -maxdepth 1 ! -name SETUP_UI_VERSION -exec rm -rf {} +
cp -R "$work/out/." "$dest/"
printf '%s\n' "$tag" > "$dest/SETUP_UI_VERSION"

echo "setup page $tag vendored into $dest:"
(cd "$dest" && sha256sum index.html)
