"""Measures the spatial renderer through the real PipeWire engine: plays test signals into the spatial sink,
records the AirPods stand-in sink, and reports cleanliness.

  - sweeps (centred and hard-left source): peaks, clipping, impulse responses;
  - 1 kHz tone and pink noise, static and with a simulated head turn fed through HeadPose like the daemon:
    broadband noise floor and the loudest high-frequency burst ("grain") relative to the tone.

Usage (inside the lab container): python3 measure.py [--sink spatial|direct] [--tag NAME] [--only sweep,tone,pink]
Set LAB_SERVICE to measure another checkout's service directory.
"""

import argparse
import importlib
import json
import os
import subprocess
import sys
import time

import numpy as np
from gi.repository import GLib
from scipy import signal
from scipy.io import wavfile

sys.path.insert(0, os.environ.get('LAB_SERVICE', '/repo/contents/service'))
RATE = 48000
TARGET = 'airpods_sim'
OUT = '/tmp/lab'


def sweep(seconds=8.0, f0=20.0, f1=20000.0, level=0.5):
    """Exponential sine sweep and its inverse filter (Farina)."""
    t = np.arange(int(seconds * RATE)) / RATE
    k = np.log(f1 / f0)
    s = level * np.sin(2 * np.pi * f0 * seconds / k * (np.exp(t * k / seconds) - 1))
    fade = int(0.01 * RATE)
    s[-fade:] *= np.linspace(1, 0, fade)
    inv = s[::-1] * np.exp(-t * k / seconds)
    return s.astype(np.float32), inv


def tone(seconds, freq=1000.0, level=0.2):
    t = np.arange(int(seconds * RATE)) / RATE
    return (level * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def pink(seconds, level=0.075, seed=1):
    n = int(seconds * RATE)
    spec = np.fft.rfft(np.random.default_rng(seed).standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / RATE)
    spec[1:] /= np.sqrt(f[1:])
    spec[0] = 0
    x = np.fft.irfft(spec, n)
    return (level * x / np.sqrt(np.mean(x ** 2))).astype(np.float32)


def write(path, frames):
    wavfile.write(path, RATE, np.asarray(frames, np.float32))


def dropouts(rec, window):
    """Runs of >= 32 exact-zero samples inside the analysed window: lost processing blocks, not DSP output."""
    silent = np.all(rec[window] == 0.0, axis=1).astype(np.int8)
    edges = np.diff(np.concatenate([[0], silent, [0]]))
    return int(np.sum((np.where(edges == -1)[0] - np.where(edges == 1)[0]) >= 32))


def capture(play_path, sink, motion=None, window=slice(None), attempts=4):
    """Recording of play_path through sink, re-taken while the analysed window has scheduling dropouts."""
    for attempt in range(attempts):
        rec = _record(play_path, sink, motion)
        lost = dropouts(rec, window)
        if not lost:
            return rec
        print(f'dropout x{lost} in {play_path} (attempt {attempt + 1}), re-capturing', file=sys.stderr)
    raise SystemExit(f'{play_path}: persistent dropouts')


def _record(play_path, sink, motion=None):
    rec_path = play_path.replace('.wav', '.rec.raw')
    rec = subprocess.Popen(['pw-record', '--target', TARGET, '-P', '{ stream.capture.sink = true }',
                            '--latency', '200ms', '--format', 'f32', '--rate', str(RATE), '--channels', '2',
                            '--raw', rec_path])
    time.sleep(0.6)
    play = subprocess.Popen(['pw-play', '--target', sink, play_path])
    context = GLib.MainContext.default()
    start = time.monotonic()
    while play.poll() is None:
        if motion:
            motion(time.monotonic() - start)
        while context.iteration(False):
            pass
        time.sleep(0.02)
    time.sleep(0.6)
    rec.terminate()
    rec.wait()
    raw = np.fromfile(rec_path, np.float32)
    os.remove(rec_path)
    return raw[:len(raw) // 2 * 2].reshape(-1, 2)


def stop_engines():
    """Engines left by an aborted run would add a second sink of the same name and corrupt every capture."""
    subprocess.run(['pkill', '-9', '-f', 'pipewire -c'])
    subprocess.run(['pkill', '-9', '-x', 'pw-cli'])
    time.sleep(0.5)


def load_spatial(tracked=False):
    import spatial
    importlib.reload(spatial)
    sp = spatial.Spatializer(lambda: None)
    sp.start(TARGET, 'AirPods Spatial (lab)', tracked)
    context = GLib.MainContext.default()
    deadline = time.monotonic() + 20
    while not sp.active and sp.engine and time.monotonic() < deadline:
        context.iteration(True)
    if not sp.active:
        raise SystemExit('spatial sink failed to attach')
    time.sleep(0.5)
    return spatial, sp


def levels(x):
    peak = float(np.max(np.abs(x)))
    rms = float(np.sqrt(np.mean(x ** 2)))
    return {'peak_dbfs': round(float(20 * np.log10(peak + 1e-12)), 2),
            'rms_dbfs': round(float(20 * np.log10(rms + 1e-12)), 2),
            'clipped_samples': int(np.sum(np.abs(x) >= 0.999))}


def noise_floor(rec, freq):
    """Energy away from the tone and its harmonics (+-150 Hz) relative to the tone, dB per channel.

    Direction changes legitimately modulate the tone (close sidebands); grain shows up as broadband energy.
    """
    out = []
    for ch in range(2):
        f, p = signal.welch(rec[:, ch], RATE, window='blackmanharris', nperseg=16384)
        near = np.zeros_like(f, bool)
        for h in range(1, int(RATE / 2 / freq) + 1):
            near |= np.abs(f - h * freq) < 150
        tone_power = p[np.abs(f - freq) < 150].sum()
        out.append(round(float(10 * np.log10(p[~near & (f > 200)].sum() / tone_power + 1e-20)), 2))
    return out


def grain(rec, freq):
    """Loudest 2 ms burst above 3x the tone frequency, relative to the tone RMS (dB)."""
    b, a = signal.butter(8, 3 * freq / (RATE / 2), 'high')
    worst = -200.0
    for ch in range(2):
        h = signal.filtfilt(b, a, rec[:, ch])[int(0.05 * RATE):-int(0.05 * RATE)]
        w = int(0.002 * RATE)
        env = np.sqrt(np.mean(h[:len(h) // w * w].reshape(-1, w) ** 2, 1))
        worst = max(worst, float(20 * np.log10(env.max() / np.sqrt(np.mean(rec[:, ch] ** 2)) + 1e-20)))
    return round(worst, 2)


def head_turn(module, sp):
    """+-45 degrees at 0.4 Hz with sensor jitter, fed through HeadPose as AirPods quaternions."""
    pose = module.HeadPose()
    rng = np.random.default_rng(7)

    def motion(t):
        yaw = 45 * np.sin(2 * np.pi * 0.4 * t) + rng.normal(0, 0.3)
        sp.steer(*pose.update((0.0, 0.0, float(np.sin(np.radians(yaw) / 2)))))
    return motion


def run(args):
    stop_engines()
    os.makedirs(OUT, exist_ok=True)
    only = set(args.only.split(','))
    report = {}
    module, sp = load_spatial(tracked=True) if args.sink == 'spatial' else (None, None)
    sink = module.SINK_NAME if sp else TARGET
    try:
        if 'sweep' in only:
            sw, inv = sweep()
            for name, frames in (('center', np.stack([sw, sw], 1)), ('left', np.stack([sw, 0 * sw], 1))):
                path = f'{OUT}/sweep_{name}.wav'
                write(path, frames)
                rec = capture(path, sink, window=slice(RATE, 8 * RATE))
                full = np.stack([signal.fftconvolve(rec[:, c], inv) for c in range(2)], 1)
                onset = int(np.argmax(np.max(np.abs(full), 1)))
                ir = full[onset - 512:onset - 512 + 24000] / np.max(np.abs(signal.fftconvolve(sw, inv)))
                np.save(f'{OUT}/ir_{args.tag}_{name}.npy', ir)
                report[f'sweep_{name}'] = levels(rec)

        window = slice(RATE, 5 * RATE)
        for name, mono in (('tone', tone(6)), ('pink', pink(6))):
            if name not in only:
                continue
            path = f'{OUT}/{name}.wav'
            write(path, np.stack([mono, mono], 1))
            conditions = [('static', None)] + ([('moving', head_turn(module, sp))] if sp else [])
            for condition, motion in conditions:
                if sp:
                    sp.center()
                rec = capture(path, sink, motion=motion, window=window)[window]
                key = f'{name}_{condition}'
                report[key] = levels(rec)
                if name == 'tone':
                    report[key].update(noise_floor_db=noise_floor(rec, 1000), grain_db=grain(rec, 1000))
    finally:
        if sp:
            sp.stop()
        stop_engines()
    with open(f'{OUT}/report_{args.tag}.json', 'w') as f:
        json.dump(report, f, indent=1)
    for key, value in report.items():
        print(key, value)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--sink', choices=['spatial', 'direct'], default='spatial')
    p.add_argument('--tag', default='current')
    p.add_argument('--only', default='sweep,tone,pink')
    run(p.parse_args())
