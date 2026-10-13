"""Loudness (ITU-R BS.1770 K-weighted) and peak level of the spatial path against direct playback.

  - stereo pink noise at three inter-channel correlations (mono, typical mix, fully wide);
  - a 0 dBFS bass-heavy, centre-panned music-like mix: the clipping headroom case.

Usage (inside the lab container): python3 loudness.py
"""

import numpy as np
from scipy import signal

import measure

RATE = measure.RATE
# BS.1770-4 K-weighting at 48 kHz
K1 = ([1.53512485958697, -2.69169618940638, 1.19839281085285], [1.0, -1.69065929318241, 0.73248077421585])
K2 = ([1.0, -2.0, 1.0], [1.0, -1.99004745483398, 0.99007225036621])


def lufs(x):
    y = signal.lfilter(*K2, signal.lfilter(*K1, x, axis=0), axis=0)
    return -0.691 + 10 * np.log10(np.sum(np.mean(y ** 2, axis=0)))


def stereo_pink(correlation, seconds=8, seed=3):
    a, b = measure.pink(seconds, seed=seed), measure.pink(seconds, seed=seed + 1)
    mid, side = np.sqrt((1 + correlation) / 2) * a, np.sqrt((1 - correlation) / 2) * b
    return np.stack([mid + side, mid - side], 1).astype(np.float32)


def music_like(seconds=8, seed=5):
    """120 bpm kick, sub bass line and vocal-band tone bursts in the centre, wide pink pad; peak 0 dBFS."""
    t = np.arange(int(seconds * RATE)) / RATE
    beat = t % 0.5
    kick = np.sin(2 * np.pi * (55 + 120 * np.exp(-beat * 30)) * beat) * np.exp(-beat * 9)
    bass = 0.5 * np.sin(2 * np.pi * np.where((t % 2) < 1, 41.2, 55.0) * t) * (0.6 + 0.4 * np.sin(np.pi * t))
    vocal = 0.35 * np.sin(2 * np.pi * 440 * t + 3 * np.sin(2 * np.pi * 5 * t)) * ((t % 1) < 0.7)
    centre = kick + bass + vocal
    mix = np.stack([centre + measure.pink(seconds, seed=seed + 1), centre + measure.pink(seconds, seed=seed + 2)], 1)
    return (mix / np.max(np.abs(mix)) * 0.999).astype(np.float32)


def level(sink, frames, name):
    path = f'{measure.OUT}/loudness_{name}.wav'
    measure.write(path, frames)
    window = slice(RATE, len(frames) - RATE // 2)
    rec = measure.capture(path, sink, window=window)[window]
    return lufs(rec), 20 * np.log10(np.max(np.abs(rec)) + 1e-12)


def main():
    measure.stop_engines()
    module, sp = measure.load_spatial()
    try:
        cases = [(f'pink, correlation {c}', stereo_pink(c)) for c in (1.0, 0.6, 0.0)]
        cases.append(('0 dBFS bass-heavy mix', music_like()))
        for i, (name, frames) in enumerate(cases):
            direct, direct_peak = level(measure.TARGET, frames, f'direct{i}')
            spatial, spatial_peak = level(module.SINK_NAME, frames, f'spatial{i}')
            print(f'{name}: spatial - direct {spatial - direct:+.2f} LU, peaks {direct_peak:+.2f} / '
                  f'{spatial_peak:+.2f} dBFS')
    finally:
        sp.stop()
        measure.stop_engines()


if __name__ == '__main__':
    main()
