#!/usr/bin/env bash
set -e
# Builds the KDE Store upload: dist/com.mintocha.airpods-<version>.plasmoid
# (a zip of metadata.json + contents/, installable with kpackagetool6 or "Get New Widgets").

cd "$(dirname "$0")"
PLASMOID_ID=$(python3 -c "import json;print(json.load(open('metadata.json'))['KPlugin']['Id'])")
VERSION=$(python3 -c "import json;print(json.load(open('metadata.json'))['KPlugin']['Version'])")
OUT="dist/$PLASMOID_ID-$VERSION.plasmoid"

mkdir -p dist
rm -f "$OUT"
zip -qr -X "$OUT" metadata.json LICENSE contents -x '*/__pycache__/*' '*.pyc'

# Verify it installs into a scratch location.
SCRATCH=$(mktemp -d)
trap 'rm -rf "$SCRATCH"' EXIT
kpackagetool6 --type Plasma/Applet --packageroot "$SCRATCH" --install "$OUT" >/dev/null
test -f "$SCRATCH/$PLASMOID_ID/contents/ui/main.qml"

echo "$OUT"
