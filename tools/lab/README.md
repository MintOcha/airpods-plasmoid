# Spatial audio lab

Measures the renderer through a real PipeWire engine with a null sink standing in for the AirPods,
so changes to `contents/service/spatial.py` or the binaural assets can be verified without hardware.

```bash
docker build -t airpods-spatial-lab tools/lab
docker run -d --init --name airpods-lab --cap-add SYS_NICE --ulimit rtprio=95 --ulimit memlock=-1 \
    -v "$PWD":/repo -v /path/to/apple-captures:/apple:ro airpods-spatial-lab
docker exec airpods-lab python3 measure.py        # clipping, noise floor, head-tracking grain
docker exec airpods-lab python3 loudness.py       # loudness vs direct playback, headroom
docker exec airpods-lab python3 match_apple.py /apple/dry.wav /apple/brir.wav   # error vs Apple's rendering
```

`LAB_SERVICE=/path/to/other/contents/service` measures another checkout for before/after comparisons.
Real-time scheduling (`SYS_NICE`, `rtprio`) keeps the engine from dropping blocks on a busy host; captures
that still contain dropped blocks are re-taken, never measured.

`match_apple.py` and `tools/apple_reference.py` need the community captures of Apple's renderer from the
HeSuVi forum ("Apple Spatial Audio" thread: `AppleSpatialAudioHRIRv1` dry set, `AppleSpatialAudio+` BRIR set).
They are not redistributed here; only measurements derived from them are (`tools/spatial_targets.json`).
