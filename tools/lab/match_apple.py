"""Compares the renderer, driven through the real PipeWire engine, with Apple's captured Spatial Audio rendering.

For FC, FL, SL, RL and the FL+FR phantom centre: sweeps that input alone, captures both ears and compares with
Apple's BRIR capture (columns matched by apple_reference.brir_layout):
  - magnitude per third octave and ear, after one common gain (capture levels are arbitrary), 63 Hz - 14 kHz;
  - interaural delay per third octave on the lateral speakers;
  - room decay time and late-to-direct level per octave.

Usage (inside the lab container): python3 match_apple.py <apple dry.wav> <apple brir.wav> [tag]
"""

import json
import os
import sys

import numpy as np
from scipy import signal

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import measure  # noqa: E402
import apple_reference as ar  # noqa: E402

RATE = measure.RATE
CHANNELS = ['FL', 'FR', 'FC', 'LFE', 'RL', 'RR', 'SL', 'SR']
THIRDS = np.array([round(1000 * 2 ** (k / 3), 1) for k in range(-12, 12)])
ALIGNMENT_BANDS = (THIRDS >= 500) & (THIRDS <= 4000)


def thirds_db(x):
    return np.array(ar.band_levels(x, RATE, THIRDS, 1 / 3))


def response(sink, active):
    """Binaural impulse response of the renderer for the given input channels driven together."""
    sw, inv = measure.sweep(level=0.1)   # low enough that the output limiter never acts: the response is level-independent
    frames = np.zeros((len(sw), len(CHANNELS)), np.float32)
    for ch in active:
        frames[:, CHANNELS.index(ch)] = sw
    path = f'{measure.OUT}/match_{"_".join(active)}.wav'
    measure.write(path, frames)
    rec = measure.capture(path, sink, window=slice(RATE, 8 * RATE))
    full = np.stack([signal.fftconvolve(rec[:, c], inv) for c in range(2)], 1)
    full /= np.max(np.abs(signal.fftconvolve(sw, inv)))
    onset = int(np.argmax(np.max(np.abs(full), 1)))
    return full[onset - 256:onset - 256 + 12000]


def main(dry_path, brir_path, tag='current'):
    _, dry = ar.load(dry_path)
    _, brir = ar.load(brir_path)
    layout = ar.brir_layout(dry, brir)
    measure.stop_engines()
    os.makedirs(measure.OUT, exist_ok=True)
    module, sp = measure.load_spatial()
    ours, apple = {}, {}
    try:
        for name in ('FC', 'FL', 'SL', 'RL'):
            ours[name] = response(module.SINK_NAME, [name])
            _, i, c = layout[name]
            apple[name] = np.stack([brir[:, i], brir[:, c]], 1)
        ours['pair'] = response(module.SINK_NAME, ['FL', 'FR'])
        _, i, c = layout['FL']
        apple['pair'] = np.stack([brir[:, i] + brir[:, c]] * 2, 1)
    finally:
        sp.stop()
        measure.stop_engines()

    diffs = {k: np.stack([thirds_db(ours[k][:, e]) - thirds_db(apple[k][:, e]) for e in (0, 1)]) for k in ours}
    gain = np.mean([d[:, ALIGNMENT_BANDS].mean() for d in diffs.values()])
    report = {}
    print(f'magnitude vs Apple, dB (ours - Apple, common gain {gain:.1f} dB removed)')
    print('band      ' + ' '.join(f'{c:>6.0f}' for c in THIRDS))
    for k, d in diffs.items():
        d = d - gain
        for e, ear in enumerate(('L', 'R')):
            print(f'{k:5} {ear}   ' + ' '.join(f'{v:6.1f}' for v in d[e]))
        report[k] = {'rms_db': round(float(np.sqrt(np.mean(d ** 2))), 2), 'max_db': round(float(np.max(np.abs(d))), 2)}

    print('interaural delay (us) at', [round(b) for b in ar.ITD_BANDS[::2]], 'Hz')
    for k in ('FL', 'SL', 'RL'):
        o = np.array(ar.itd_profile(ours[k][:400, 0], ours[k][:400, 1], RATE)) * 1e6
        a = np.array(ar.itd_profile(apple[k][:400, 0], apple[k][:400, 1], RATE)) * 1e6
        print(f'  {k} ours ', np.round(o[::2]).astype(int))
        print(f'  {k} Apple', np.round(a[::2]).astype(int))
        report[k]['itd_rms_us'] = round(float(np.sqrt(np.mean((o - a) ** 2))), 1)

    print('room per octave', ar.OCTAVES, ': decay (s), late-to-direct (dB)')
    for k in ('FC', 'FL', 'SL', 'RL'):
        for who, ir in (('ours ', ours[k]), ('Apple', apple[k])):
            room = ar.room(ir[:, 0], ir[:, 1], RATE)
            print(f'  {k} {who}', [round(b['decay_seconds'], 2) for b in room['bands']],
                  [round(b['late_to_direct_db'], 1) for b in room['bands']])
    print('summary', report)
    with open(f'{measure.OUT}/match_{tag}.json', 'w') as f:
        json.dump(report, f, indent=1)


if __name__ == '__main__':
    main(*sys.argv[1:4])
