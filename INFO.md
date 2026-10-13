# INFO.md — maintainer notes

## Plasmoid ID

`com.mintocha.airpods`

## Layout

```
contents/
├── ui/        QML widget (talks to the service over the session bus)
├── service/   Python service: org.mintocha.airpods on the session bus
│   ├── daemon.py     D-Bus object, device lifecycle, cache
│   ├── session.py    AAP L2CAP connection
│   ├── aap.py        AAP packet encoding/parsing
│   ├── proximity.py  BLE battery adverts
│   ├── controls.py   settings catalog shown in the widget
│   ├── media.py      ear-detection pause, conversation ducking
│   ├── spatial.py    PipeWire HRTF spatializer + head tracking
│   ├── binaural/     HRTF set and virtual room used by spatial.py (built by tools/build_binaural.py)
│   └── setup.sh      installs/starts airpods-plugin.service
└── icons/
tools/                not packaged
├── apple_reference.py   measures Apple's renderer from captures -> spatial_targets.json
├── build_binaural.py    builds contents/service/binaural/ from the KU100 set and spatial_targets.json
└── lab/                 PipeWire measurement container (see tools/lab/README.md)
```

Cached device state and preferences: `~/.config/airpods-plugin.json`.

## Commands

```bash
./install.sh                         # install widget + service locally
./package.sh                         # build dist/com.mintocha.airpods-<ver>.plasmoid for the KDE Store
journalctl --user -u airpods-plugin -f
```

## Spatial audio assets

```bash
python3 tools/apple_reference.py dry.wav brir.wav     # only when re-measuring Apple's renderer
python3 tools/build_binaural.py HRIR_FULL2DEG.sofa     # full rebuild (~4 min)
python3 tools/build_binaural.py --room-only            # room only, from the built HRTF
```

Verify any change in the lab container (`tools/lab/README.md`) before shipping.

## Release

1. Bump `Version` in `metadata.json`.
2. `./package.sh`
3. Upload the `.plasmoid` at https://store.kde.org (category: Plasma 6 Extensions -> Plasma Widgets).
