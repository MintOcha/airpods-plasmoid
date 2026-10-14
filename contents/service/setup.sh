#!/usr/bin/env bash
# Installs and starts the AirPods user service. Run by the widget when the service is
# missing (KDE Store installs only ship the widget) and by install.sh.
set -e

SERVICE="airpods-plugin.service"
DAEMON="$(cd "$(dirname "$0")" && pwd)/daemon.py"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/$SERVICE" <<EOF
[Unit]
Description=AirPods service for the Plasma AirPods widget
After=graphical-session.target pipewire.service
PartOf=graphical-session.target

[Service]
ExecStart=/usr/bin/python3 $DAEMON
Restart=on-failure
RestartSec=3

[Install]
WantedBy=graphical-session.target
EOF

# Bluetooth codec policy: AAC at its highest variable-bitrate quality, SBC-XQ as fallback. WirePlumber only reads it
# at startup and the codec is negotiated per connection, so restart it only when the file changed.
WP_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/wireplumber/wireplumber.conf.d"
WP_CONF="51-airpods-codec.conf"
mkdir -p "$WP_DIR"
if ! cmp -s "$(dirname "$DAEMON")/wireplumber/$WP_CONF" "$WP_DIR/$WP_CONF"; then
    cp "$(dirname "$DAEMON")/wireplumber/$WP_CONF" "$WP_DIR/$WP_CONF"
    systemctl --user try-restart wireplumber.service || true
fi

systemctl --user daemon-reload
systemctl --user enable "$SERVICE"
systemctl --user restart "$SERVICE"
