"""Spatial audio: a PipeWire filter-chain that renders audio through HRTF virtual
7.1 speakers (stereo is upmixed, like Apple's "Spatialize Stereo"), optionally
steered by AirPods head tracking."""

import json
import math
import os
import subprocess
import time

from gi.repository import GLib

HRTF = '/usr/share/libmysofa/default.sofa'
SINK_NAME = 'airpods_spatial'
# Virtual speaker layout (ITU-R BS.775 7.1): channel -> (azimuth, elevation), azimuth counter-clockwise.
SPEAKERS = {'FL': (30, 0), 'FR': (330, 0), 'FC': (0, 0), 'LFE': (0, -60),
            'RL': (150, 0), 'RR': (210, 0), 'SL': (90, 0), 'SR': (270, 0)}
RECENTER_SECONDS = 8.0      # a held head pose slowly becomes the new front, as on Apple devices
UPDATE_INTERVAL = 0.05
UPDATE_THRESHOLD = 1.5      # degrees
ATTACH_ATTEMPTS = 40
STREAM_GAP_SECONDS = 0.5
# Per-speaker HRTF gain, calibrated so music-like noise through the spatializer matches
# the loudness of the direct AirPods sink.
SPEAKER_GAIN = 1.64


def _graph():
    nodes, links = [], []
    for i, (ch, (az, el)) in enumerate(SPEAKERS.items(), 1):
        nodes.append(f'{{ type = sofa label = spatializer name = sp{ch} '
                     f'config = {{ filename = "{HRTF}" gain = {SPEAKER_GAIN} }} '
                     f'control = {{ "Azimuth" = {az}.0 "Elevation" = {el}.0 "Radius" = 1.0 }} }}')
        links.append(f'{{ output = "sp{ch}:Out L" input = "mixL:In {i}" }}')
        links.append(f'{{ output = "sp{ch}:Out R" input = "mixR:In {i}" }}')
    nodes += ['{ type = builtin label = mixer name = mixL }', '{ type = builtin label = mixer name = mixR }']
    inputs = ' '.join(f'"sp{ch}:In"' for ch in SPEAKERS)
    return ('nodes = [\n' + '\n'.join(nodes) + '\n]\nlinks = [\n' + '\n'.join(links) +
            f'\n]\ninputs = [ {inputs} ]\noutputs = [ "mixL:Out" "mixR:Out" ]')


CONF = """context.properties = {{ log.level = 0 }}
context.spa-libs = {{
    audio.convert.* = audioconvert/libspa-audioconvert
    support.*       = support/libspa-support
}}
context.modules = [
    {{ name = libpipewire-module-rt flags = [ ifexists nofail ] }}
    {{ name = libpipewire-module-protocol-native }}
    {{ name = libpipewire-module-client-node }}
    {{ name = libpipewire-module-adapter }}
    {{ name = libpipewire-module-filter-chain
        args = {{
            node.description = "{description}"
            filter.graph = {{
{graph}
            }}
            capture.props = {{
                node.name = "{sink}"
                media.class = Audio/Sink
                audio.channels = {channels}
                audio.position = [ {positions} ]
                channelmix.upmix = true
                channelmix.upmix-method = psd
                channelmix.lfe-cutoff = 120
                channelmix.fc-cutoff = 6000
                channelmix.rear-delay = 12.0
            }}
            playback.props = {{
                node.name = "{sink}.output"
                node.passive = true
                node.dont-fallback = true
                target.object = "{target}"
                audio.channels = 2
                audio.position = [ FL FR ]
            }}
        }}
    }}
]
"""


def _run(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ''


def airpods_sink(address):
    """PipeWire sink of the AirPods, or None while no A2DP sink exists."""
    for sink in json.loads(_run('pactl', '-f', 'json', 'list', 'sinks') or '[]'):
        if sink.get('properties', {}).get('api.bluez5.address') == address:
            return sink['name']
    return None


def _mul(a, b):
    w1, x1, y1, z1 = a
    w2, x2, y2, z2 = b
    return (w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2, w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
            w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2, w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2)


class HeadPose:
    """Yaw/pitch of the head relative to a slowly re-centring front direction.

    Calibrated on AirPods Pro 2: turning left gives positive yaw, looking up positive pitch.
    """

    def __init__(self):
        self.recenter()

    def recenter(self):
        self.front = None
        self.previous = None
        self.last = 0.0

    def _rotation(self, vector):
        # The pods send only (x, y, z); of the two possible w signs, pick the one continuing
        # the previous sample, otherwise the decoded rotation mirrors near a half turn.
        w = math.sqrt(max(0.0, 1 - sum(v * v for v in vector)))
        candidates = [(w, *vector), (-w, *vector)]
        if self.previous is None:
            return candidates[0]
        return max(candidates, key=lambda q: abs(sum(a * b for a, b in zip(q, self.previous))))

    def update(self, vector):
        now = time.monotonic()
        if self.front is not None and now - self.last > STREAM_GAP_SECONDS:
            self.recenter()     # the pods restart their reference frame when the stream resumes
        q = self._rotation(vector)
        self.previous = q
        if self.front is None:
            self.front, self.last = q, now
            return 0.0, 0.0
        if sum(a * b for a, b in zip(q, self.front)) < 0:
            q = tuple(-v for v in q)
        k = min(1.0, (now - self.last) / RECENTER_SECONDS)
        self.last = now
        blend = tuple(f + (c - f) * k for f, c in zip(self.front, q))
        norm = math.sqrt(sum(v * v for v in blend))
        self.front = tuple(v / norm for v in blend)
        w, x, y, z = _mul(q, (self.front[0], -self.front[1], -self.front[2], -self.front[3]))
        yaw = math.degrees(math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)))
        pitch = math.degrees(math.asin(max(-1.0, min(1.0, 2 * (w * x - y * z)))))
        return yaw, pitch


class Spatializer:
    def __init__(self, on_change):
        self.on_change = on_change
        self.engine = None
        self.control = None
        self.node_id = None
        self.target = None
        self.sent = (0.0, 0.0)
        self.sent_at = 0.0
        self.attempts = 0

    @property
    def active(self):
        return self.control is not None

    def start(self, target, description):
        if self.engine and self.target == target:
            return
        self.stop()
        self.target = target
        path = os.path.join(GLib.get_user_runtime_dir(), 'airpods-spatial.conf')
        with open(path, 'w') as f:
            f.write(CONF.format(description=description, graph=_graph(), sink=SINK_NAME, target=target,
                                channels=len(SPEAKERS), positions=' '.join(SPEAKERS)))
        self.engine = subprocess.Popen(['pipewire', '-c', path],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.attempts = 0
        GLib.timeout_add(250, self._attach)

    def _attach(self):
        if not self.engine:
            return False
        self.attempts += 1
        for obj in json.loads(_run('pw-dump') or '[]'):
            props = (obj.get('info') or {}).get('props') or {}
            if props.get('node.name') == SINK_NAME and obj.get('type', '').endswith('Node'):
                self.node_id = obj['id']
                self.control = subprocess.Popen(['pw-cli'], stdin=subprocess.PIPE, text=True,
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                _run('pactl', 'set-default-sink', SINK_NAME)
                self.center()
                self.on_change()
                return False
        if self.attempts >= ATTACH_ATTEMPTS or self.engine.poll() is not None:
            self.stop()
            return False
        return True

    def stop(self):
        was_active = self.control is not None
        if was_active and self.target:
            _run('pactl', 'set-default-sink', self.target)
        for proc in (self.control, self.engine):
            if proc:
                proc.kill()
                proc.wait()
        self.engine = self.control = self.node_id = self.target = None
        if was_active:
            self.on_change()

    def steer(self, yaw, pitch, force=False):
        now = time.monotonic()
        if not self.control:
            return
        if not force and (now - self.sent_at < UPDATE_INTERVAL or
                          max(abs(yaw - self.sent[0]), abs(pitch - self.sent[1])) < UPDATE_THRESHOLD):
            return
        self.sent, self.sent_at = (yaw, pitch), now
        params = []
        for ch, (az, el) in SPEAKERS.items():
            params += [f'"sp{ch}:Azimuth" {(az - yaw) % 360:.1f}',
                       f'"sp{ch}:Elevation" {max(-90.0, min(90.0, el - pitch)):.1f}']
        try:
            self.control.stdin.write(f's {self.node_id} Props {{ params = [ {" ".join(params)} ] }}\n')
            self.control.stdin.flush()
        except OSError:
            self.stop()

    def center(self):
        self.steer(0.0, 0.0, force=True)
