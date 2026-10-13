# AirPods

<img width="512" height="512" alt="logo" src="https://github.com/user-attachments/assets/c36b15b9-ea96-4a01-a830-09e471bd6446" />


A Plasma 6 plasmoid for AirPods: exact battery, listening modes, and the settings Apple devices expose.

## Features

- **Panel widget** - AirPods icon with the lowest earbud battery
- **Exact battery** - left, right and case to the percent, with charging and in-ear state
- **Battery without connecting** - paired AirPods report their battery over Bluetooth LE when the case is opened nearby; otherwise the last-known state is shown, greyed out
- **Listening modes** - Off, Transparency, Adaptive, Noise Cancellation
- **Press-and-hold cycle** - choose which modes the stem cycles through; saved on the AirPods
- **AirPods settings** - Adaptive Audio level, Conversation Awareness, Personalized Volume, press speed/duration, swipe volume, ear detection, microphone, and more. Only settings your model reports are shown
- **Ear detection** - pauses media when an AirPod is removed, resumes when it is put back
- **Conversation Awareness** - lowers volume while you speak
- **Spatial Audio** - virtual 7.1 HRTF rendering, fixed or head tracked
- **Any AirPods** - detected from their Bluetooth service, not a fixed address

## Requirements

- Plasma 6, PipeWire, BlueZ
- `python-dbus`, `python-gobject`, `python-cryptography`
- `libmysofa` (Spatial Audio)

## Installation

### KDE Store

Add Widgets -> Get New Widgets -> search **AirPods**. The widget starts its background service on first run.

### From source

```bash
./install.sh
```

Then right-click panel -> Add Widgets -> AirPods.

Exact battery while disconnected needs the AirPods to have connected once with the service running.

## License

MIT

## Credits

- AirPods protocol documentation: [LibrePods](https://github.com/librepods-org/librepods) reverse-engineering notes (no LibrePods code is included)
- Icon: [MingCute](https://github.com/Richard9394/MingCute) (Apache-2.0)
