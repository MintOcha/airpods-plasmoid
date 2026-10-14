"""Measures the rendering targets of Apple's generic Spatial Audio renderer from community captures.

Inputs are HeSuVi 7-channel captures of Apple's "Spatialize Stereo" output (posted on the HeSuVi forum):
a dry HRIR set and a reverberant BRIR set. Only measurements are written out (spatial_targets.json);
build_binaural.py separately trims the BRIR capture into the fixed-mode renderer's brir.wav.

The posted BRIR files do not agree on which column holds which speaker, so BRIR columns are assigned to the dry
set's speakers by matching each (ipsilateral, contralateral) pair's direct sound.

Usage: python3 tools/apple_reference.py <dry.wav> <brir.wav>
"""

import json
import os
import sys

import numpy as np
from scipy import signal
from scipy.io import wavfile

# 7-channel symmetric HeSuVi layout: (ipsilateral, contralateral) column per left-side speaker; FC is one column.
LAYOUT = {'FC': (0.0, 6, 6), 'FL': (30.0, 0, 1), 'SL': (90.0, 2, 3), 'RL': (150.0, 4, 5)}
THIRD_OCTAVES = [float(round(1000 * 2 ** (k / 3), 1)) for k in range(-10, 12)]   # 100 Hz .. 14.4 kHz
ITD_BANDS = [float(round(1000 * 2 ** (k / 3), 1)) for k in range(-7, 10)]      # 200 Hz .. 8 kHz
OCTAVES = [63.0, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0]
LOW_THIRDS = [float(round(1000 * 2 ** (k / 3), 1)) for k in range(-13, -4)]   # 50 .. 315 Hz: room modes
DIRECT_SECONDS = 0.0025
NFFT = 1 << 15
OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'spatial_targets.json')


def load(path):
    rate, data = wavfile.read(path)
    if data.dtype.kind == 'i':
        data = data / float(np.iinfo(data.dtype).max)
    return rate, data.astype(np.float64)


def band_levels(ir, rate, centers, width):
    spec = np.abs(np.fft.rfft(ir, NFFT)) ** 2
    f = np.fft.rfftfreq(NFFT, 1 / rate)
    return [float(10 * np.log10(spec[(f >= c / 2 ** (width / 2)) & (f < c * 2 ** (width / 2))].mean() + 1e-20))
            for c in centers]


def minimum_phase_angle(ir, n):
    cep = np.fft.irfft(np.log(np.abs(np.fft.rfft(ir, n)) + 1e-12), n)
    fold = np.zeros(n)
    fold[0], fold[n // 2] = cep[0], cep[n // 2]
    fold[1:n // 2] = 2 * cep[1:n // 2]
    return np.imag(np.fft.rfft(fold))


def itd_profile(ipsi, contra, rate):
    """Interaural delay per third octave (seconds): the excess-phase part of the interaural phase difference.

    Apple's renderer delays the far ear mostly at low frequencies; a single broadband ITD cannot describe it.
    """
    n = 4096
    f = np.fft.rfftfreq(n, 1 / rate)
    hi, hc = np.fft.rfft(ipsi, n), np.fft.rfft(contra, n)
    excess = np.unwrap(np.angle(hc / hi)) - (minimum_phase_angle(contra, n) - minimum_phase_angle(ipsi, n))
    tau = -excess / (2 * np.pi * np.maximum(f, 1.0))
    weight = np.abs(hc) ** 2
    out = []
    for c in ITD_BANDS:
        band = (f >= c / 2 ** (1 / 6)) & (f < c * 2 ** (1 / 6))
        out.append(float(np.average(tau[band], weights=weight[band])))
    return out


def onset(x):
    return int(np.argmax(np.abs(x) > 0.1 * np.max(np.abs(x))))


def octave(x, rate, center):
    sos = signal.butter(4, [center / np.sqrt(2), min(center * np.sqrt(2), rate / 2 * 0.95)], 'bandpass',
                        fs=rate, output='sos')
    return signal.sosfiltfilt(sos, x)


def decay_seconds(x, rate):
    """Reverberation time extrapolated from the -5..-25 dB span of the Schroeder decay curve."""
    edc = np.cumsum(x[::-1] ** 2)[::-1]
    db = 10 * np.log10(edc / edc[0] + 1e-30)
    i5, i25 = np.argmax(db < -5), np.argmax(db < -25)
    return float(3.0 * (i25 - i5) / rate)


def room(ipsi, contra, rate):
    """Late-field statistics per octave plus the total (direct + room) band energy at each ear."""
    start = min(onset(ipsi), onset(contra))
    split = start + int(DIRECT_SECONDS * rate)
    bands = []
    for c in OCTAVES:
        bi, bc = octave(ipsi, rate, c), octave(contra, rate, c)
        direct = np.sum(bi[start:split] ** 2) + np.sum(bc[start:split] ** 2)
        late_i, late_c = bi[split:], bc[split:]
        coherence = np.sum(late_i * late_c) / np.sqrt(np.sum(late_i ** 2) * np.sum(late_c ** 2))
        balance = 10 * np.log10(np.sum(late_i ** 2) / np.sum(late_c ** 2))
        bands.append({'center': c,
                      'late_to_direct_db': float(10 * np.log10((np.sum(late_i ** 2) + np.sum(late_c ** 2)) / direct)),
                      'ipsilateral_minus_contralateral_db': float(balance),
                      'coherence': float(coherence),
                      'decay_seconds': decay_seconds(np.hypot(late_i, late_c), rate)})
    env = np.abs(signal.hilbert(ipsi[split:] + contra[split:]))
    env = np.convolve(env, np.ones(48) / 48, mode='same')
    predelay = (np.argmax(env > env.max() * 0.1) + split - start) / rate
    return {'bands': bands, 'predelay_seconds': float(predelay),
            'total_ipsilateral_db': band_levels(ipsi, rate, OCTAVES, 1.0),
            'total_contralateral_db': band_levels(contra, rate, OCTAVES, 1.0),
            'low_ipsilateral_db': band_levels(ipsi, rate, LOW_THIRDS, 1 / 3),
            'low_contralateral_db': band_levels(contra, rate, LOW_THIRDS, 1 / 3)}


def similarity(brir_column, dry_column):
    start = onset(brir_column)
    direct = brir_column[max(0, start - 20):start + 120]
    c = np.correlate(direct, dry_column, 'full')
    return np.max(np.abs(c)) / np.linalg.norm(direct) / np.linalg.norm(dry_column)


def brir_layout(dry, brir):
    """Speaker -> (ipsilateral, contralateral) BRIR columns, chosen by direct-sound similarity to the dry set."""
    pairs = [(0, 1), (2, 3), (4, 5), (6, 6)]
    layout = {}
    for name, (azimuth, ipsi, contra) in LAYOUT.items():
        scores = [similarity(brir[:, i], dry[:, ipsi]) + similarity(brir[:, c], dry[:, contra]) for i, c in pairs]
        layout[name] = (azimuth, *pairs[int(np.argmax(scores))])
    assert len({v[1:] for v in layout.values()}) == len(layout), f'ambiguous BRIR layout {layout}'
    return layout


def main(dry_path, brir_path):
    rate, dry = load(dry_path)
    brate, brir = load(brir_path)
    rooms = brir_layout(dry, brir)
    directions = {}
    for name, (azimuth, ipsi, contra) in LAYOUT.items():
        directions[name] = {
            'azimuth': azimuth,
            'ipsilateral_db': band_levels(dry[:, ipsi], rate, THIRD_OCTAVES, 1 / 3),
            'contralateral_db': band_levels(dry[:, contra], rate, THIRD_OCTAVES, 1 / 3),
            'itd_seconds': itd_profile(dry[:, ipsi], dry[:, contra], rate),
            'room': room(brir[:, rooms[name][1]], brir[:, rooms[name][2]], brate),
        }
    with open(OUTPUT, 'w') as f:
        json.dump({'source': 'Apple Spatial Audio (Spatialize Stereo) community captures, HeSuVi forum',
                   'third_octaves': THIRD_OCTAVES, 'itd_bands': ITD_BANDS, 'octaves': OCTAVES, 'low_thirds': LOW_THIRDS,
                   'directions': directions}, f, indent=1)
    print(OUTPUT, {k: v[1:] for k, v in rooms.items()})


if __name__ == '__main__':
    main(*sys.argv[1:3])
