"""Spatial audio: a PipeWire filter-chain that renders every channel as a virtual speaker through a dense
measured HRTF set, steered by AirPods head tracking.

Head tracking re-renders the virtual speakers with two filter banks: the idle bank is moved to the new head pose,
given time to settle, then crossfaded in. Neither bank's filters ever change while audible, and the crossfade
weight is one control smoothed at audio rate, so no update can step or tear.
"""

import json
import math
import os
import subprocess
import sys
import time
from gi.repository import GLib

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'binaural')
HRTF = os.path.join(ASSETS, 'hrtf.sofa')
ROOM = os.path.join(ASSETS, 'room.wav')   # per speaker: late field at the left ear, then the right ear
SINK_NAME = 'airpods_spatial'

# Virtual speaker layout (ITU-R BS.775 7.1): (channel, azimuth counter-clockwise from front, elevation) in degrees
SPEAKERS = [
    ('FL', 30.0, 0.0),
    ('FR', -30.0, 0.0),
    ('FC', 0.0, 0.0),
    ('SL', 90.0, 0.0),
    ('SR', -90.0, 0.0),
    ('RL', 150.0, 0.0),
    ('RR', -150.0, 0.0),
]
CHANNELS = ['FL', 'FR', 'FC', 'LFE', 'RL', 'RR', 'SL', 'SR']
LFE_GAIN = 0.5
OUTPUT_GAIN = 0.37    # full-scale bass-heavy mixes stay below 0 dBFS (no limiter in the chain)
BANKS = ('A', 'B')

# Rendering cadence. The engine runs at a fixed rate and requests a small processing block so the idle bank's
# convolvers have refilled their history (one block after a move) well within the settle time.
RATE = 48000
QUANTUM = 512
SETTLE_SECONDS = 2 * QUANTUM / RATE
FADE_SECONDS = 0.03                    # crossfade weight is within -80 dB of its target after this long
FADE_POLE = math.exp(-12.0 / (FADE_SECONDS * RATE))

# Head tracking constants
RECENTER_SECONDS = 12.0
UPDATE_THRESHOLD = 1.0   # degrees of head motion worth re-rendering
SWITCH_COOLDOWN_SECONDS = 0.08
ATTACH_ATTEMPTS = 40
STREAM_GAP_SECONDS = 0.5
FILTER_MIN_CUTOFF_HZ = 0.8     # smoothing cutoff while the head is still
FILTER_SPEED_GAIN = 0.03       # extra cutoff Hz per degree/second of head speed
FILTER_SPEED_CUTOFF_HZ = 1.0   # smoothing of the speed estimate itself


def _graph():
    nodes, links = [], []
    for ch, azimuth, elevation in SPEAKERS:
        nodes.append(f'{{ type = builtin label = copy name = in{ch} }}')
        for bank in BANKS:
            nodes.append(f'{{ type = sofa label = spatializer name = spk{ch}{bank} config = {{ filename = "{HRTF}" }} '
                         f'control = {{ "Azimuth" = {azimuth % 360.0} "Elevation" = {elevation} }} }}')
            links.append(f'{{ output = "in{ch}:Out" input = "spk{ch}{bank}:In" }}')
    nodes.append('{ type = builtin label = copy name = inLFE }')

    # The virtual room stays fixed to the head: its late field is diffuse, so it does not need steering
    for i, (ch, _, _) in enumerate(SPEAKERS):
        for side, offset in (('L', 0), ('R', 1)):
            nodes.append(f'{{ type = builtin label = convolver name = room{ch}{side} '
                         f'config = {{ filename = "{ROOM}" channel = {2 * i + offset} }} }}')
            links.append(f'{{ output = "in{ch}:Out" input = "room{ch}{side}:In" }}')

    # Crossfade weight of bank B: a constant set by one control, smoothed by a critically damped two-pole
    # low-pass (monotonic S-curve, no overshoot). Bank A gets the complement.
    nodes.append('{ type = builtin label = sine name = fade control = { "Freq" = 0.0 "Ampl" = 0.0 "Offset" = 0.0 } }')
    # bq_raw takes its coefficients from config; the matching controls keep port defaults from replacing them
    poles = {'b0': (1 - FADE_POLE) ** 2, 'b1': 0.0, 'b2': 0.0, 'a0': 1.0, 'a1': -2 * FADE_POLE, 'a2': FADE_POLE ** 2}
    coefficients = ' '.join(f'{k} = {v!r}' for k, v in poles.items())
    controls = ' '.join(f'"{k}" = {v!r}' for k, v in poles.items())
    nodes.append(f'{{ type = builtin label = bq_raw name = weightB config = {{ coefficients = [ '
                 f'{{ rate = {RATE} {coefficients} }} ] }} control = {{ {controls} }} }}')
    nodes.append('{ type = builtin label = linear name = weightA control = { "Mult" = -1.0 "Add" = 1.0 } }')
    links.append('{ output = "fade:Out" input = "weightB:In" }')
    links.append('{ output = "weightB:Out" input = "weightA:In" }')

    gains = ' '.join(f'"Gain {i}" = {OUTPUT_GAIN}' for i in range(1, len(SPEAKERS) + 1))
    for side, port in (('L', 'Out L'), ('R', 'Out R')):
        for bank in BANKS:
            nodes.append(f'{{ type = builtin label = mixer name = sum{side}{bank} control = {{ {gains} }} }}')
            for i, (ch, _, _) in enumerate(SPEAKERS, 1):
                links.append(f'{{ output = "spk{ch}{bank}:{port}" input = "sum{side}{bank}:In {i}" }}')
            nodes.append(f'{{ type = builtin label = mult name = faded{side}{bank} }}')
            links.append(f'{{ output = "sum{side}{bank}:Out" input = "faded{side}{bank}:In 1" }}')
            links.append(f'{{ output = "weight{bank}:Out" input = "faded{side}{bank}:In 2" }}')
        nodes.append(f'{{ type = builtin label = mixer name = room{side} control = {{ {gains} }} }}')
        for i, (ch, _, _) in enumerate(SPEAKERS, 1):
            links.append(f'{{ output = "room{ch}{side}:Out" input = "room{side}:In {i}" }}')
        nodes.append(f'{{ type = builtin label = mixer name = out{side} control = {{ "Gain 1" = 1.0 "Gain 2" = 1.0 '
                     f'"Gain 3" = {OUTPUT_GAIN * LFE_GAIN} "Gain 4" = 1.0 }} }}')
        links.append(f'{{ output = "faded{side}A:Out" input = "out{side}:In 1" }}')
        links.append(f'{{ output = "faded{side}B:Out" input = "out{side}:In 2" }}')
        links.append(f'{{ output = "inLFE:Out" input = "out{side}:In 3" }}')
        links.append(f'{{ output = "room{side}:Out" input = "out{side}:In 4" }}')

    port_of = {ch: f'"in{ch}:In"' for ch in CHANNELS}
    inputs = ' '.join(port_of[ch] for ch in CHANNELS)
    return ('nodes = [\n' + '\n'.join(nodes) + '\n]\nlinks = [\n' + '\n'.join(links) +
            f'\n]\ninputs = [ {inputs} ]\noutputs = [ "outL:Out" "outR:Out" ]')


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
            node.latency = "{quantum}/{rate}"
            audio.rate = {rate}
            filter.graph = {{
{graph}
            }}
            capture.props = {{
                node.name = "{sink}"
                media.class = Audio/Sink
                audio.channels = {channels}
                audio.position = [ {positions} ]
                channelmix.normalize = false
                channelmix.upmix = false
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


def source_direction(azimuth, elevation, yaw, pitch):
    """Direction (azimuth, elevation) of a world-fixed source as heard by a head turned by yaw/pitch."""
    az, el, yw, pt = map(math.radians, (azimuth, elevation, yaw, pitch))
    x, y, z = math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el)
    x, y = x * math.cos(yw) + y * math.sin(yw), y * math.cos(yw) - x * math.sin(yw)
    x, z = x * math.cos(pt) + z * math.sin(pt), z * math.cos(pt) - x * math.sin(pt)
    return math.degrees(math.atan2(y, x)) % 360.0, math.degrees(math.asin(max(-1.0, min(1.0, z))))


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
    return (w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
            w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
            w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
            w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2)


class SpeedAdaptiveFilter:
    """One-euro low-pass: the cutoff rises with the signal's speed. `wrap` filters an angle in degrees."""

    def __init__(self, wrap=False):
        self.wrap = wrap
        self.value = None
        self.speed = 0.0

    @staticmethod
    def _alpha(dt, cutoff_hz):
        return 1.0 / (1.0 + 1.0 / (2.0 * math.pi * cutoff_hz * dt))

    def __call__(self, x, dt):
        dt = max(dt, 1e-3)
        if self.value is None:
            self.value = x
            return x
        diff = x - self.value
        if self.wrap:
            diff = (diff + 180.0) % 360.0 - 180.0
        self.speed += self._alpha(dt, FILTER_SPEED_CUTOFF_HZ) * (abs(diff) / dt - self.speed)
        self.value += self._alpha(dt, FILTER_MIN_CUTOFF_HZ + FILTER_SPEED_GAIN * self.speed) * diff
        if self.wrap:
            self.value = (self.value + 180.0) % 360.0 - 180.0
        return self.value


class HeadPose:
    """Yaw/pitch tracking from AirPods quaternions with motion-aware stabilization."""

    def __init__(self):
        self.recenter()

    def recenter(self):
        self.front = None
        self.previous = None
        self.last_time = 0.0
        self.still_time = 0.0
        self.last_raw = (0.0, 0.0)
        self.yaw_filter = SpeedAdaptiveFilter(wrap=True)
        self.pitch_filter = SpeedAdaptiveFilter()

    def update(self, vector):
        now = time.monotonic()
        dt = (now - self.last_time) if self.last_time else 0.02
        if self.front is not None and self.last_time and (now - self.last_time) > STREAM_GAP_SECONDS:
            self.recenter()
            dt = 0.02
        self.last_time = now

        # Reconstruct quaternion: w = sqrt(max(0, 1 - sum(v^2)))
        v_sq = sum(v * v for v in vector)
        w = math.sqrt(max(0.0, 1.0 - v_sq))
        q = (w, *vector)

        # Enforce quaternion continuity on S^3 (prevent antipodal flips)
        if self.previous is not None:
            dot = sum(a * b for a, b in zip(q, self.previous))
            if dot < 0.0:
                q = tuple(-x for x in q)
        self.previous = q

        if self.front is None:
            self.front = q
            return 0.0, 0.0

        # Relative rotation: q_rel = q * front^(-1)
        front = self.front
        if sum(a * b for a, b in zip(q, front)) < 0.0:
            front = tuple(-x for x in front)

        w, x, y, z = _mul(q, (front[0], -front[1], -front[2], -front[3]))
        yaw = math.degrees(math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)))
        pitch = math.degrees(math.asin(max(-1.0, min(1.0, 2 * (w * x - y * z)))))

        # Motion-aware slow auto-recenter: only drift when head is held still
        raw_delta = math.hypot(yaw - self.last_raw[0], pitch - self.last_raw[1])
        self.last_raw = (yaw, pitch)

        if raw_delta < 1.0:
            self.still_time += dt
            if self.still_time > 4.0:
                blend_k = min(0.5, dt / RECENTER_SECONDS)
                blended = tuple(f + (c - f) * blend_k for f, c in zip(self.front, q))
                norm = math.hypot(*blended)
                if norm > 0:
                    self.front = tuple(f / norm for f in blended)
        else:
            self.still_time = 0.0

        # Speed-adaptive low-pass: heavy smoothing while the head is still (sensor jitter never reaches the
        # renderer), light smoothing while it turns (no lag)
        return self.yaw_filter(yaw, dt), self.pitch_filter(pitch, dt)


class Spatializer:
    def __init__(self, on_change):
        self.on_change = on_change
        self.engine = None
        self.control = None
        self.node_id = None
        self.target = None
        self.attempts = 0
        self.reset_steering()

    def reset_steering(self):
        self.bank = 'A'
        self.rendered = (0.0, 0.0)
        self.pending = None
        self.switching = None

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
                                channels=len(CHANNELS), positions=' '.join(CHANNELS), quantum=QUANTUM, rate=RATE))
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
                self.on_change()
                return False
        if self.attempts >= ATTACH_ATTEMPTS or self.engine.poll() is not None:
            print('spatial audio engine did not start (needs PipeWire with the SOFA filter plugin, libmysofa)',
                  file=sys.stderr)
            self.stop()
            return False
        return True

    def stop(self):
        was_active = self.control is not None
        if was_active and self.target:
            _run('pactl', 'set-default-sink', self.target)
            time.sleep(0.05)
        for proc in (self.control, self.engine):
            if proc:
                proc.kill()
                proc.wait()
        if self.switching:
            GLib.source_remove(self.switching)
        self.engine = self.control = self.node_id = self.target = None
        self.reset_steering()
        if was_active:
            self.on_change()

    def steer(self, yaw, pitch):
        """Render the virtual speakers for a head pose; poses arriving mid-switch coalesce into the next switch."""
        if not self.control:
            return
        if self.switching:
            self.pending = (yaw, pitch)
            return
        turned = abs((yaw - self.rendered[0] + 180.0) % 360.0 - 180.0)
        if max(turned, abs(pitch - self.rendered[1])) < UPDATE_THRESHOLD:
            return
        idle = 'B' if self.bank == 'A' else 'A'
        directions = []
        for ch, azimuth, elevation in SPEAKERS:
            az, el = source_direction(azimuth, elevation, yaw, pitch)
            directions.append(f'"spk{ch}{idle}:Azimuth" {az:.2f} "spk{ch}{idle}:Elevation" {el:.2f}')
        if not self._set(directions):
            return
        self.bank, self.rendered = idle, (yaw, pitch)
        self.switching = GLib.timeout_add(math.ceil(SETTLE_SECONDS * 1000), self._fade)

    def _fade(self):
        self.switching = None
        if self._set([f'"fade:Offset" {1.0 if self.bank == "B" else 0.0}']):
            self.switching = GLib.timeout_add(math.ceil(FADE_SECONDS * 1000), self._switched)
        return False

    def _set(self, params):
        try:
            self.control.stdin.write(f's {self.node_id} Props {{ params = [ {" ".join(params)} ] }}\n')
            self.control.stdin.flush()
            return True
        except OSError:
            self.stop()
            return False

    def _switched(self):
        # Cooldown: bank switches are rate-limited so continuous motion cannot modulate the sound
        self.switching = GLib.timeout_add(math.ceil(SWITCH_COOLDOWN_SECONDS * 1000), self._ready)
        return False

    def _ready(self):
        self.switching = None
        if self.pending:
            pose, self.pending = self.pending, None
            self.steer(*pose)
        return False

    def center(self):
        self.steer(0.0, 0.0)
