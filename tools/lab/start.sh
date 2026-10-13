#!/bin/bash
# Boots PipeWire with a 48 kHz null sink ("airpods_sim") standing in for the AirPods A2DP sink.
mkdir -p "$XDG_RUNTIME_DIR" && chmod 700 "$XDG_RUNTIME_DIR"
dbus-daemon --session --address="$DBUS_SESSION_BUS_ADDRESS" --fork
pipewire >/tmp/pipewire.log 2>&1 &
sleep 1
wireplumber >/tmp/wireplumber.log 2>&1 &
pipewire-pulse >/tmp/pipewire-pulse.log 2>&1 &
sleep 2
pactl load-module module-null-sink sink_name=airpods_sim rate=48000 channels=2 >/dev/null
pactl set-default-sink airpods_sim
exec sleep infinity
