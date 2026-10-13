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
│   └── setup.sh      installs/starts airpods-plugin.service
└── icons/
```

Cached device state and preferences: `~/.config/airpods-plugin.json`.

## Commands

```bash
./install.sh                         # install widget + service locally
./package.sh                         # build dist/com.mintocha.airpods-<ver>.plasmoid for the KDE Store
journalctl --user -u airpods-plugin -f
```

## Release

1. Bump `Version` in `metadata.json`.
2. `./package.sh`
3. Upload the `.plasmoid` at https://store.kde.org (category: Plasma 6 Extensions -> Plasma Widgets).
