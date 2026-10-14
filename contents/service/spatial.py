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
BRIR = os.path.join(ASSETS, 'brir.wav')   # Apple's captured virtual-speaker rendering: left-ear then right-ear per speaker
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
OUTPUT_GAIN = 0.53    # head-tracked renderer: same level as the fixed renderer
FIXED_GAIN = 1.08     # fixed renderer: Apple's capture plus 0.7 dB so that no program is quieter than direct playback
LIMITER_WINDOW_SECONDS = 0.01   # peak hold, gain settling time and the latency the limiter adds
LIMITER_TAPS = 24
LIMITER_CEILING = 0.93
LIMITER_SETTLE_FACTOR = 6.64   # a critically damped low-pass is within 1% of a step after 6.64 / (2 pi f0) seconds
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
UPDATE_THRESHOLD = 1.0   # degrees of head motion worth re-rendering: above sensor jitter, below what is audible
ATTACH_ATTEMPTS = 40
STREAM_GAP_SECONDS = 0.5
# One-euro filter: heavy smoothing while the head is still (kills sensor jitter), little lag while it turns
FILTER_MIN_CUTOFF_HZ = 0.6
FILTER_SPEED_GAIN = 0.05     # cutoff increase per degree/second of head speed
FILTER_SPEED_CUTOFF_HZ = 1.0


def _tracked_graph():
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

    return _assemble(nodes, links)


def _assemble(nodes, links):
    """Graph text for a renderer whose binaural mix ends at outL/outR, followed by the peak limiter."""
    limiter_nodes, limiter_links = _limiter()
    inputs = ' '.join(f'"in{ch}:In"' for ch in CHANNELS)
    return ('nodes = [\n' + '\n'.join(nodes + limiter_nodes) + '\n]\nlinks = [\n' + '\n'.join(links + limiter_links) +
            f'\n]\ninputs = [ {inputs} ]\noutputs = [ "safeL:Out" "safeR:Out" ]')


def _limiter():
    """Look-ahead peak limiter on the binaural pair, so that matching the loudness of direct playback cannot clip.

    The stereo-linked peak is held over LIMITER_WINDOW_SECONDS (taps spread so any tone frequency is sampled at many
    phases), turned into a gain that keeps it under LIMITER_CEILING and smoothed by a critically damped low-pass that
    settles within the same time. The audio is delayed by exactly one window: the gain has settled when a peak arrives
    (attack) and only starts to recover once the last loud sample has left (release). A final clamp guarantees the
    ceiling.
    """
    nodes, links = [], []
    # max-delay must exceed the longest delay: a delay equal to max-delay passes the signal through undelayed
    delay = lambda name, seconds: nodes.append(
        f'{{ type = builtin label = delay name = {name} config = {{ "max-delay" = {4 * LIMITER_WINDOW_SECONDS} }} '
        f'control = {{ "Delay (s)" = {seconds!r} }} }}')
    for side in 'LR':
        nodes.append(f'{{ type = builtin label = abs name = abs{side} }}')
        links.append(f'{{ output = "out{side}:Out" input = "abs{side}:In" }}')
    nodes.append('{ type = builtin label = max name = peak }')
    links += ['{ output = "absL:Out" input = "peak:In 1" }', '{ output = "absR:Out" input = "peak:In 2" }']
    # The hold must see every phase of any tone: evenly spaced taps would sample a 1 kHz tone at only a few phases and
    # leave a ripple that the smoothing averages away. Golden-ratio offsets cover the window evenly for any frequency.
    offsets = [0.0] + [LIMITER_WINDOW_SECONDS * ((k * 0.6180339887498949) % 1.0) for k in range(1, LIMITER_TAPS - 1)]
    offsets.append(LIMITER_WINDOW_SECONDS)
    # `max` has 8 inputs: the taps are folded through a two-level tree
    groups = [range(i, min(i + 8, LIMITER_TAPS)) for i in range(0, LIMITER_TAPS, 8)]
    nodes.append('{ type = builtin label = max name = hold }')
    for g, taps in enumerate(groups):
        nodes.append(f'{{ type = builtin label = max name = hold{g} }}')
        links.append(f'{{ output = "hold{g}:Out" input = "hold:In {g + 1}" }}')
        for slot, k in enumerate(taps, 1):
            if k == 0:
                links.append(f'{{ output = "peak:Out" input = "hold{g}:In {slot}" }}')
                continue
            delay(f'tap{k}', offsets[k])
            links += [f'{{ output = "peak:Out" input = "tap{k}:In" }}', f'{{ output = "tap{k}:Out" input = "hold{g}:In {slot}" }}']
    nodes.append(f'{{ type = builtin label = linear name = overshoot control = {{ "Mult" = {1 / LIMITER_CEILING!r} "Add" = 0.0 }} }}')
    nodes.append('{ type = builtin label = clamp name = overshootOnly control = { "Min" = 1.0 "Max" = 1000.0 } }')
    nodes.append('{ type = builtin label = recip name = reduction }')
    f0 = LIMITER_SETTLE_FACTOR / (2 * math.pi * LIMITER_WINDOW_SECONDS)
    nodes.append(f'{{ type = builtin label = bq_lowpass name = reductionSmooth control = {{ "Freq" = {f0!r} "Q" = 0.5 "Gain" = 0.0 }} }}')
    links += ['{ output = "hold:Out" input = "overshoot:In" }', '{ output = "overshoot:Out" input = "overshootOnly:In" }',
              '{ output = "overshootOnly:Out" input = "reduction:In" }', '{ output = "reduction:Out" input = "reductionSmooth:In" }']
    for side in 'LR':
        delay(f'ahead{side}', LIMITER_WINDOW_SECONDS)
        nodes.append(f'{{ type = builtin label = mult name = limited{side} }}')
        nodes.append(f'{{ type = builtin label = clamp name = safe{side} control = {{ "Min" = -1.0 "Max" = 1.0 }} }}')
        links += [f'{{ output = "out{side}:Out" input = "ahead{side}:In" }}',
                  f'{{ output = "ahead{side}:Out" input = "limited{side}:In 1" }}',
                  '{ output = "reductionSmooth:Out" input = "limited' + side + ':In 2" }',
                  f'{{ output = "limited{side}:Out" input = "safe{side}:In" }}']
    return nodes, links


def _fixed_graph():
    """Apple's captured rendering of each virtual speaker, played back as is: one convolver per speaker and ear."""
    nodes, links = [], []
    gains = ' '.join(f'"Gain {i}" = {FIXED_GAIN}' for i in range(1, len(SPEAKERS) + 1))
    for side, offset in (('L', 0), ('R', 1)):
        nodes.append(f'{{ type = builtin label = mixer name = out{side} control = {{ {gains} '
                     f'"Gain {len(SPEAKERS) + 1}" = {FIXED_GAIN * LFE_GAIN} }} }}')
        for i, (ch, _, _) in enumerate(SPEAKERS):
            nodes.append(f'{{ type = builtin label = convolver name = brir{ch}{side} '
                         f'config = {{ filename = "{BRIR}" channel = {2 * i + offset} }} }}')
            links.append(f'{{ output = "in{ch}:Out" input = "brir{ch}{side}:In" }}')
            links.append(f'{{ output = "brir{ch}{side}:Out" input = "out{side}:In {i + 1}" }}')
        links.append(f'{{ output = "inLFE:Out" input = "out{side}:In {len(SPEAKERS) + 1}" }}')
    nodes += [f'{{ type = builtin label = copy name = in{ch} }}' for ch, _, _ in SPEAKERS]
    nodes.append('{ type = builtin label = copy name = inLFE }')
    return _assemble(nodes, links)


def _graph(tracked):
    return _tracked_graph() if tracked else _fixed_graph()


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


def _alpha(cutoff_hz, dt):
    """Smoothing factor of a first-order low-pass with the given cutoff, for samples dt seconds apart."""
    return 1.0 / (1.0 + 1.0 / (2.0 * math.pi * cutoff_hz * dt))


def _mul(a, b):
    w1, x1, y1, z1 = a
    w2, x2, y2, z2 = b
    return (w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
            w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
            w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
            w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2)


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
        self.smooth_yaw = 0.0
        self.smooth_pitch = 0.0
        self.speed = 0.0

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

        # One-euro filter on the wrapped difference, so turning past +-180 degrees does not swing round
        yaw_step = (yaw - self.smooth_yaw + 180.0) % 360.0 - 180.0
        pitch_step = pitch - self.smooth_pitch
        self.speed += _alpha(FILTER_SPEED_CUTOFF_HZ, dt) * (math.hypot(yaw_step, pitch_step) / dt - self.speed)
        alpha = _alpha(FILTER_MIN_CUTOFF_HZ + FILTER_SPEED_GAIN * self.speed, dt)
        self.smooth_yaw = (self.smooth_yaw + alpha * yaw_step + 180.0) % 360.0 - 180.0
        self.smooth_pitch += alpha * pitch_step
        return self.smooth_yaw, self.smooth_pitch


class Spatializer:
    def __init__(self, on_change):
        self.on_change = on_change
        self.engine = None
        self.control = None
        self.node_id = None
        self.target = None
        self.tracked = False
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

    def start(self, target, description, tracked):
        if self.engine and (self.target, self.tracked) == (target, tracked):
            return
        self.stop()
        self.target, self.tracked = target, tracked
        path = os.path.join(GLib.get_user_runtime_dir(), 'airpods-spatial.conf')
        with open(path, 'w') as f:
            f.write(CONF.format(description=description, graph=_graph(tracked), sink=SINK_NAME, target=target,
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
        self.tracked = False
        self.reset_steering()
        if was_active:
            self.on_change()

    def steer(self, yaw, pitch):
        """Render the virtual speakers for a head pose; poses arriving mid-switch coalesce into the next switch."""
        if not self.control or not self.tracked:
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
        self.switching = None
        if self.pending:
            pose, self.pending = self.pending, None
            self.steer(*pose)
        return False

    def center(self):
        self.steer(0.0, 0.0)
