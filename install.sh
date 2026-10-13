#!/usr/bin/env bash
set -e
# AirPods Plasmoid - Install Script

PLASMOID_ID="com.mintocha.airpods"
cd "$(dirname "$0")"

echo "==> Installing AirPods plasmoid..."
kpackagetool6 --type Plasma/Applet --upgrade . 2>/dev/null || \
kpackagetool6 --type Plasma/Applet --install .

echo "==> Starting background service..."
bash "$HOME/.local/share/plasma/plasmoids/$PLASMOID_ID/contents/service/setup.sh"

echo "==> Restarting Plasma..."
systemctl --user restart plasma-plasmashell.service

echo ""
echo "==> Done! Right-click panel -> Add Widgets -> AirPods"
