"""Builds the renderer's binaural assets: an HRTF set matched to Apple's generic Spatial Audio rendering and a
binaural room tail statistically matched to Apple's virtual room.

HRTF: the TH Koeln Neumann KU100 measurements (2 degree Gauss grid, CC BY-SA 3.0, https://zenodo.org/records/3928297)
are morphed toward spatial_targets.json (measured by apple_reference.py):
  - magnitudes: each direction gets a direction-independent EQ plus a residual correction that is exact at the
    measured virtual speaker directions and interpolated smoothly in between;
  - every response is rebuilt as minimum phase plus Apple's frequency-dependent interaural delay (the far ear is
    delayed mostly below ~1.5 kHz), which keeps phantom sources between virtual speakers free of comb filtering.
Room: per virtual speaker and ear, decaying noise per octave band with Apple's decay time and interaural coherence,
levelled so the total (direct + room) band energy at each ear matches Apple's capture.

Usage: python3 tools/build_binaural.py <HRIR_FULL2DEG.sofa>
       python3 tools/build_binaural.py --room-only     (re-derive the room from the built HRTF)
       python3 tools/build_binaural.py <HRIR_FULL2DEG.sofa|--room-only|--exact-only> <dry.wav> <brir.wav>
                                                       (also writes brir.wav: Apple's captured rendering itself)
"""

import json
import os
import sys

import h5py
import numpy as np
import sofar
from scipy import optimize, signal
from scipy.io import wavfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS = os.path.join(ROOT, 'tools', 'spatial_targets.json')
OUT_DIR = os.path.join(ROOT, 'contents', 'service', 'binaural')
sys.path.insert(0, os.path.join(ROOT, 'contents', 'service'))
from spatial import SPEAKERS, RATE  # noqa: E402
import apple_reference as ar  # noqa: E402

TAPS = 160
NFFT = 4096
MAX_ELEVATION = 40.0          # head pitch range the renderer can reach
# Every other measured ring and azimuth (~4 degrees) off the horizon: responses carry interaural delay only at low
# frequencies, where neighbours this far apart still interpolate without comb filtering. The horizon, where the
# virtual speakers sit and head turns move them, keeps every measured azimuth.
GRID_STRIDE = 2
MAX_CORRECTION_DB = 8.0
CORRECTION_TOP_HZ = (14000.0, 18000.0)   # captures are band-limited near 17 kHz: fade corrections out here
CORRECTION_FLOOR_HZ = 250.0              # 3 ms captures do not resolve lower bands
ROOM_SECONDS = 0.35
RATIO_LIMITS = (-0.25, 1.6)                 # Apple ITD / KU100 broadband ITD
ROOM_MATCHED_ABOVE_HZ = 2000.0             # late-to-direct is reliable here; lower bands match total energy


def spectrum_db(ir):
    return 20 * np.log10(np.abs(np.fft.rfft(ir, NFFT)) + 1e-12)


def bands_db(ir, centers):
    spec = np.abs(np.fft.rfft(ir, 1 << 15)) ** 2
    f = np.fft.rfftfreq(1 << 15, 1 / RATE)
    return np.array([10 * np.log10(spec[(f >= c / 2 ** (1 / 6)) & (f < c * 2 ** (1 / 6))].mean() + 1e-20)
                     for c in centers])


def arrival(ir):
    """Arrival delay in samples: lag between the response and its minimum-phase version (sub-sample)."""
    up = signal.resample(ir, len(ir) * 8)
    mp = signal.minimum_phase(up, method='homomorphic', half=False)
    c = signal.correlate(up, mp, mode='full')[len(mp) - 1:]
    i = int(np.argmax(c[:len(up) // 2]))
    if 0 < i < len(c) - 1:
        i += 0.5 * (c[i - 1] - c[i + 1]) / (c[i - 1] - 2 * c[i] + c[i + 1])
    return i / 8.0


def rebuild(magnitude_db, delay):
    """Minimum-phase response with the given magnitude, delayed by `delay` samples per bin, truncated to TAPS."""
    log_mag = np.log(10 ** (magnitude_db / 20))
    cep = np.fft.irfft(log_mag, NFFT)
    fold = np.zeros(NFFT)
    fold[0], fold[NFFT // 2] = cep[0], cep[NFFT // 2]
    fold[1:NFFT // 2] = 2 * cep[1:NFFT // 2]
    spec = np.exp(np.fft.rfft(fold))
    spec *= np.exp(-2j * np.pi * np.fft.rfftfreq(NFFT) * delay)
    ir = np.fft.irfft(spec, NFFT)[:TAPS]
    ir[-16:] *= np.hanning(32)[16:]
    return ir


def load_hrtf(path):
    f = h5py.File(path, 'r')
    pos = f['SourcePosition'][:]
    assert float(np.ravel(f['Data.SamplingRate'][:])[0]) == RATE
    rings = np.unique(np.round(pos[:, 1], 4))
    horizon = int(np.argmin(np.abs(rings)))
    kept_rings = rings[(np.arange(len(rings)) - horizon) % GRID_STRIDE == 0]
    azimuths = np.unique(np.round(pos[:, 0], 4))
    kept_azimuths = azimuths[::GRID_STRIDE]
    on_horizon = np.round(pos[:, 1], 4) == rings[horizon]
    on_grid = np.isin(np.round(pos[:, 1], 4), kept_rings) & np.isin(np.round(pos[:, 0], 4), kept_azimuths)
    keep = (on_horizon | on_grid) & (np.abs(pos[:, 1]) <= MAX_ELEVATION + 1e-6)
    return pos[keep], f['Data.IR'][:][keep].astype(np.float64), float(pos[0, 2])


def nearest(pos, azimuth, elevation=0.0):
    d = np.abs((pos[:, 0] - azimuth + 180) % 360 - 180) + np.abs(pos[:, 1] - elevation)
    return int(np.argmin(d))


def correction_field(pos, irs, targets):
    """Global EQ and per-ear residual corrections on the third-octave grid, keyed by left-ear azimuth."""
    centers = np.array(targets['third_octaves'])
    points = []   # (left-ear azimuth, apple band levels, ku100 band levels)
    for d in targets['directions'].values():
        az = d['azimuth']
        for ear_az, apple in ((az, d['ipsilateral_db']), ((-az) % 360, d['contralateral_db'])):
            if az == 0.0 and ear_az != az:
                continue
            ku = bands_db(irs[nearest(pos, ear_az), 0], centers)
            points.append((ear_az, np.array(apple), ku))
    diffs = np.array([a - k for _, a, k in points])
    reference = (centers >= 500) & (centers <= 2000)
    eq = diffs.mean(0)
    eq -= eq[reference].mean()
    low = centers < CORRECTION_FLOOR_HZ
    eq[low] = eq[~low][0]
    residual = {}
    for (ear_az, a, k), diff in zip(points, diffs):
        r = diff - diffs.mean(0)
        r[low] = r[~low][0] * (centers[low] / CORRECTION_FLOOR_HZ)
        residual[ear_az] = np.clip(r, -MAX_CORRECTION_DB, MAX_CORRECTION_DB)
    azimuths = np.array(sorted(residual))
    table = np.array([residual[a] for a in azimuths])
    return centers, eq, azimuths, table


def to_bins(centers, values):
    f = np.fft.rfftfreq(NFFT, 1 / RATE)
    out = np.interp(np.log2(np.maximum(f, 20.0)), np.log2(centers), values)
    lo, hi = CORRECTION_TOP_HZ
    fade = np.clip((hi - f) / (hi - lo), 0.0, 1.0)
    return out * fade


def morph(pos, irs, targets):
    centers, eq, azimuths, table = correction_field(pos, irs, targets)
    eq_bins = to_bins(centers, eq)

    itd_ratio = itd_ratios(pos, irs, targets)
    out = np.zeros((len(pos), 2, TAPS))
    for m, (az, el, _) in enumerate(pos):
        left, right = arrival(irs[m, 0]), arrival(irs[m, 1])
        mid, itd = (left + right) / 2, right - left
        lateral = np.degrees(np.arcsin(min(1.0, abs(np.sin(np.radians(az)) * np.cos(np.radians(el))))))
        itd_bins = itd * itd_ratio(lateral)
        delays = (mid - itd_bins / 2, mid + itd_bins / 2)
        weight = float(np.clip(1 - abs(el) / 45.0, 0.0, 1.0))
        for ear in (0, 1):
            ear_az = az % 360 if ear == 0 else (-az) % 360
            residual = np.array([np.interp(ear_az, azimuths, table[:, b], period=360) for b in range(len(centers))])
            corr = eq_bins + weight * to_bins(centers, residual)
            out[m, ear] = rebuild(spectrum_db(irs[m, ear]) + corr, delays[ear])
    # Unit energy at the front so libmysofa's loudness normalization leaves levels as built
    front = nearest(pos, 0.0)
    out *= np.sqrt(2.0 / np.sum(out[front] ** 2))
    return out


def itd_ratios(pos, irs, targets):
    """Apple's interaural delay relative to the KU100's broadband ITD, per bin, as a function of lateral angle."""
    bands = np.array(targets['itd_bands'])
    by_lateral = {}
    for d in targets['directions'].values():
        if d['azimuth'] == 0.0:
            continue
        m = nearest(pos, d['azimuth'])
        ku = (arrival(irs[m, 1]) - arrival(irs[m, 0])) / RATE
        lateral = round(np.degrees(np.arcsin(abs(np.sin(np.radians(d['azimuth']))))))
        by_lateral.setdefault(lateral, []).append(np.clip(np.array(d['itd_seconds']) / ku, *RATIO_LIMITS))
    laterals = np.array(sorted(by_lateral))
    table = np.array([np.mean(by_lateral[l], 0) for l in laterals])
    f = np.fft.rfftfreq(NFFT, 1 / RATE)
    log_f = np.log2(np.clip(f, bands[0], bands[-1]))

    def ratio(lateral):
        per_band = np.array([np.interp(lateral, laterals, table[:, b]) for b in range(len(bands))])
        return np.interp(log_f, np.log2(bands), per_band)
    return ratio


def octave_weights(f, centers):
    """Complementary raised-cosine octave bands on a log-frequency axis (they sum to one)."""
    x = np.log2(np.maximum(f, 1.0))
    c = np.log2(centers)
    w = np.zeros((len(centers), len(f)))
    for i in range(len(centers)):
        if i > 0:
            rise = (x >= c[i - 1]) & (x < c[i])
            w[i, rise] = np.sin(np.pi / 2 * (x[rise] - c[i - 1]) / (c[i] - c[i - 1])) ** 2
        else:
            w[i, x < c[i]] = 1.0
        if i < len(centers) - 1:
            fall = (x >= c[i]) & (x < c[i + 1])
            w[i, fall] = np.cos(np.pi / 2 * (x[fall] - c[i]) / (c[i + 1] - c[i])) ** 2
        else:
            w[i, x >= c[i]] = 1.0
        w[i, x == c[i]] = 1.0
    return w


def band_power(x, centers, width):
    """Mean power per rectangular band of `width` octaves: the measure apple_reference.py uses for totals."""
    spec = np.abs(np.fft.rfft(x, 1 << 15)) ** 2
    f = np.fft.rfftfreq(1 << 15, 1 / RATE)
    return np.array([spec[(f >= c / 2 ** (width / 2)) & (f < c * 2 ** (width / 2))].mean() for c in centers])


def band_cross(x, y, centers, width):
    """Real cross-power per band: how the room and the direct sound interfere in that band's total."""
    sx, sy = np.fft.rfft(x, 1 << 15), np.fft.rfft(y, 1 << 15)
    f = np.fft.rfftfreq(1 << 15, 1 / RATE)
    return np.array([np.real(sx * np.conj(sy))[(f >= c / 2 ** (width / 2)) & (f < c * 2 ** (width / 2))].mean()
                     for c in centers])


def room_tail(direct, room, low_thirds, seed):
    """Binaural late field (ipsilateral ear, contralateral ear) for one left-side or centre virtual speaker.

    The field is a sum of decaying noise bands (third octaves below 400 Hz to resolve room modes, octaves above)
    with Apple's decay time and interaural coherence. Each band's level is solved exactly, including its
    interference with the direct sound, so the total band power at each ear matches Apple: bands at or above
    ROOM_MATCHED_ABOVE_HZ take Apple's late-to-direct level, lower bands Apple's total, anchored between the two.
    """
    n = int(ROOM_SECONDS * RATE)
    rng = np.random.default_rng(seed)
    octaves = np.array([b['center'] for b in room['bands']])
    lows = np.array(low_thirds)
    centers = np.concatenate([lows, octaves[octaves > lows[-1] * 1.2]])
    width = np.where(centers <= lows[-1], 1 / 3, 1.0)

    def stat(key):
        return np.interp(np.log2(centers), np.log2(octaves), [b[key] for b in room['bands']])

    decay, coherence = stat('decay_seconds'), np.clip(stat('coherence'), -0.95, 0.95)
    late_to_direct, balance = stat('late_to_direct_db'), stat('ipsilateral_minus_contralateral_db')
    totals = [np.interp(np.log2(centers), np.log2(np.concatenate([lows, octaves])),
                        np.concatenate([room[f'low_{side}_db'], room[f'total_{side}_db']]))
              for side in ('ipsilateral', 'contralateral')]

    f = np.fft.rfftfreq(n, 1 / RATE)
    weights = octave_weights(f, centers)
    t = np.arange(n) / RATE
    start = min(arrival(direct[0]), arrival(direct[1])) / RATE + room['predelay_seconds']
    padded = [np.pad(d, (0, n - len(d))) for d in direct]
    components = [[], []]
    for i in range(len(centers)):
        # Band-limit stationary noise first, then window it: the field then cannot start before `start`
        a, c = (np.fft.irfft(np.fft.rfft(rng.standard_normal(n)) * weights[i], n) for _ in range(2))
        ramp = max(0.001, 1.0 / centers[i])   # about one period: no onset splatter outside the band
        onset = np.sin(np.pi / 2 * np.clip((t - start) / ramp, 0.0, 1.0)) ** 2
        env = onset * np.exp(-6.91 * np.maximum(t - start, 0) / max(decay[i], 0.02))
        for ear, x in enumerate((a, coherence[i] * a + np.sqrt(1 - coherence[i] ** 2) * c)):
            components[ear].append(x * env)

    nfft = 1 << 15
    f_fine = np.fft.rfftfreq(nfft, 1 / RATE)
    masks = [(f_fine >= c / 2 ** (w / 2)) & (f_fine < c * 2 ** (w / 2)) for c, w in zip(centers, width)]

    def powers(spectrum):
        return np.array([np.mean(np.abs(spectrum[m]) ** 2) for m in masks])

    direct_spec = [np.fft.rfft(padded[e], nfft) for e in (0, 1)]
    direct_power = [powers(direct_spec[e]) for e in (0, 1)]
    matched = centers >= ROOM_MATCHED_ABOVE_HZ
    share = 10 ** (balance / 10) / (1 + 10 ** (balance / 10))
    late = (direct_power[0] + direct_power[1]) * 10 ** (late_to_direct / 10)
    target = [direct_power[0] + late * share, direct_power[1] + late * (1 - share)]
    anchor = int(np.argmax(matched))
    level = (target[0][anchor] + target[1][anchor]) / sum(10 ** (totals[e][anchor] / 10) for e in (0, 1))
    for ear in (0, 1):
        target[ear] = np.where(matched, target[ear], level * 10 ** (totals[ear] / 10))

    # Bands overlap once windowed, and each interferes with the direct sound, so all band levels are solved
    # jointly: least squares on the dB error of every band's total power, levels kept non-negative.
    gains = []
    for ear in (0, 1):
        spectra = np.array([np.fft.rfft(c, nfft) for c in components[ear]])

        def error(g, ear=ear, spectra=spectra):
            return 10 * np.log10(powers(direct_spec[ear] + g @ spectra) / target[ear])

        own_power = np.array([powers(sp)[i] for i, sp in enumerate(spectra)])
        start_gain = np.sqrt(np.maximum(target[ear] - direct_power[ear], 0) / own_power)
        gains.append(optimize.least_squares(error, start_gain, bounds=(0, np.inf)).x)
    return [sum(g * c for g, c in zip(gains[ear], components[ear])) for ear in (0, 1)]


def write_hrtf(pos, hrtf):
    sofa = sofar.Sofa('SimpleFreeFieldHRIR')
    sofa.Data_IR = hrtf
    sofa.Data_SamplingRate = float(RATE)
    sofa.SourcePosition = pos
    sofa.SourcePosition_Type = 'spherical'
    sofa.SourcePosition_Units = 'degree, degree, metre'
    sofa.GLOBAL_Title = 'Neumann KU100 HRIRs matched to Apple generic Spatial Audio rendering'
    sofa.GLOBAL_License = 'CC BY-SA 3.0'
    sofa.GLOBAL_Organization = 'Derived from TH Koeln KU100 HRIR measurements (Bernschuetz)'
    sofa.GLOBAL_References = 'https://zenodo.org/records/3928297'
    sofa.GLOBAL_Comment = ('Minimum phase with frequency-dependent interaural delay and magnitudes matched to '
                           f'tools/spatial_targets.json; elevations within +-{MAX_ELEVATION:g} deg.')
    sofar.write_sofa(os.path.join(OUT_DIR, 'hrtf.sofa'), sofa, compression=9)


def speaker_channels(columns):
    """room.wav / brir.wav layout: left-ear then right-ear response per virtual speaker, in SPEAKERS order.

    `columns` maps a speaker's azimuth magnitude to its (ipsilateral, contralateral) responses. Right-side speakers
    get the mirror image of their left counterpart, as in a symmetric room.
    """
    channels = []
    for _, azimuth, _ in SPEAKERS:
        ipsi, contra = columns[abs(azimuth)]
        channels += [contra, ipsi] if azimuth < 0 else [ipsi, contra]
    return np.array(channels, np.float32).T


def exact_brir(dry_path, brir_path):
    """Apple's captured virtual-speaker rendering itself (direct sound and room), for the fixed-pose renderer.

    The capture's lead-in is trimmed by one common amount so interaural delays are kept.
    """
    _, dry = ar.load(dry_path)
    _, brir = ar.load(brir_path)
    layout = ar.brir_layout(dry, brir)
    used = {abs(a): (i, c) for _, (a, i, c) in layout.items()}
    lead = max(0, min(ar.onset(brir[:, k]) for pair in used.values() for k in pair) - 32)
    columns = {a: (brir[lead:, i], brir[lead:, c]) for a, (i, c) in used.items()}
    wavfile.write(os.path.join(OUT_DIR, 'brir.wav'), RATE, speaker_channels(columns))
    print(f'brir.wav: {len(brir) - lead} samples, lead-in {lead} trimmed')


def main(source, *captures):
    with open(TARGETS) as f:
        targets = json.load(f)
    os.makedirs(OUT_DIR, exist_ok=True)
    if captures:
        exact_brir(*captures)
    if source == '--room-only':
        built = h5py.File(os.path.join(OUT_DIR, 'hrtf.sofa'), 'r')
        pos, hrtf = built['SourcePosition'][:], built['Data.IR'][:]
    elif source == '--exact-only':
        return
    else:
        pos, irs, _ = load_hrtf(source)
        hrtf = morph(pos, irs, targets)
        write_hrtf(pos, hrtf)

    # Right-side speakers get the mirror image of their left counterpart's room, as in a symmetric room:
    # their late fields then add coherently at low frequencies (room modes) and stay decorrelated higher up.
    directions = {d['azimuth']: d for d in targets['directions'].values()}
    tails = {}
    for azimuth in sorted({abs(a) for _, a, _ in SPEAKERS}):
        m = nearest(pos, azimuth)
        room = directions[azimuth]['room']
        if azimuth == 0.0:
            # The capture holds one centre column for both ears, so its interaural coherence is degenerate
            room = dict(room, bands=[dict(b, coherence=fb['coherence']) for b, fb in
                                     zip(room['bands'], directions[30.0]['room']['bands'])])
        tails[azimuth] = room_tail((hrtf[m, 0], hrtf[m, 1]), room, targets['low_thirds'], seed=int(azimuth))
    wavfile.write(os.path.join(OUT_DIR, 'room.wav'), RATE, speaker_channels(tails))
    print(f'{len(pos)} directions, room {2 * len(SPEAKERS)} channels')


if __name__ == '__main__':
    main(*sys.argv[1:])
