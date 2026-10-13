"""Host-side behaviours Apple devices perform for AirPods: pausing on ear removal,
ducking for Conversation Awareness and routing audio to the AirPods."""

import json
import subprocess

import dbus

MPRIS_PREFIX = 'org.mpris.MediaPlayer2.'
MPRIS_PATH = '/org/mpris/MediaPlayer2'
MPRIS_PLAYER = 'org.mpris.MediaPlayer2.Player'
DUCK_FLOOR = 0.2            # volume fraction while the wearer is speaking
SPEAKING, NORMAL = 3, 8     # conversation levels: <=3 speaking, >=8 normal, between ramps


def pactl(*args):
    try:
        return subprocess.run(['pactl', *args], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ''


def default_sink():
    return pactl('get-default-sink').strip()


def set_default_sink(name):
    pactl('set-default-sink', name)


def sink_volume(name):
    for sink in json.loads(pactl('-f', 'json', 'list', 'sinks') or '[]'):
        if sink['name'] == name:
            channels = sink.get('volume', {}).values()
            return max((c['value'] for c in channels), default=0)
    return None


def set_sink_volume(name, raw):
    pactl('set-sink-volume', name, str(int(raw)))


class Players:
    """Pauses playing MPRIS players and resumes exactly the ones it paused."""

    def __init__(self, bus):
        self.bus = bus
        self.paused = set()

    def _player(self, name):
        return dbus.Interface(self.bus.get_object(name, MPRIS_PATH), MPRIS_PLAYER)

    def pause(self):
        for name in self.bus.list_names():
            if not name.startswith(MPRIS_PREFIX):
                continue
            try:
                props = dbus.Interface(self.bus.get_object(name, MPRIS_PATH), dbus.PROPERTIES_IFACE)
                if props.Get(MPRIS_PLAYER, 'PlaybackStatus') == 'Playing':
                    self._player(name).Pause()
                    self.paused.add(name)
            except dbus.DBusException:
                continue

    def resume(self):
        for name in self.paused:
            try:
                self._player(name).Play()
            except dbus.DBusException:
                continue
        self.paused.clear()

    def forget(self):
        self.paused.clear()


class Ducker:
    """Lowers a sink while the wearer talks and restores the level they had."""

    def __init__(self):
        self.sink = None
        self.original = None

    def level(self, sink, level):
        if level >= NORMAL:
            self.restore()
            return
        if self.original is None:
            self.original = sink_volume(sink)
            self.sink = sink
            if self.original is None:
                return
        ramp = min(1.0, max(0.0, (level - SPEAKING) / (NORMAL - SPEAKING)))
        set_sink_volume(self.sink, self.original * (DUCK_FLOOR + (1 - DUCK_FLOOR) * ramp))

    def restore(self):
        if self.original is not None:
            set_sink_volume(self.sink, self.original)
        self.sink = self.original = None
